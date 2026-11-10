"""Invoice extraction (OpenAI vision) and appending to a sheet's 'Materials' tab."""

import base64
import json
import logging
import os
import re
from datetime import datetime

import httpx

from config.database import get_connection
from repositories.google_sheets_repository import get_client
from utils.sheet_utils import extract_sheet_id

logger = logging.getLogger("invoice_service")

INVOICE_MODEL = os.getenv("INVOICE_OPENAI_MODEL", "gpt-6-luna")
MATERIALS_TAB = "Materials"
HEADER_ROW = 5          # headers live in row 5, columns B..K
FIRST_COL, LAST_COL = "B", "K"

SYSTEM_PROMPT = """
You are an expert invoice and receipt data extraction system.

Your task is to extract structured information from uploaded
vendor invoices and purchase receipts, including:

- Retail receipts
- Fuel receipts
- Supplier invoices
- Multi-page PDF invoices
- Scanned or photographed documents

Return ONLY one valid JSON object with exactly these keys:

{
    "tag": "",
    "date": "",
    "vendor": "",
    "invoice_number": "",
    "items": "",
    "amount": null,
    "payment_method": "",
    "notes": ""
}

EXTRACTION RULES:

1. TAG:
- Extract handwritten labels, project names, locations,
  or job identifiers written by a person on the document.
- Examples: "Burbank 14", "Martin Avalon 9".
- Preserve the original wording and numbers.
- Do NOT use printed vendor names, customer names,
  job names, or purchase order information as TAG.
- If no handwritten label exists, return "".

2. DATE:
- Extract the actual transaction date for receipts.
- Extract the invoice date for supplier invoices.
- Do NOT confuse invoice date with order date,
  due date, delivery date, or payment date.
- Return dates in MM/DD/YYYY format.
- Convert two-digit years to four-digit years.
- If the correct date cannot be determined, return "".

3. VENDOR:
- Extract the actual vendor or store name.
- Prefer the business name printed on the invoice.
- Examples: "Vons", "Chevron Stations Inc",
  "Superior Pool Products, LLC".
- Do not use the customer's name as the vendor.

4. INVOICE_NUMBER:
- Extract the explicit invoice number or receipt number.
- Prefer a number explicitly labeled INVOICE #,
  RECEIPT #, or equivalent.
- If a document has both an invoice number and
  transaction/order number, prefer the invoice number.
- Do not mistake barcode numbers, authorization codes,
  card numbers, or purchase order numbers for invoice numbers.
- If no identifiable invoice or receipt number exists,
  return "".

5. ITEMS:
- Extract the purchased products, materials, or services.
- Return a concise comma-separated summary.
- Include meaningful product names and services.
- For receipts with multiple products, summarize the
  purchased products without prices.
- For supplier invoices, summarize the main materials
  and services rather than reproducing every line item.
- Include delivery fees, pallet charges, and other
  purchased services when applicable.
- Do not include taxes, payment information, or totals.
- Do not invent missing products.

6. AMOUNT:
- Extract the FINAL TOTAL amount paid or owed.
- For retail receipts, use the final amount paid.
- For supplier invoices, use the final invoice total,
  including applicable taxes, fees, and charges.
- Do NOT use merchandise subtotal, pre-tax subtotal,
  individual item prices, or discounts as the final amount.
- For invoices with multiple pages, inspect all pages
  and locate the final invoice total.
- Ignore promotional offers, savings, reward points,
  and unrelated amounts.
- Return a plain numeric value without currency symbols.
- Example: 2179.62
- If the final amount cannot be determined, return null.

7. PAYMENT_METHOD:
- Extract the actual payment method when explicitly shown.
- Examples:
  "American Express 3068"
  "Visa 1234"
  "Cash"
  "Check"
- Include the card type and last four digits when available.
- Do not return full card numbers.
- Do NOT confuse payment terms such as NET 30,
  NET 15, or NET 60 with actual payment methods.
- If the invoice does not show an actual payment method,
  return "".

8. NOTES:
- Extract important additional information not captured
  by the other fields.
- Examples include:
  - Purchase order numbers
  - Job references
  - Special delivery charges
  - Important invoice remarks
- Do not duplicate information already captured.
- If no relevant additional information exists,
  return "".

GENERAL RULES:

- Inspect the entire uploaded document, including all
  pages of multi-page PDFs.
- Pay attention to handwritten annotations.
- Distinguish printed information from handwritten notes.
- Never assume that similar-looking numbers represent
  the same type of information.
- Never calculate or invent missing values.
- If information is unclear, use an empty string or null
  according to the field definition.
- Ignore advertisements, promotional content, coupons,
  loyalty points, and unrelated text.
- Extract information only from the uploaded document.

IMPORTANT:
Return ONLY valid JSON.
Do not include Markdown, explanations, or additional text.
"""

def list_invoice_sheets() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, spreadsheet_id, name, url FROM invoice_sheets ORDER BY LOWER(name)"
        ).fetchall()
    return [dict(r) for r in rows]


def get_invoice_sheet(row_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, spreadsheet_id, name, url FROM invoice_sheets WHERE id=%s", (row_id,)
        ).fetchone()
    return dict(row) if row else None


def add_invoice_sheet(url: str) -> dict:
    """Validate a Google Sheet URL (access + 'Materials' tab) and store it."""
    sid = extract_sheet_id(url or "")
    if not sid:
        raise ValueError("That doesn't look like a Google Sheets URL")
    try:
        sh = get_client().open_by_key(sid)
    except Exception as exc:
        raise ValueError(
            "Can't open that sheet. Share it with the service account as Editor first."
        ) from exc
    if MATERIALS_TAB not in [w.title for w in sh.worksheets()]:
        raise ValueError(f"'{sh.title}' has no '{MATERIALS_TAB}' tab")
    clean_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM invoice_sheets WHERE spreadsheet_id=%s", (sid,)).fetchone():
            raise ValueError(f"'{sh.title}' is already in the list")
        row = conn.execute(
            "INSERT INTO invoice_sheets (spreadsheet_id, name, url) VALUES (%s,%s,%s) "
            "RETURNING id, spreadsheet_id, name, url",
            (sid, sh.title, clean_url),
        ).fetchone()
        conn.commit()
    return dict(row)


def delete_invoice_sheet(row_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM invoice_sheets WHERE id=%s", (row_id,))
        conn.commit()
        return cur.rowcount > 0


def extract_invoice(file_bytes: bytes, content_type: str, filename: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    b64 = base64.b64encode(file_bytes).decode()
    if content_type == "application/pdf":
        part = {"type": "file", "file": {"filename": filename or "invoice.pdf",
                                         "file_data": f"data:application/pdf;base64,{b64}"}}
    else:
        part = {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}}

    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": INVOICE_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": "Extract this invoice."}, part]},
            ],
        },
        timeout=180,
    )
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", {}).get("message", resp.reason_phrase)
        except ValueError:
            msg = resp.reason_phrase
        logger.error("OpenAI invoice error %s: %s", resp.status_code, msg)
        raise RuntimeError(f"OpenAI returned HTTP {resp.status_code}: {msg}")
    content = resp.json()["choices"][0]["message"].get("content") or "{}"
    return json.loads(content)


def _to_number(v):
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        return v
    cleaned = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(cleaned)
    except ValueError:
        return str(v)


def append_to_materials(sheet_id: str, data: dict, entered_by: str) -> dict:
    """Write one row into the first empty row (B..K) under the row-5 headers."""
    sh = get_client().open_by_key(sheet_id)
    try:
        ws = sh.worksheet(MATERIALS_TAB)
    except Exception as exc:
        raise ValueError(f"No '{MATERIALS_TAB}' tab found in '{sh.title}'") from exc

    # Existing rows under the header, limited to the B..K block (other tables sit to the right).
    existing = ws.get(f"{FIRST_COL}{HEADER_ROW + 1}:{LAST_COL}{ws.row_count}")
    next_row = HEADER_ROW + 1
    for i, row in enumerate(existing):
        if any(str(c).strip() for c in row):
            next_row = HEADER_ROW + 1 + i + 1
    if next_row > ws.row_count:
        ws.add_rows(50)

    # B TAG, C DATE, D VENDOR, E INVOICE #, F ITEMS, G AMT, H PAYMENT, I NOTES, J SPLIT, K ENTERED BY
    row = [
        data.get("tag") or "",
        data.get("date") or datetime.now().strftime("%m/%d/%Y"),
        data.get("vendor") or "",
        data.get("invoice_number") or "",
        data.get("items") or "",
        _to_number(data.get("amount")),
        data.get("payment_method") or "",
        data.get("notes") or "",
        "",
        entered_by,
    ]
    rng = f"{FIRST_COL}{next_row}:{LAST_COL}{next_row}"
    ws.update(range_name=rng, values=[row], value_input_option="USER_ENTERED")
    return {"sheet": sh.title, "row": next_row, "range": rng, "values": row}
