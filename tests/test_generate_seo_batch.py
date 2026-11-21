import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def _config(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


google_package = ModuleType("google")
google_package.__path__ = []
genai_package = ModuleType("google.genai")
genai_package.__path__ = []
genai_package.Client = object
types_module = ModuleType("google.genai.types")
types_module.GenerateContentConfig = _config
types_module.HttpOptions = _config
types_module.HttpRetryOptions = _config
genai_package.types = types_module
google_package.genai = genai_package
sys.modules.update(
    {
        "google": google_package,
        "google.genai": genai_package,
        "google.genai.types": types_module,
    }
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_seo_batch.py"
SPEC = importlib.util.spec_from_file_location("generate_seo_batch", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load SEO engine from {SCRIPT_PATH}")
seo_batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seo_batch)


class GenerateContentTests(unittest.TestCase):
    def test_uses_supported_model_and_parses_structured_json(self) -> None:
        expected = {
            "title": "Custom Mailer Boxes",
            "meta_title": "Custom Mailer Boxes",
            "meta_description": "Packaging for shipping.",
            "short_description": "A tailored mailer box.",
            "body_html": '<p class="ignored">Review sizing options.</p>',
            "suggested_slug": "Custom Mailer Boxes",
            "image_search_query": "custom mailer box",
            "keyword_ideas": ["mailer packaging", "printed mailer boxes", "shipping mailers"],
            "image_url": "",
        }

        class FakeModels:
            call = None

            def generate_content(self, **kwargs: object) -> SimpleNamespace:
                self.call = kwargs
                return SimpleNamespace(text=json.dumps(expected))

        models = FakeModels()
        result = seo_batch.generate_content(SimpleNamespace(models=models), "custom mailer boxes")

        self.assertEqual(models.call["model"], "gemini-3.8-flash")
        self.assertEqual(models.call["config"].response_schema["properties"]["keyword_ideas"]["minItems"], 3)
        self.assertEqual(models.call["config"].response_schema["properties"]["keyword_ideas"]["maxItems"], 5)
        self.assertEqual(result["suggested_slug"], "custom-mailer-boxes")
        self.assertEqual(result["body_html"], "<p>Review sizing options.</p>")

    def test_retries_once_after_remote_protocol_disconnect(self) -> None:
        response_data = {
            "title": "Custom Mailer Boxes",
            "meta_title": "Custom Mailer Boxes",
            "meta_description": "Packaging for shipping.",
            "short_description": "A tailored mailer box.",
            "body_html": "<p>Review sizing options.</p>",
            "suggested_slug": "custom-mailer-boxes",
            "image_search_query": "custom mailer box",
            "keyword_ideas": ["mailer packaging", "printed mailer boxes", "shipping mailers"],
            "image_url": "",
        }

        class FakeModels:
            attempts = 0

            def generate_content(self, **kwargs: object) -> SimpleNamespace:
                self.attempts += 1
                if self.attempts == 1:
                    raise seo_batch.RemoteProtocolError("server disconnected")
                return SimpleNamespace(text=json.dumps(response_data))

        models = FakeModels()
        with patch.object(seo_batch.time, "sleep") as sleep:
            result = seo_batch.generate_content(
                SimpleNamespace(models=models),
                "custom mailer boxes",
            )

        self.assertEqual(models.attempts, 2)
        self.assertEqual(result["suggested_slug"], "custom-mailer-boxes")
        sleep.assert_called_once_with(2)

    def test_rejects_too_few_keyword_ideas(self) -> None:
        response_data = {
            "title": "Custom Mailer Boxes",
            "meta_title": "Custom Mailer Boxes",
            "meta_description": "Packaging for shipping.",
            "short_description": "A tailored mailer box.",
            "body_html": "<p>Review sizing options.</p>",
            "suggested_slug": "custom-mailer-boxes",
            "image_search_query": "custom mailer box",
            "keyword_ideas": ["printed mailer boxes"],
            "image_url": "",
        }

        class FakeModels:
            def generate_content(self, **kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(text=json.dumps(response_data))

        with self.assertRaisesRegex(RuntimeError, "3 to 5 distinct keyword_ideas"):
            seo_batch.generate_content(SimpleNamespace(models=FakeModels()), "custom mailer boxes")


class ClientConfigurationTests(unittest.TestCase):
    def test_configures_bounded_retries_for_transient_api_failures(self) -> None:
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch.object(seo_batch, "read_keywords", return_value=[]),
            patch.object(seo_batch.genai, "Client") as client_factory,
        ):
            self.assertEqual(seo_batch.main(), 0)

        http_options = client_factory.call_args.kwargs["http_options"]
        retry_options = http_options.retry_options
        self.assertEqual(http_options.timeout, 60_000)
        self.assertEqual(retry_options.attempts, 3)
        self.assertEqual(retry_options.initial_delay, 1.0)
        self.assertEqual(retry_options.max_delay, 4.0)
        self.assertIn(503, retry_options.http_status_codes)

    def test_requires_webhook_token_when_webhook_url_is_set(self) -> None:
        with patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "test-key", "GOOGLE_SHEET_WEBHOOK_URL": "https://example.test/exec"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "Set WEBHOOK_TOKEN"):
                seo_batch.main()


class WebhookContractTests(unittest.TestCase):
    def test_staging_row_matches_sheet_headers_and_posts_token(self) -> None:
        data = {
            "title": "Custom Mailer Boxes",
            "meta_title": "Custom Mailer Boxes",
            "meta_description": "Packaging for shipping.",
            "short_description": "A tailored mailer box.",
            "body_html": "<p>Review sizing options.</p>",
            "suggested_slug": "custom-mailer-boxes",
            "image_search_query": "custom mailer box",
            "keyword_ideas": ["mailer packaging", "printed mailer boxes", "shipping mailers"],
            "image_url": "",
        }

        with tempfile.TemporaryDirectory() as staging_dir:
            with (
                patch.object(seo_batch, "STAGING_DIR", Path(staging_dir)),
                patch.object(seo_batch, "download_image", return_value=(None, "No image URL.")),
            ):
                row = seo_batch.write_staging_folder("custom mailer boxes", data)

            webhook_source = (SCRIPT_PATH.parents[1] / "google-apps-script" / "webhook_handler.gs").read_text(
                encoding="utf-8"
            )
            header_match = re.search(r"const PRODUCT_HEADERS = (\[.*?\]);", webhook_source, re.DOTALL)
            self.assertIsNotNone(header_match)
            headers = json.loads(header_match.group(1))
            self.assertEqual(list(row), headers)

            class FakeResponse:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict[str, object]:
                    return {"ok": True}

            with patch.object(seo_batch.requests, "post", return_value=FakeResponse()) as post:
                seo_batch.post_rows("https://example.test/exec", "unit-test-token", [row])

            self.assertEqual(
                post.call_args.kwargs["json"],
                {"token": "unit-test-token", "rows": [row]},
            )


if __name__ == "__main__":
    unittest.main()
