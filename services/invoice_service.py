"""Invoice extraction (OpenAI vision) and appending to a sheet's 'Materials' tab."""

import base64
import json
import logging
import os
import re
from datetime import datetime

import httpx

from repositories.google_sheets_repository import get_client

logger = logging.getLogger("invoice_service")

INVOICE_MODEL = os.getenv("INVOICE_OPENAI_MODEL", "gpt-6.1-terra")
MATERIALS_TAB = "Materials"
HEADER_ROW = 5          # headers live in row 5, columns B..K
FIRST_COL, LAST_COL = "B", "K"
# Invoice sheets selectable in the UI (comma separated spreadsheet ids).
DEFAULT_SHEET_IDS = "1Ke0CHxoSZIW0do-HoIxCb2gYXCEQRw5A3Df9DfS5mYE"

SYSTEM_PROMPT = """You extract data from a vendor invoice or receipt (image or PDF).
Return ONLY a JSON object with these keys:
- "tag": any HANDWRITTEN label/note written on the paper by a person (e.g. a store/job name and number such as "Burbank 14"), copied exactly; "" if none. Do not use the printed store name here.
- "date": invoice/receipt date as MM/DD/YYYY, or "" if not found
- "vendor": vendor / store name
- "invoice_number": receipt or invoice number, or ""
- "items": concise summary of the line items purchased (comma separated, no prices)
- "amount": the final total amount paid/due as a plain number (no currency symbol), or null
- "payment_method": e.g. "Visa 1234", "Cash", "Check", "Net 30", or ""
- "notes": anything important not captured above (e.g. PO/job number), or ""
Never invent values; use "" or null when unsure."""


def configured_sheet_ids() -> list[str]:
    raw = os.getenv("INVOICE_SHEET_IDS", DEFAULT_SHEET_IDS)
    return [s.strip() for s in raw.split(",") if s.strip()]


_title_cache: dict[str, str] = {}


def list_invoice_sheets() -> list[dict]:
    client = None
    out = []
    for sid in configured_sheet_ids():
        title = _title_cache.get(sid)
        if not title:
            try:
                client = client or get_client()
                title = client.open_by_key(sid).title
                _title_cache[sid] = title
            except Exception as exc:  # no access yet: still list it by id
                logger.warning("Could not read title for %s: %s", sid, exc)
                title = sid
        out.append({"id": sid, "name": title})
    return out


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
