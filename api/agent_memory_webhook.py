import os
import json
import logging
import re
from typing import Any

import gspread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from services.llm_service import complete_text
from google.oauth2.service_account import Credentials

logger = logging.getLogger("app")

router = APIRouter()

SPREADSHEET_ID    = "1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok"
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

SHEET_LMI  = "LMI Data Extraction"
SHEET_LMD  = "LMD Data Extraction"
SHEET_LMCC = "LMCC Data Extraction"

PHONE_COL = {
    SHEET_LMI:  "phone_to",
    SHEET_LMD:  "Called To",
    SHEET_LMCC: "Called To",
}

def _get_gspread_client() -> gspread.Client:
    if not GOOGLE_CREDS_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var is not set.")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def _normalize_phone(phone: str) -> str:
    """Strip all non-digit characters for loose matching."""
    return re.sub(r"\D", "", str(phone))


def fetch_rows_for_phone(phone_number: str) -> dict[str, list[dict[str, Any]]]:
    """
    Search all 3 sheets and return every row where the phone column
    matches `phone_number` (digits-only comparison).
    """
    client = _get_gspread_client()
    wb     = client.open_by_key(SPREADSHEET_ID)
    target = _normalize_phone(phone_number)
    results: dict[str, list[dict[str, Any]]] = {}

    for sheet_name, phone_col in PHONE_COL.items():
        try:
            ws      = wb.worksheet(sheet_name)
            records = ws.get_all_records()
        except gspread.exceptions.WorksheetNotFound:
            logger.warning("agent_memory: sheet not found — %s", sheet_name)
            continue
        except Exception as exc:
            logger.error("agent_memory: error reading sheet %s — %s", sheet_name, exc)
            continue

        matched = [
            row for row in records
            if _normalize_phone(row.get(phone_col, "")) == target
        ]
        if matched:
            results[sheet_name] = matched
            logger.info(
                "agent_memory: sheet '%s' — %d row(s) matched for %s",
                sheet_name, len(matched), phone_number,
            )
        else:
            logger.info("agent_memory: sheet '%s' — no match for %s", sheet_name, phone_number)

    return results


#  Claude summarizer 
SYSTEM_PROMPT = """You are a real-estate acquisitions assistant AI helping a calling agent recall a lead's full history before a call.

You receive raw CRM data pulled from multiple call-tracking sheets for a specific phone number.
Your job is to write a concise, structured MEMORY BRIEF the calling agent can rely on at the very start of the conversation.

Rules:
- Write in plain English, addressing the agent directly ("The lead's name is...", "In the last call...")
- Cover: lead identity, property details, motivation to sell, objections/roadblocks, previous call outcomes, next steps agreed, and any important flags (frustrated with AI, call interrupted, change of mind, not interested, checkback time, etc.)
- 150-300 words maximum — be concise but complete
- Only use information that is explicitly present in the data. Do NOT invent or assume anything.
- If a field is empty, null, or clearly irrelevant (raw internal IDs, etc.), omit it
- If absolutely no data exists, respond with exactly: "No prior interaction history found for this number."
"""

def build_memory_brief(sheet_data: dict[str, list[dict[str, Any]]], phone_number: str) -> str:
    """Pass all matched sheet rows to Claude and return a memory brief string."""
    if not sheet_data:
        return "No prior interaction history found for this number."

    # Build a readable data block for Claude
    data_block = f"Phone Number: {phone_number}\n\n"
    for sheet_name, rows in sheet_data.items():
        data_block += f"=== {sheet_name} ({len(rows)} record(s)) ===\n"
        for i, row in enumerate(rows, 1):
            data_block += f"\n--- Record {i} ---\n"
            for col, val in row.items():
                if val not in ("", None, 0):
                    data_block += f"  {col}: {val}\n"
        data_block += "\n"

    return complete_text(SYSTEM_PROMPT, [
            {
                "role": "user",
                "content": (
                    "Here is the CRM data for the incoming caller. "
                    "Please write the memory brief for the agent:\n\n"
                    + data_block
                ),
            }
        ], 600)


#  Route 
@router.post("/fetch-client-memory")
async def fetch_client_memory(request: Request):
    try:
        body = await request.json()
    except Exception:
        logger.warning("agent_memory: could not parse JSON body")
        return JSONResponse(
            {"dynamic_variables": {"client_memory": "No prior interaction history found for this number."}}
        )

    logger.info("agent_memory: payload received — %s", json.dumps(body, default=str))

    # ElevenLabs wraps caller vars inside "parameters"; fall back to root-level keys
    params = body.get("parameters", body)
    phone_number = (
        params.get("called_number")
        or params.get("phone_to")
        or params.get("to_number")
        or params.get("phone")
        or params.get("caller_id")
        or ""
    ).strip()

    if not phone_number:
        logger.warning("agent_memory: no phone number found in payload")
        return JSONResponse(
            {"dynamic_variables": {"client_memory": "No prior interaction history found for this number."}}
        )

    # Fetch from all 3 sheets
    try:
        sheet_data = fetch_rows_for_phone(phone_number)
    except Exception as exc:
        logger.error("agent_memory: Google Sheets fetch failed — %s", exc)
        return JSONResponse(
            {"dynamic_variables": {"client_memory": "Unable to retrieve lead history at this time."}}
        )

    # Summarize with Claude
    try:
        memory_brief = build_memory_brief(sheet_data, phone_number)
    except Exception as exc:
        logger.error("agent_memory: Claude summarization failed — %s", exc)
        memory_brief = "Unable to summarize lead history at this time."

    logger.info("agent_memory: brief ready (%d chars) for %s", len(memory_brief), phone_number)

    return JSONResponse({"dynamic_variables": {"client_memory": memory_brief}})