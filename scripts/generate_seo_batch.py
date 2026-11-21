#!/usr/bin/env python3
"""Generate packaging SEO drafts and optional Google Sheets webhook rows."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
from urllib.parse import urlparse

import requests
from google import genai
from google.genai import types


ROOT = Path(__file__).resolve().parents[1]
KEYWORDS_FILE = ROOT / "keywords.txt"
STAGING_DIR = ROOT / "seo_staging"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_HTML_TAGS = {"p", "h2", "h3", "ul", "ol", "li", "strong", "em", "br"}
IMAGE_TYPES = {
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/gif": ("gif", (b"GIF87a", b"GIF89a")),
    "image/webp": ("webp", b"RIFF"),
}
FIELDS = (
    "title",
    "meta_title",
    "meta_description",
    "short_description",
    "body_html",
    "suggested_slug",
    "image_search_query",
)

SEO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "meta_title": {"type": "STRING"},
        "meta_description": {"type": "STRING"},
        "short_description": {"type": "STRING"},
        "body_html": {"type": "STRING"},
        "suggested_slug": {"type": "STRING"},
        "image_search_query": {"type": "STRING"},
        "keyword_ideas": {"type": "ARRAY", "items": {"type": "STRING"}},
        "image_url": {"type": "STRING"},
    },
    "required": [
        "title",
        "meta_title",
        "meta_description",
        "short_description",
        "body_html",
        "suggested_slug",
        "image_search_query",
        "keyword_ideas",
        "image_url",
    ],
}


class SafeHTMLParser(HTMLParser):
    """Keep a small set of formatting tags and discard all attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_HTML_TAGS:
            return
        self.parts.append(f"<{tag}>")
        if tag != "br":
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ALLOWED_HTML_TAGS:
            self.parts.append(f"<{tag}>" if tag == "br" else f"<{tag}></{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def result(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts).strip()


class PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", value).strip("-")


def read_keywords() -> list[str]:
    try:
        lines = KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Could not read {KEYWORDS_FILE}: {exc}") from exc
    keywords = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not keywords:
        raise RuntimeError(f"No keywords found in {KEYWORDS_FILE}")
    return keywords


def generate_content(client: genai.Client, keyword: str) -> dict[str, object]:
    prompt = f"""Create an accurate SEO product draft for CustomPackCo, a custom packaging store.
Use this target keyword as the starting point: {keyword}

Return JSON matching the supplied schema. Include 3-5 distinct related keyword ideas.
Write a concise natural product title, a meta title, a meta description no longer than
160 characters, a short description, and a useful body description using only p, h2,
h3, ul, ol, li, strong, em, and br HTML tags. Include a practical buyer next step.
Do not invent prices, minimum quantities, dimensions, materials, certifications,
production capabilities, shipping promises, or environmental claims. Phrase options
as items to confirm with the supplier. Use a short lowercase hyphenated slug.
Provide an image search query. Set image_url to an empty string unless you know a
direct, publicly accessible HTTPS image-file URL; never provide a search-results URL.
"""
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SEO_SCHEMA,
            temperature=0.4,
        ),
    )
    if not response.text:
        raise RuntimeError(f"Gemini returned an empty response for keyword: {keyword}")
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON for keyword {keyword}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Gemini response for {keyword} must be a JSON object")

    for field in FIELDS:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise RuntimeError(f"Gemini response is missing a valid {field!r} for {keyword}")
        data[field] = data[field].strip()
    ideas = data.get("keyword_ideas")
    if not isinstance(ideas, list) or not all(isinstance(item, str) and item.strip() for item in ideas):
        raise RuntimeError(f"Gemini response has invalid keyword_ideas for {keyword}")
    data["keyword_ideas"] = [item.strip() for item in ideas]
    if not isinstance(data.get("image_url"), str):
        raise RuntimeError(f"Gemini response has invalid image_url for {keyword}")
    data["image_url"] = data["image_url"].strip()
    data["meta_description"] = data["meta_description"][:160]

    parser = SafeHTMLParser()
    parser.feed(data["body_html"])
    parser.close()
    data["body_html"] = parser.result()
    if not data["body_html"]:
        raise RuntimeError(f"Gemini response has no usable body_html for {keyword}")
    data["suggested_slug"] = slugify(data["suggested_slug"]) or slugify(data["title"])
    if not data["suggested_slug"]:
        raise RuntimeError(f"Could not create a URL slug for keyword: {keyword}")
    return data


def verify_public_image_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
        raise ValueError("Only public HTTPS image URLs on the standard port are accepted.")
    if parsed.username or parsed.password:
        raise ValueError("Image URLs containing credentials are not accepted.")
    host = parsed.hostname
    addresses = {result[4][0] for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("The image host does not resolve exclusively to public IP addresses.")
    return url


def download_image(url: str, image_dir: Path) -> tuple[str | None, str]:
    if not url:
        return None, "No direct image URL was generated. Use the image search query to find and license a suitable product photo."
    try:
        safe_url = verify_public_image_url(url)
        with requests.get(
            safe_url,
            timeout=(5, 15),
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "CustomPackCo-SEODraft/1.0"},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in IMAGE_TYPES:
                raise ValueError(f"URL did not return a supported image content type ({content_type or 'unknown'}).")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 8 MB download limit.")
            image_data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                image_data.extend(chunk)
                if len(image_data) > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds the 8 MB download limit.")
        extension, signature = IMAGE_TYPES[content_type]
        signatures = signature if isinstance(signature, tuple) else (signature,)
        if not any(image_data.startswith(item) for item in signatures):
            raise ValueError("Image bytes do not match the reported image type.")
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"reference-image.{extension}"
        image_path.write_bytes(image_data)
        return image_path.name, f"Downloaded reference image: images/{image_path.name}"
    except (requests.RequestException, OSError, ValueError) as exc:
        return None, f"Image download skipped: {exc}"


def plain_text_from_html(body_html: str) -> str:
    parser = PlainTextParser()
    parser.feed(body_html)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def write_staging_folder(keyword: str, data: dict[str, object]) -> dict[str, object]:
    slug = str(data["suggested_slug"])
    folder = STAGING_DIR / slug
    images_dir = folder / "images"
    folder.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    keywords = [keyword, *data["keyword_ideas"]]
    (folder / "keywords.txt").write_text("\n".join(keywords) + "\n", encoding="utf-8")
    plain_description = plain_text_from_html(str(data["body_html"]))
    admin_fields = [
        f"Title: {data['title']}",
        f"Slug: {slug}",
        "Industry: ",
        "Style: ",
        "Material: ",
        "Minimum order: Confirm with supplier",
        f"Short description (short_desc): {data['short_description']}",
        "Long description (full_details; plain text for the current admin form):",
        plain_description,
        f"Meta title (meta_title): {data['meta_title']}",
        f"Meta description (meta_description): {data['meta_description']}",
        f"Image search query: {data['image_search_query']}",
        "Main image: upload a selected, licensed image in the admin panel; the local reference image is not uploaded to the site.",
        "HTML description: see body.html; the current admin form stores full_details as text.",
    ]
    (folder / "admin_fields.txt").write_text("\n".join(admin_fields) + "\n", encoding="utf-8")
    (folder / "body.html").write_text(str(data["body_html"]) + "\n", encoding="utf-8")

    image_name, image_note = download_image(str(data["image_url"]), images_dir)
    image_notes = [
        f"Search query: {data['image_search_query']}",
        f"Generated direct image URL: {data['image_url'] or 'None'}",
        image_note,
        "Confirm image ownership/license, product accuracy, and alt text before uploading to the live site.",
    ]
    (images_dir / "image_notes.txt").write_text("\n".join(image_notes) + "\n", encoding="utf-8")

    product_url = f"https://custompackco.com/product/{slug}"
    checklist = f"""# Indexing checklist: {data['title']}

Draft product URL: {product_url}

- [ ] Review the copy, slug, product specifications, images, and rights.
- [ ] Add or update the product through the admin panel; do not treat this draft as published.
- [ ] Confirm the live product URL above loads correctly and returns an indexable page.
- [ ] In Google Search Console, select the verified `custompackco.com` property.
- [ ] Use **URL inspection**, enter the live URL, and run **Test live URL**.
- [ ] If the live test succeeds, select **Request indexing**. This is a request, not a guarantee.
- [ ] Check the URL inspection result later for indexing or coverage issues.

## Internal links

- Add a contextual link from the most relevant existing packaging category page.
- Link from a related product or useful blog/guide where it genuinely helps shoppers.
- Use a descriptive, natural anchor; verify each internal link after the product is live.
- Do not add links to unpublished or nonexistent pages.
"""
    (folder / "indexing_checklist.md").write_text(checklist, encoding="utf-8")

    sheet_row = {field: data[field] for field in FIELDS}
    sheet_row.update(
        {
            "seed_keyword": keyword,
            "keyword_ideas": "; ".join(data["keyword_ideas"]),
            "image_url": data["image_url"],
            "slug": slug,
            "image_file": f"images/{image_name}" if image_name else "",
        }
    )
    return sheet_row


def post_rows(webhook_url: str, token: str, rows: list[dict[str, object]]) -> None:
    response = requests.post(
        webhook_url,
        json={"token": token, "rows": rows},
        timeout=(5, 30),
    )
    response.raise_for_status()
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("The Google Sheets webhook returned a non-JSON response.") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"Google Sheets webhook rejected the batch: {result}")


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY in the environment before running this script.")
    webhook_url = os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    webhook_token = os.environ.get("WEBHOOK_TOKEN", "").strip()
    if webhook_url and not webhook_token:
        raise RuntimeError("Set WEBHOOK_TOKEN when GOOGLE_SHEET_WEBHOOK_URL is enabled.")

    client = genai.Client(api_key=api_key)
    rows = []
    seen_slugs: set[str] = set()
    for keyword in read_keywords():
        data = generate_content(client, keyword)
        original_slug = str(data["suggested_slug"])
        if original_slug in seen_slugs:
            data["suggested_slug"] = f"{original_slug}-{slugify(keyword)}"
        slug = str(data["suggested_slug"])
        if slug in seen_slugs:
            raise RuntimeError(f"Generated duplicate staging slug {slug!r}; edit keywords.txt and retry.")
        seen_slugs.add(slug)
        rows.append(write_staging_folder(keyword, data))
        print(f"Generated local SEO draft: seo_staging/{slug}")

    if webhook_url:
        post_rows(webhook_url, webhook_token, rows)
        print(f"Posted {len(rows)} draft row(s) to Google Sheets.")
    else:
        print("GOOGLE_SHEET_WEBHOOK_URL is unset; drafts were saved locally only.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"SEO batch failed: {exc}", file=sys.stderr)
        sys.exit(1)
