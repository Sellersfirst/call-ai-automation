"""
Standalone script — extracts GHL opportunities from pipeline 'Leads', stage
'Discovery', filtered to Lead Source == 'TCPA Website', and appends them to
the 'Discovery' worksheet so the existing active calling job (sheets.id=6)
picks them up.

Does not import or modify any existing api/service/repository logic except
importing shared, already-tested helpers (get_client, append_extracted_variables-
style header matching) for the Google Sheets write.

Run: python3 scripts/ghl_discovery_sync.py
Requires GHL_API_KEY and GHL_LOCATION_ID in .env.
"""
import logging
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.config  # noqa: triggers load_dotenv()
from repositories.google_sheets_repository import get_client
from utils.phone_utils import digits_only

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ghl_discovery_sync")

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"

PIPELINE_NAME = "Leads"
STAGE_NAME = "Discovery"
LEAD_SOURCE_FILTER = "TCPA Website"

OUTPUT_SHEET_ID = "1lY2tpctUnMc7D2Js7BBUytUWqg-Kt1cvk3TT6j_-oUU"
OUTPUT_WORKSHEET_NAME = "Discovery"

OUTPUT_COLUMNS = [
    "FIRST_NAME", "LAST_NAME", "MOBILE_PHONE", "MOBILE_PHONE_DNC",
    "PERSONAL_ADDRESS", "PERSONAL_PHONE", "PERSONAL_PHONE_DNC",
    "SKIPTRACE_ADDRESS", "SKIPTRACE_B2B_PHONE", "VALID_PHONES", "Lead Source",
]


def _ghl_headers() -> dict:
    api_key = os.getenv("GHL_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GHL_API_KEY in .env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": GHL_API_VERSION,
        "Accept": "application/json",
    }


def _ghl_get(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    """GET with basic 429 retry/backoff — GHL's v2 API rate-limits bursts."""
    for attempt in range(5):
        res = client.get(url, params=params, timeout=30)
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", 2 * (attempt + 1)))
            logger.warning(f"Rate limited, retrying in {wait}s")
            time.sleep(wait)
            continue
        res.raise_for_status()
        return res.json()
    raise RuntimeError(f"Gave up after repeated 429s: {url}")


def _resolve_pipeline_and_stage(client: httpx.Client, location_id: str) -> tuple[str, str]:
    data = _ghl_get(client, f"{GHL_API_BASE}/opportunities/pipelines", params={"locationId": location_id})
    for pipeline in data.get("pipelines", []):
        if pipeline["name"].strip().lower() == PIPELINE_NAME.lower():
            for stage in pipeline.get("stages", []):
                if stage["name"].strip().lower() == STAGE_NAME.lower():
                    return pipeline["id"], stage["id"]
            available = [s["name"] for s in pipeline.get("stages", [])]
            raise RuntimeError(f"Stage {STAGE_NAME!r} not found in pipeline {PIPELINE_NAME!r}. Available: {available}")
    available = [p["name"] for p in data.get("pipelines", [])]
    raise RuntimeError(f"Pipeline {PIPELINE_NAME!r} not found. Available: {available}")


def _fetch_opportunities(client: httpx.Client, location_id: str, pipeline_id: str, stage_id: str) -> list[dict]:
    opportunities = []
    url = f"{GHL_API_BASE}/opportunities/search"
    params = {
        "location_id": location_id,
        "pipeline_id": pipeline_id,
        "pipeline_stage_id": stage_id,
        "limit": 100,
    }

    while True:
        data = _ghl_get(client, url, params=params)
        opportunities.extend(data.get("opportunities", []))
        next_url = (data.get("meta") or {}).get("nextPageUrl")
        if not next_url:
            break
        url, params = next_url, None  # nextPageUrl already has all query params baked in

    return opportunities


def _fetch_contact(client: httpx.Client, contact_id: str) -> dict | None:
    try:
        data = _ghl_get(client, f"{GHL_API_BASE}/contacts/{contact_id}")
        return data.get("contact")
    except Exception as exc:
        logger.warning(f"Failed to fetch contact {contact_id}: {exc}")
        return None


def _format_phone(raw: str) -> tuple[str, str]:
    """Matches the existing sheet convention: '(760) 265-3345' + '17602653345'."""
    digits = digits_only(raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return "", digits
    d10 = digits[1:]
    formatted = f"({d10[0:3]}) {d10[3:6]}-{d10[6:10]}"
    return formatted, digits


def _existing_phone_digits(sheet) -> set:
    records = sheet.get_all_records()
    return {digits_only(r.get("VALID_PHONES", "")) for r in records if digits_only(r.get("VALID_PHONES", ""))}


def sync():
    location_id = os.getenv("GHL_LOCATION_ID")
    if not location_id:
        raise RuntimeError("Missing GHL_LOCATION_ID in .env")

    headers = _ghl_headers()

    with httpx.Client(headers=headers) as ghl_client:
        pipeline_id, stage_id = _resolve_pipeline_and_stage(ghl_client, location_id)
        logger.info(f"Resolved pipeline={pipeline_id} stage={stage_id}")

        opportunities = _fetch_opportunities(ghl_client, location_id, pipeline_id, stage_id)
        logger.info(f"Fetched {len(opportunities)} opportunities in {PIPELINE_NAME}/{STAGE_NAME}")

        matching = [o for o in opportunities if (o.get("source") or "").strip() == LEAD_SOURCE_FILTER]
        logger.info(f"{len(matching)} match Lead Source == {LEAD_SOURCE_FILTER!r}")

        gs_client = get_client()
        sheet = gs_client.open_by_key(OUTPUT_SHEET_ID).worksheet(OUTPUT_WORKSHEET_NAME)
        headers_row = sheet.row_values(1)
        existing_phones = _existing_phone_digits(sheet)
        logger.info(f"{len(existing_phones)} phone numbers already present in '{OUTPUT_WORKSHEET_NAME}'")

        new_rows = []
        skipped_dupe = 0
        skipped_no_phone = 0

        for opp in matching:
            contact_id = opp.get("contactId")
            contact = _fetch_contact(ghl_client, contact_id) if contact_id else None
            if not contact:
                skipped_no_phone += 1
                continue

            formatted, digits = _format_phone(contact.get("phone", ""))
            if not digits:
                skipped_no_phone += 1
                continue
            if digits in existing_phones:
                skipped_dupe += 1
                continue
            existing_phones.add(digits)

            data_map = {
                "FIRST_NAME": contact.get("firstName") or "",
                "LAST_NAME": contact.get("lastName") or "",
                "MOBILE_PHONE": formatted,
                "MOBILE_PHONE_DNC": digits,
                "PERSONAL_ADDRESS": "",
                "PERSONAL_PHONE": formatted,
                "PERSONAL_PHONE_DNC": digits,
                "SKIPTRACE_ADDRESS": "",
                "SKIPTRACE_B2B_PHONE": "",
                "VALID_PHONES": formatted,
                "Lead Source": opp.get("source") or "",
            }
            new_rows.append([data_map.get(col, "") for col in headers_row])

        if new_rows:
            sheet.append_rows(new_rows, value_input_option="USER_ENTERED")

        logger.info(
            f"Done | appended={len(new_rows)} | skipped_duplicate={skipped_dupe} | "
            f"skipped_no_phone={skipped_no_phone} | total_candidates={len(matching)}"
        )


if __name__ == "__main__":
    sync()
