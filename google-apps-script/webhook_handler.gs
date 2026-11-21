const PRODUCT_HEADERS = [
  "title",
  "meta_title",
  "meta_description",
  "short_description",
  "body_html",
  "suggested_slug",
  "image_search_query",
  "seed_keyword",
  "keyword_ideas",
  "image_url",
  "slug",
  "image_file"
];

function doPost(e) {
  try {
    const payload = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    const expectedToken = PropertiesService.getScriptProperties().getProperty("WEBHOOK_TOKEN");
    if (!expectedToken || !payload.token || !tokensMatch(String(payload.token), expectedToken)) {
      return jsonResponse({ ok: false, error: "Unauthorized" });
    }

    const rows = Array.isArray(payload.rows) ? payload.rows : [payload];
    if (rows.length === 0 || !rows.every(isProductRow)) {
      return jsonResponse({ ok: false, error: "Expected one or more valid product rows" });
    }

    const lock = LockService.getScriptLock();
    lock.waitLock(30000);
    try {
      const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
      if (!spreadsheet) {
        throw new Error("Bind this script to the destination spreadsheet before deployment.");
      }
      let sheet = spreadsheet.getSheetByName("Products");
      if (!sheet) {
        sheet = spreadsheet.insertSheet("Products");
      }

      if (sheet.getLastRow() === 0) {
        sheet.appendRow(PRODUCT_HEADERS);
      } else {
        const headers = sheet.getRange(1, 1, 1, PRODUCT_HEADERS.length).getValues()[0];
        if (PRODUCT_HEADERS.some((header, index) => headers[index] !== header)) {
          throw new Error("The Products sheet header does not match the expected columns.");
        }
      }

      const values = rows.map(function (row) {
        return PRODUCT_HEADERS.map(function (header) {
          return safeCellValue(row[header]);
        });
      });
      sheet.getRange(sheet.getLastRow() + 1, 1, values.length, PRODUCT_HEADERS.length).setValues(values);
    } finally {
      lock.releaseLock();
    }
    return jsonResponse({ ok: true, appended: rows.length });
  } catch (error) {
    console.error(error);
    return jsonResponse({ ok: false, error: String(error.message || error) });
  }
}

function isProductRow(row) {
  return row && typeof row === "object" && PRODUCT_HEADERS.every(function (header) {
    return Object.prototype.hasOwnProperty.call(row, header);
  });
}

function safeCellValue(value) {
  const text = Array.isArray(value) ? value.join(", ") : String(value == null ? "" : value);
  return /^[\s]*[=+\-@]/.test(text) ? "'" + text : text;
}

function tokensMatch(actual, expected) {
  if (actual.length !== expected.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < actual.length; index += 1) {
    difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return difference === 0;
}

function jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
