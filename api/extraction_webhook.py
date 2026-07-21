import logging
from datetime import datetime

import pytz
from fastapi import APIRouter, Request

from config.database import get_connection, get_prompt_text_by_id
from repositories.google_sheets_repository import get_client, append_extracted_variables
from services.variable_extraction_service import extract_variables

logger = logging.getLogger(__name__)
router = APIRouter()


def _direct_fields(payload: dict, transcript_text: str, event_timestamp: int | None) -> dict:
    """
    Fields passed straight through from the ElevenLabs payload — no LLM involved.
    record_id/call_from/lead_owner are best-effort: they're only populated if the
    calling job threaded them through as dynamic_variables when the call was placed
    (currently 'address' is set by alab_sheets_bot.py, and ElevenLabs' own
    'system__called_number' is set natively — everything else will come through
    blank). call_from/call_to are also null for non-telephony calls (e.g. web
    widget / react_sdk test calls have no phone number involved).
    """
    custom_vars = (
        payload.get("conversation_initiation_client_data", {}).get("dynamic_variables", {})
    )
    metadata = payload.get("metadata", {}) or {}

    pacific_time = ""
    if event_timestamp:
        dt = datetime.utcfromtimestamp(event_timestamp)
        pacific = pytz.timezone("America/Los_Angeles")
        pacific_time = dt.replace(tzinfo=pytz.utc).astimezone(pacific).strftime("%m/%d/%Y %H:%M:%S")

    return {
        "Call ID": payload.get("conversation_id") or "",
        "Record ID": custom_vars.get("record_id") or custom_vars.get("lead_id") or custom_vars.get("contact_id") or "",
        "Call From": custom_vars.get("system__caller_id") or custom_vars.get("call_from") or "",
        "Call To": custom_vars.get("system__called_number") or "",
        "Lead Owner": custom_vars.get("lead_owner") or "",
        "Timestamp": pacific_time,
        "Duration": metadata.get("call_duration_secs", ""),
        "Transcript": transcript_text,
    }


def _find_extraction_job(agent_id: str) -> dict | None:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sheets
            WHERE agent_id=%s
              AND output_sheet_url IS NOT NULL
              AND output_worksheet_name IS NOT NULL
              AND variables_to_record IS NOT NULL
              AND extraction_prompt_id IS NOT NULL
            ORDER BY id DESC
            """,
            (agent_id,),
        ).fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            f"agent_id={agent_id} matches {len(rows)} extraction-configured jobs; using most recent (id={rows[0]['id']})"
        )
    return dict(rows[0])


@router.post("/post-call")
async def extraction_post_call(request: Request):
    try:
        data = await request.json()
        payload = data.get("data", {})

        agent_id = payload.get("agent_id")
        if not agent_id:
            logger.warning("extraction_post_call: missing agent_id in payload")
            return {"message": "Missing agent_id"}

        duration = int((payload.get("metadata") or {}).get("call_duration_secs", 0) or 0)
        if duration <= 0:
            logger.info(f"extraction_post_call: skipping — no answered call | agent_id={agent_id}")
            return {"message": "No answered call — skipping extraction"}

        sheet_db = _find_extraction_job(agent_id)
        if not sheet_db:
            logger.info(f"extraction_post_call: no extraction-configured job for agent_id={agent_id}")
            return {"message": "No extraction config for this agent"}

        prompt_text = get_prompt_text_by_id(sheet_db["extraction_prompt_id"])
        if not prompt_text:
            logger.warning(f"extraction_post_call: prompt {sheet_db['extraction_prompt_id']} not found")
            return {"message": "Prompt not found"}

        var_names = [v.strip() for v in sheet_db["variables_to_record"].split(",") if v.strip()]
        if not var_names:
            logger.warning(f"extraction_post_call: no variables configured for sheet_id={sheet_db['id']}")
            return {"message": "No variables configured"}

        transcript_text = "\n".join(
            f"{m.get('role', '').capitalize()}: {m.get('message', '')}"
            for m in payload.get("transcript", []) if m.get("message")
        )
        summary = payload.get("analysis", {}).get("transcript_summary") or ""

        extracted = extract_variables(
            prompt_text, transcript_text, summary, var_names, sheet_db.get("variable_descriptions")
        )
        if not extracted:
            logger.warning(f"extraction_post_call: extraction returned nothing | sheet_id={sheet_db['id']}")
            return {"message": "Extraction failed"}

        data_map = {
            **_direct_fields(payload, transcript_text, data.get("event_timestamp")),
            **extracted,
        }

        client = get_client()
        append_extracted_variables(client, sheet_db["output_sheet_url"], sheet_db["output_worksheet_name"], data_map)

        logger.info(
            f"extraction_post_call: row appended | sheet_id={sheet_db['id']} conv_id={payload.get('conversation_id')}"
        )
        return {"status": "ok", "sheet_id": sheet_db["id"], "extracted": extracted}

    except Exception as e:
        logger.error(f"extraction_post_call error: {e}", exc_info=True)
        return {"error": str(e)}
