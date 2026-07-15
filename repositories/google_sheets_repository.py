import logging
import os
import re
import json
from datetime import datetime

import gspread
import pytz
from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials

from config.database import get_connection
from utils.phone_utils import phones_match
from utils.sheet_utils import extract_sheet_id

logger = logging.getLogger("sheets_repo")

_gs_client = None
def get_client():
    logger.info("Initializing Google Sheets client")
    service_account_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}"))
    
    # Fix broken newlines in private key
    service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")
    
    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    logger.info("Google Sheets client initialized successfully")
    return client

def get_sheets_client():
    global _gs_client
    if _gs_client:
        return _gs_client
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    service_account_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}"))
    if not service_account_info:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
    _gs_client = gspread.authorize(creds)
    return _gs_client


#  AREA CODE MAP 

def load_area_code_map():
    logger.info("Loading area code mapping from Google Sheet")
    client  = get_client()
    sheet   = client.open_by_key("1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok").worksheet("BOT area codes (LIVE)")
    records = sheet.get_all_records()
    area_map = {}
    for row in records:
        area     = str(row.get("Area Code")).strip()
        phone_id = row.get("Phone Number ID")
        number   = row.get("Number")
        if area and phone_id:
            area_map[area] = [phone_id, number]
    logger.info(f"Loaded {len(area_map)} area mappings")
    return area_map


#  PHONE ROW FINDER 

def find_row_by_phone(sheet, phone):
    logger.info(f"Searching for phone in sheet: {phone}")
    records = sheet.get_all_records()
    for idx, r in enumerate(records, start=2):
        if phones_match(phone, r.get("VALID_PHONES", "")) or phones_match(phone, r.get("MOBILE_PHONE", "")):
            logger.info(f"Match found at row: {idx}")
            return idx
    logger.warning("No matching row found")
    return None


#  SHARED HELPERS 

def _safe(val):
    return str(val) if val is not None else ""


def _resolve_disposition(duration, analysis, call_disposition=None, voicemail_detected=False):
    """
    Single source of truth for Call Disposition.
    Priority:
      1. call_disposition passed explicitly from the webhook (most accurate)
      2. Transferred flag from analysis
      3. Voicemail flag
      4. Duration-based fallback
    """
    if call_disposition:
        return call_disposition

    transferred = str((analysis or {}).get("call_transferred", "")).lower() == "true"

    if duration <= 0:
        return "Not Answered"
    elif transferred:
        return "Transferred"
    elif voicemail_detected:
        return "Voicemail"
    else:
        return "Answered"


def _build_data_map(
    lead_info, lead_id, duration, conv_id,
    analysis, call_count, called_from, called_to,
    disposition, timestamp_str,
):
    return {
        "Timestamp":              timestamp_str,
        "Call ID":                _safe(conv_id),
        "Call Interrupted":       _safe(analysis.get("call_interrupted")      if analysis else ""),
        "Frustrated With AI":     _safe(analysis.get("frustrated_with_ai")    if analysis else ""),
        "Are they looking to sell?": _safe(analysis.get("is_looking_to_sell") if analysis else ""),
        "Is Interested?":         _safe(analysis.get("is_interested")         if analysis else ""),
        "Motivation":             _safe(analysis.get("motivation")            if analysis else ""),
        "Fair Cash Price":        _safe(analysis.get("fair_cash_price")       if analysis else ""),
        "Roadblocks":             _safe(analysis.get("roadblocks")            if analysis else ""),
        "Influencer":             _safe(analysis.get("influencer")            if analysis else ""),
        "timeline":               _safe(analysis.get("timeline")              if analysis else ""),
        "condition":              _safe(analysis.get("condition")             if analysis else ""),
        "Next Steps":             _safe(analysis.get("next_steps")            if analysis else ""),
        "Change of Mind Reason":  _safe(analysis.get("change_of_mind_reason") if analysis else ""),
        "Checkback Time":         _safe(analysis.get("checkback_time")        if analysis else ""),
        "lead_score":             _safe(analysis.get("lead_score")            if analysis else ""),
        "Wrong / DNC":            _safe(analysis.get("wrong_call")            if analysis else ""),
        "Was it Transferred":     _safe(analysis.get("call_transferred")      if analysis else ""),
        "call summary":           _safe(analysis.get("call_summary")          if analysis else ""),
        "Called From":            _safe(called_from),
        "Called To":              _safe(called_to),
        "Call Duration":          f"{duration}s",
        "Call Disposition":       disposition,
        "Call Count":             str(call_count),
        "Lead Name":              _safe(lead_info.get("Name")),
        "ACQ Manager":            _safe(lead_info.get("ACQ_Manager__c")),
        "Property Address":       _safe(
            f"{lead_info.get('Street', '')}, {lead_info.get('City', '')}, "
            f"{lead_info.get('State', '')} {lead_info.get('PostalCode', '')}"
        ),
        "Link to Profile": f"https://leftmain-4606.lightning.force.com/lightning/r/Lead/{lead_id}/view",
    }


#  LOG TO SHEETS (trigger-time placeholder row) 

def log_to_sheets(
    lead_info, lead_id, duration, conv_id,
    analysis=None, call_count=0, called_from="", called_to="",
    sheet_url=None, worksheet_name=None,
    call_disposition=None, voicemail_detected=False,
):
    """
    Appends a new placeholder row at trigger time.
    duration=0 → disposition='Not Answered' until post-call updates it.
    """
    try:
        gs_client = get_sheets_client()

        if sheet_url and worksheet_name:
            sheet_key = (
                sheet_url.split("/spreadsheets/d/")[-1].split("/")[0]
                if "/spreadsheets/d/" in sheet_url
                else sheet_url
            )
            sheet = gs_client.open_by_key(sheet_key).worksheet(worksheet_name)
            logger.info(f"log_to_sheets: opened {sheet_key} / {worksheet_name}")
        else:
            sheet = gs_client.open_by_key(
                "1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok"
            ).worksheet("Copy of Call Recording Metrics")
            logger.info("log_to_sheets: using default sheet")

        if duration > 0:
            call_count = (call_count or 0) + 1

        disposition = _resolve_disposition(duration, analysis, call_disposition, voicemail_detected)

        los_angeles_tz = pytz.timezone("America/Los_Angeles")
        timestamp_str  = datetime.now(los_angeles_tz).strftime("%Y-%m-%d %H:%M:%S PDT")

        headers  = sheet.row_values(1)
        data_map = _build_data_map(
            lead_info, lead_id, duration, conv_id,
            analysis, call_count, called_from, called_to,
            disposition, timestamp_str,
        )

        row = [data_map.get(col, "") for col in headers]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"log_to_sheets: row added for lead {lead_id} | disposition={disposition}")

    except Exception as e:
        logger.error(f"Google Sheets error (log_to_sheets): {repr(e)}")


#  UPDATE SHEET ROW (post-call webhook) 

def update_sheet_row(
    lead_info, lead_id, duration, conv_id,
    analysis=None, call_count=0, called_from="", called_to="",
    sheet_url=None, worksheet_name=None,
    call_disposition=None, voicemail_detected=False,   #  accepted from webhook
):
    """
    Finds the existing placeholder row (matched by 'Called To') and overwrites
    it with final post-call data. Never duplicates rows.
    """
    try:
        if not sheet_url or not worksheet_name:
            logger.warning("update_sheet_row: sheet_url or worksheet_name missing, skipping")
            return

        gs_client = get_sheets_client()
        sheet_key = (
            sheet_url.split("/spreadsheets/d/")[-1].split("/")[0]
            if "/spreadsheets/d/" in sheet_url
            else sheet_url
        )
        sheet = gs_client.open_by_key(sheet_key).worksheet(worksheet_name)
        logger.info(f"update_sheet_row: opened {sheet_key} / {worksheet_name}")

        if duration > 0:
            call_count = (call_count or 0) + 1

        #  Use the disposition decided by the webhook — no recalculation from duration
        disposition = _resolve_disposition(duration, analysis, call_disposition, voicemail_detected)

        los_angeles_tz = pytz.timezone("America/Los_Angeles")
        timestamp_str  = datetime.now(los_angeles_tz).strftime("%Y-%m-%d %H:%M:%S PDT")

        headers  = sheet.row_values(1)
        data_map = _build_data_map(
            lead_info, lead_id, duration, conv_id,
            analysis, call_count, called_from, called_to,
            disposition, timestamp_str,
        )

        print(f"[SHEET HEADERS] {headers}")
        print(f"[DATA MAP KEYS] {list(data_map.keys())}")
        print(f"[DATA MAP VALUES] wrong/dnc={data_map.get('Wrong / DNC')!r} | lead_score={data_map.get('lead_score')!r} | call_summary={data_map.get('Call Summary')!r}")

        #  Find the row whose "Called To" matches this call
        called_to_digits = re.sub(r"\D", "", called_to)

        try:
            called_to_col = headers.index("Called To") + 1  # 1-based
        except ValueError:
            logger.error("update_sheet_row: 'Called To' column not found in headers")
            return

        all_values  = sheet.get_all_values()
        matched_row = None

        for row_idx, row_values in enumerate(all_values[1:], start=2):  # skip header
            cell_val = row_values[called_to_col - 1] if len(row_values) >= called_to_col else ""
            if re.sub(r"\D", "", cell_val) == called_to_digits:
                matched_row = row_idx
                # keep looping — always update the LAST match (most recent call)

        if matched_row is None:
            logger.warning(
                f"update_sheet_row: no row found for called_to={called_to} "
                f"(lead {lead_id}) — appending new row as fallback"
            )
            row = [data_map.get(col, "") for col in headers]
            sheet.append_row(row, value_input_option="USER_ENTERED")
            return

        #  Overwrite matched row in one batch call 
        new_row  = [data_map.get(col, "") for col in headers]
        col_end  = len(headers)
        range_a1 = (
            f"A{matched_row}:{chr(64 + col_end)}{matched_row}"
            if col_end <= 26
            else f"A{matched_row}:AZ{matched_row}"
        )
        sheet.update(range_a1, [new_row], value_input_option="USER_ENTERED")
        logger.info(
            f"update_sheet_row: row {matched_row} updated for lead {lead_id} "
            f"| disposition={disposition} | duration={duration}s"
        )

    except Exception as e:
        logger.error(f"Google Sheets error (update_sheet_row): {repr(e)}")


#  APPEND EXTRACTED VARIABLES (post-call output sheet)

def _cell_safe(value):
    """A single Sheets cell can't hold a raw list/dict (e.g. Claude returning
    ["missed q1", "missed q2"] for a 'Missed Questions' variable) — flatten it."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return value


def append_extracted_variables(client, sheet_url: str, worksheet_name: str, data_map: dict) -> None:
    """
    Appends a new row to an output sheet, matching columns by header name
    (same convention as log_to_sheets/update_sheet_row). Unknown headers are
    left blank; data_map keys with no matching header are ignored.
    """
    try:
        sheet_key = extract_sheet_id(sheet_url)
        sheet = client.open_by_key(sheet_key).worksheet(worksheet_name)

        headers = sheet.row_values(1)
        row = [_cell_safe(data_map.get(col)) for col in headers]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"append_extracted_variables: row appended to {sheet_key}/{worksheet_name}")

    except Exception as e:
        logger.error(f"Google Sheets error (append_extracted_variables): {repr(e)}")