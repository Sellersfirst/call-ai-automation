from __future__ import annotations
import io
import json
import logging
import os
import re
import resend
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
import anthropic
import gspread
import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from faster_whisper import WhisperModel
from google.oauth2.service_account import Credentials
from clients.client import get_client
from config.config import SF_INSTANCE_URL
from config.database import (
    get_connection,
    add_conversation_message,
    get_recent_conversation_messages,
    get_active_prompt_text,
    get_active_prompt_with_id,
    CONVERSATION_HISTORY_LIMIT,
)
from services.salesforce_service import get_sf_access_token
from utils.retry import safe_request
from config.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/smrt", tags=["smrt"])

_EMAIL_RECIPIENTS = ["connorg@sellersfirstre.com", "blakef@sellersfirstre.com"]
_EMAIL_SUBJECT = "call rubrics"

_SMRT_SHEET_ID        = "1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok"
_SMRT_WORKSHEET_NAME  = "Scoring Rubrics"

_call_store: dict[str, dict[str, Any]] = {}


def _get_or_create(call_id: str) -> dict[str, Any]:
    if call_id not in _call_store:
        _call_store[call_id] = {
            "call_id": call_id,
            "completed": False,
            "transcript_text": None,
            "audio_url": None,
            "summary_text": None,
            "keywords": [],
            "call_from": None,
            "call_to": None,
            "timestamp": None,
            "status": None,
            "caller_id_name": None,
            "user_name": None,
            "contact_name": None,
            "call_notes": None,
            "call_outcome": None,
            "smrt_phone_call_id": None,
            "device": None,
            "event": None,
            "processed": False,
            # new fields
            "record_id": None,
            "call_link": None,
            "lead_owner": None,
            "opportunity_owner": None,
            "duration": None,
        }
    return _call_store[call_id]


_WHISPER_MODEL: WhisperModel | None = None
_WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small.en")
_WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
_WHISPER_TTL = 3600  # seconds of inactivity before unloading
_WHISPER_LOCK = threading.Lock()
_WHISPER_EVICTION_TIMER: threading.Timer | None = None


def _evict_whisper_model() -> None:
    global _WHISPER_MODEL, _WHISPER_EVICTION_TIMER
    with _WHISPER_LOCK:
        _WHISPER_MODEL = None
        _WHISPER_EVICTION_TIMER = None
    logger.info("Whisper model unloaded after %ds of inactivity", _WHISPER_TTL)


def _get_whisper_model() -> WhisperModel:
    global _WHISPER_MODEL, _WHISPER_EVICTION_TIMER
    with _WHISPER_LOCK:
        timer_was_running = _WHISPER_EVICTION_TIMER is not None
        if timer_was_running:
            _WHISPER_EVICTION_TIMER.cancel()

        cold_start = _WHISPER_MODEL is None
        if cold_start:
            logger.info(
                "Whisper model COLD START — loading %s on %s",
                _WHISPER_MODEL_SIZE, _WHISPER_DEVICE,
            )
            _WHISPER_MODEL = WhisperModel(_WHISPER_MODEL_SIZE, device=_WHISPER_DEVICE)
            logger.info("Whisper model loaded successfully (cold start complete)")
        else:
            logger.info(
                "Whisper model WARM HIT — already in cache, eviction timer reset to %ds",
                _WHISPER_TTL,
            )

        _WHISPER_EVICTION_TIMER = threading.Timer(_WHISPER_TTL, _evict_whisper_model)
        _WHISPER_EVICTION_TIMER.daemon = True
        _WHISPER_EVICTION_TIMER.start()

    return _WHISPER_MODEL


@router.post("/call-ended")
async def smrt_call_ended(request: Request, background_tasks: BackgroundTasks):
    """
    Single endpoint that receives all SMRT Studio webhook events.
    The platform sends multiple payloads per call (one per event type).
    """
    try:
        payload: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type: str = payload.get("event") or payload.get("type") or ""
    call_id: str = (
        payload.get("callId")
        or payload.get("call_id")
        or payload.get("id")
        or "unknown"
    )

    logger.info("SMRT event=%s call_id=%s", event_type, call_id)
    # log full payload at DEBUG level to see every field SMRT sends (helps map duration/call_link)
    logger.debug("SMRT full payload call_id=%s: %s", call_id, json.dumps(payload))

    record = _get_or_create(call_id)

    record["event"] = event_type
    record["caller_id_name"] = record["caller_id_name"] or payload.get("callerIdName")
    record["user_name"] = record["user_name"] or payload.get("userName")
    record["contact_name"] = record["contact_name"] or payload.get("contactName")
    record["call_notes"] = record["call_notes"] or payload.get("callNotes")
    record["call_outcome"] = record["call_outcome"] or payload.get("callOutcome")
    record["smrt_phone_call_id"] = record["smrt_phone_call_id"] or payload.get("smrtPhoneCallId")
    record["device"] = record["device"] or payload.get("device")
    record["audio_url"] = record["audio_url"] or payload.get("recordingUrl") or payload.get("audioUrl")
    record["timestamp"] = record["timestamp"] or payload.get("date") or payload.get("timestamp") or payload.get("endedAt")
    # populate new fields from payload
    record["record_id"] = record["record_id"] or payload.get("recordId") or payload.get("record_id")
    record["call_link"] = record["call_link"] or payload.get("callLink") or payload.get("call_link")
    record["lead_owner"] = record["lead_owner"] or payload.get("leadOwner") or payload.get("lead_owner")
    record["opportunity_owner"] = record["opportunity_owner"] or payload.get("opportunityOwner") or payload.get("opportunity_owner")
    record["duration"] = record["duration"] or payload.get("duration") or payload.get("callDuration")

    if "status" in event_type.lower() or event_type == "call_status_updated":
        record["status"] = payload.get("status") or payload.get("callStatus")
        logger.info("Status update for %s → %s", call_id, record["status"])

    elif "complet" in event_type.lower() or event_type == "call_completed":
        record["completed"] = True
        # renamed: caller → call_from, receiver → call_to
        record["call_from"] = record["call_from"] or payload.get("caller") or payload.get("from")
        record["call_to"] = record["call_to"] or payload.get("receiver") or payload.get("to")
        logger.info("Marking completed for %s", call_id)

    elif "transcript" in event_type.lower():
        record["transcript_text"] = (
            payload.get("transcript")
            or payload.get("transcriptText")
            or payload.get("text")
        )
        record["call_from"] = record["call_from"] or payload.get("caller")
        record["call_to"] = record["call_to"] or payload.get("receiver")

    elif "summary" in event_type.lower():
        record["summary_text"] = payload.get("summary") or payload.get("text")

    elif "keyword" in event_type.lower():
        record["keywords"] = payload.get("keywords") or []

    logger.info("processed: %s, completed: %s, transcript_text: %s, audio_url: %s", record["processed"], record["completed"], bool(record["transcript_text"]), record["audio_url"])

    ready = (
        not record["processed"]
        and record["completed"]
        and (record["transcript_text"] or record["audio_url"])
    )

    if ready:
        record["processed"] = True
        background_tasks.add_task(_run_pipeline, dict(record))

    return {"status": "ok", "call_id": call_id, "pipeline_triggered": ready}


async def _run_pipeline(record: dict):
    call_id = record["call_id"]
    logger.info("Pipeline starting for call_id=%s", call_id)

    try:
        transcript = await _get_transcript(record)
        if not transcript:
            logger.warning("No transcript for call_id=%s — aborting", call_id)
            return

        analysis = await _score_with_claude(transcript)

        # Normalize overall_score to 1-10 if Claude returned a 0-100 value
        raw_score = analysis.get("overall_score")
        if raw_score is not None:
            try:
                s = float(raw_score)
                if s > 10:
                    analysis["overall_score"] = round(s / 10, 1)
                    logger.info("overall_score normalized: %.1f → %.1f", s, analysis["overall_score"])
            except (TypeError, ValueError):
                pass

        if analysis.get("call_type") in ["process_call", "offer_call"]:
            logger.warning("No transcript for call_id=%s — aborting", call_id)
            return

        # Only run the full pipeline for process calls over 3 minutes
        duration = float(record.get("duration") or 0)
        if duration < 180:
            logger.info(
                "Skipping pipeline — process call under 3 minutes | "
                "call_id=%s duration=%.1fs", call_id, duration
            )
            return

        
        agent_analysis: dict = {}
        try:
            agent_analysis = await _score_with_claude_agent(transcript) or {}
        except Exception as exc:
            logger.error("Agent scoring failed for call_id=%s: %s", call_id, exc)

        # use call_from (previously caller) for SF lead resolution
        # returns (sf_id, sf_record_url) so we can build a clickable link in Sheets
        sf_lead_id, sf_record_url = await _resolve_salesforce_lead(record)
        if sf_lead_id:
            record["record_id"] = sf_record_url  # store full URL for Sheets HYPERLINK formula
            await _post_to_salesforce_chatter(sf_lead_id, analysis, record, agent_analysis or None)
            await _update_salesforce_lead_score(sf_lead_id, sf_record_url, analysis)
        else:
            logger.warning("No Salesforce lead found for call_id=%s", call_id)

        await _send_email(analysis, record)

        _append_to_google_sheet(record, analysis, transcript, agent_analysis)

        logger.info("Pipeline complete for call_id=%s", call_id)

    except Exception as exc:
        logger.exception("Pipeline failed for call_id=%s: %s", call_id, exc)


async def _get_transcript(record: dict) -> str | None:
    if record.get("transcript_text"):
        logger.info("Using pre-supplied transcript for call_id=%s", record["call_id"])
        return record["transcript_text"]

    audio_url = record.get("audio_url")
    if not audio_url:
        return None

    logger.info("Downloading audio for Whisper transcription: %s", audio_url)
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.get(audio_url)
            resp.raise_for_status()
            audio_bytes = resp.content
    except httpx.HTTPStatusError as e:
        logger.warning("Failed to download audio for call_id=%s: %s", record.get("call_id"), e)
        return None

    audio_io = io.BytesIO(audio_bytes)
    url_lower = audio_url.lower()
    extension = "mp3"
    if url_lower.endswith(".wav"):
        extension = "wav"
    elif url_lower.endswith(".ogg"):
        extension = "ogg"
    elif url_lower.endswith(".webm"):
        extension = "webm"
    audio_io.name = f"audio.{extension}"

    model = _get_whisper_model()
    segments, info = model.transcribe(
        audio_io,
        language="en",
        task="transcribe",
        without_timestamps=True,
    )

    if info.duration < 180:
        logger.warning(
            "Audio too short for call_id=%s | duration=%.1f seconds (minimum 180s required)",
            record["call_id"],
            info.duration,
        )
        return None

    transcript = " ".join(
        segment.text.strip() for segment in segments if segment.text.strip()
    )
    logger.info(
        "Whisper transcription complete for call_id=%s | length=%d | duration=%.1f seconds",
        record["call_id"],
        len(transcript),
        info.duration,
    )
    # Extract and store actual call duration from audio file
    record["duration"] = info.duration
    return transcript or None


def _extract_json_text(raw: str) -> str:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE)

    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = cleaned[start:end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

    raise ValueError("Unable to parse valid JSON from Claude response")


def _load_active_system_prompt() -> str:
    prompt_text = get_active_prompt_text("rubrics")
    if prompt_text:
        return prompt_text

    logger.warning("Using fallback default scoring prompt")
    return "insert prompt here"


def _normalise_messages(messages: list[dict]) -> list[dict]:
    """
    Anthropic requires strict user/assistant alternation.
    Merge consecutive same-role messages by joining their content
    with a newline so the list is always alternating.
    """
    if not messages:
        return []

    normalised: list[dict] = []
    for msg in messages:
        if normalised and normalised[-1]["role"] == msg["role"]:
            # Merge into the previous message
            normalised[-1]["content"] += "\n\n" + msg["content"]
        else:
            normalised.append({"role": msg["role"], "content": msg["content"]})

    return normalised


async def _score_with_claude(transcript: str) -> dict:
    """
    Score a call transcript using Claude.

    Flow:
      1. Load active system prompt from DB.
      2. Fetch recent conversation history (last CONVERSATION_HISTORY_LIMIT messages).
      3. Build the messages array: history + new user turn (transcript scoring request).
      4. Call Claude.
      5. Persist the new user turn and Claude's reply to conversation_messages.
      6. Parse and return the JSON analysis.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = _load_active_system_prompt()

    current_prompt = (
        "Please score this call transcript and return ONLY valid JSON using the schema defined in the system prompt. "
        "Do not include any commentary, markdown fences, or extra text. "
        "If you cannot supply valid JSON, return an empty JSON object {}.\n\n"
        f"Transcript:\n\n{transcript}"
    )

    # ------------------------------------------------------------------
    # Build messages array from persistent conversation history
    # ------------------------------------------------------------------
    history = get_recent_conversation_messages(prompt_id=1, include_null=True)  # uses CONVERSATION_HISTORY_LIMIT (50); rubrics or NULL

    messages: list[dict] = []
    for row in history:
        role = "user" if row["message_from"] in ("user", "system") else "assistant"
        messages.append({"role": role, "content": row["message"]})

    # Append the current transcript as the new user turn
    messages.append({"role": "user", "content": current_prompt})

    # Ensure strict user/assistant alternation required by Anthropic API
    messages = _normalise_messages(messages)

    # ------------------------------------------------------------------
    # Call Claude
    # ------------------------------------------------------------------
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=messages,
    )

    raw = (message.content[0].text or "").strip()

    # ------------------------------------------------------------------
    # Persist both turns to conversation history
    # ------------------------------------------------------------------
    try:
            add_conversation_message("user", current_prompt, prompt_id=1)
            add_conversation_message("assistant", raw, prompt_id=1)
    except Exception as exc:
        # Non-fatal — log but don't abort the scoring pipeline
        logger.error("Failed to persist conversation messages: %s", exc)

    # ------------------------------------------------------------------
    # Parse JSON response
    # ------------------------------------------------------------------
    try:
        json_text = _extract_json_text(raw)
        return json.loads(json_text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Claude returned invalid JSON for transcript scoring: %s", raw[:1000])
        raise ValueError(f"Claude JSON parse error: {exc}") from exc


async def _score_with_claude_agent(transcript: str) -> dict:
    """
    Score a call transcript using Claude with the active 'agent_scoring' prompt.

    Conversation history is stored and retrieved using the real prompt id from the
    database so the frontend can display it under that prompt, exactly like the
    'rubrics' flow — just filtered by the agent_scoring prompt_id instead.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt, prompt_id = get_active_prompt_with_id("agent_scoring")
    if not system_prompt or system_prompt == "insert prompt here":
        logger.warning("No active 'agent_scoring' prompt found in DB — skipping agent scoring")
        return {}

    current_prompt = (
        "Please score this call transcript and return ONLY valid JSON using the schema defined in the system prompt. "
        "Do not include any commentary, markdown fences, or extra text. "
        "If you cannot supply valid JSON, return an empty JSON object {}.\n\n"
        f"Transcript:\n\n{transcript}"
    )

    history = get_recent_conversation_messages(prompt_id=prompt_id)

    messages: list[dict] = []
    for row in history:
        role = "user" if row["message_from"] in ("user", "system") else "assistant"
        messages.append({"role": role, "content": row["message"]})

    messages.append({"role": "user", "content": current_prompt})
    messages = _normalise_messages(messages)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=messages,
    )

    raw = (message.content[0].text or "").strip()

    try:
        add_conversation_message("user", current_prompt, prompt_id=prompt_id)
        add_conversation_message("assistant", raw, prompt_id=prompt_id)
    except Exception as exc:
        logger.error("Failed to persist agent scoring conversation messages: %s", exc)

    try:
        json_text = _extract_json_text(raw)
        return json.loads(json_text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Agent scoring Claude returned invalid JSON: %s", raw[:1000])
        raise ValueError(f"Agent scoring JSON parse error: {exc}") from exc


async def _resolve_salesforce_lead(record: dict) -> tuple[str, str] | tuple[None, None]:
    phone_from = record.get("call_from")
    phone_to = record.get("call_to")

    logger.info(f"DEBUGGING: PHONE_TO: {phone_to}, RECORD: {record}")

    if not phone_to:
        return None, None

    digits  = re.sub(r"\D", "", str(phone_to))
    last_10 = digits[-10:] if len(digits) >= 10 else digits
    if not last_10:
        return None, None

    # Build multiple phone format variations to match however SF stores it
    area    = last_10[0:3]
    prefix  = last_10[3:6]
    line    = last_10[6:10]
    phone_variants = [
        last_10,                        # 3105614025
        f"+1{last_10}",                 # +13105614025
        f"1{last_10}",                  # 13105614025
        f"({area}) {prefix}-{line}",    # (310) 561-4025  ← THIS is what SF stores
        f"{area}-{prefix}-{line}",      # 310-561-4025
        f"{area}.{prefix}.{line}",      # 310.561.4025
        f"{area} {prefix} {line}",      # 310 561 4025
    ]

    def build_phone_conditions(field: str) -> str:
        return " OR ".join(f"{field} LIKE '%{v}%'" for v in phone_variants)

    logger.info("Searching SF with last_10=%s, variants=%s", last_10, phone_variants)

    access_token = await get_sf_access_token()
    sf_headers   = {"Authorization": f"Bearer {access_token}"}
    query_url    = f"{SF_INSTANCE_URL}/services/data/v57.0/query"

    async with get_client() as client:

        # 1. Lead lookup
        lead_conditions = build_phone_conditions("Phone")
        res = await safe_request(
            client, "GET", query_url,
            params={"q": (
                f"SELECT Id, Owner.Name FROM Lead "
                f"WHERE ({lead_conditions}) AND IsConverted = false LIMIT 1"
            )},
            headers=sf_headers,
        )
        records = res.json().get("records", [])
        if records:
            sf_id  = records[0]["Id"]
            sf_url = f"{SF_INSTANCE_URL}/lightning/r/Lead/{sf_id}/view"
            record["lead_owner"] = (records[0].get("Owner") or {}).get("Name", "")
            logger.info("Lead found for phone %s: ID=%s, Owner=%s", phone_to, sf_id, record["lead_owner"])
            return sf_id, sf_url

        # 2. Opportunity lookup via Account phone fields (all variants, all phone fields)
        opp_conditions = " OR ".join([
            build_phone_conditions("Account.Phone"),
            build_phone_conditions("Account.PersonMobilePhone"),
            build_phone_conditions("Account.PersonHomePhone"),
        ])
        res = await safe_request(
            client, "GET", query_url,
            params={"q": (
                f"SELECT Id, Name, Owner.Name, Account.Phone, Account.PersonMobilePhone, Account.PersonHomePhone "
                f"FROM Opportunity "
                f"WHERE ({opp_conditions}) "
                f"ORDER BY CloseDate DESC NULLS LAST, CreatedDate DESC "
                f"LIMIT 1"
            )},
            headers=sf_headers,
        )
        opp_records = res.json().get("records", [])
        logger.info("opp_records for phone %s: %s", phone_to, opp_records)
        if opp_records:
            opp   = opp_records[0]
            sf_id = opp["Id"]
            sf_url = f"{SF_INSTANCE_URL}/lightning/r/Opportunity/{sf_id}/view"
            record["opportunity_owner"] = (opp.get("Owner") or {}).get("Name", "")
            logger.info("Opportunity found for phone %s: ID=%s, Owner=%s", phone_to, sf_id, record["opportunity_owner"])
            return sf_id, sf_url

        # 3. Contact lookup (all variants)
        contact_conditions = " OR ".join([
            build_phone_conditions("Phone"),
            build_phone_conditions("MobilePhone"),
            build_phone_conditions("HomePhone"),
        ])
        res = await safe_request(
            client, "GET", query_url,
            params={"q": (
                f"SELECT Id, Owner.Name FROM Contact "
                f"WHERE ({contact_conditions}) LIMIT 1"
            )},
            headers=sf_headers,
        )
        records = res.json().get("records", [])
        if records:
            contact_id = records[0]["Id"]
            record["lead_owner"] = (records[0].get("Owner") or {}).get("Name", "")
            logger.info("Contact found for phone %s: ID=%s, Owner=%s", phone_to, contact_id, record["lead_owner"])

            # Try to get linked Opportunity via OpportunityContactRole and post Chatter there
            opp_res = await safe_request(
                client, "GET", query_url,
                params={"q": (
                    f"SELECT Opportunity.Id, Opportunity.Owner.Name "
                    f"FROM OpportunityContactRole "
                    f"WHERE ContactId = '{contact_id}' "
                    f"ORDER BY IsPrimary DESC, Opportunity.CloseDate DESC NULLS LAST, Opportunity.CreatedDate DESC "
                    f"LIMIT 1"
                )},
                headers=sf_headers,
            )
            opp_records = opp_res.json().get("records", [])
            if opp_records:
                opportunity = opp_records[0].get("Opportunity") or {}
                opp_id = opportunity.get("Id", "")
                record["opportunity_owner"] = (opportunity.get("Owner") or {}).get("Name", "")
                logger.info("Linked Opportunity for Contact %s: ID=%s, Owner=%s", contact_id, opp_id, record["opportunity_owner"])
                # Post Chatter on the Opportunity, not the Contact
                return opp_id, f"{SF_INSTANCE_URL}/lightning/r/Opportunity/{opp_id}/view"

            # No linked Opportunity — fall back to posting on the Contact
            return contact_id, f"{SF_INSTANCE_URL}/lightning/r/Contact/{contact_id}/view"

    logger.info("No Lead, Opportunity, or Contact found for phone %s", phone_to)
    return None, None


def _build_chatter_body(analysis: dict, record: dict | None = None, agent_analysis: dict | None = None) -> str:
    record = record or {}

    raw_call_type = str(analysis.get("call_type", "") or "").strip().lower()
    if "process" in raw_call_type:
        call_type_label = "Process Call"
        is_process, is_offer, is_incomplete = True, False, False
    elif "offer" in raw_call_type:
        call_type_label = "Offer Call"
        is_process, is_offer, is_incomplete = False, True, False
    elif "incomplete" in raw_call_type:
        call_type_label = "Incomplete Call"
        is_process, is_offer, is_incomplete = False, False, True
    else:
        call_type_label = raw_call_type.replace("_", " ").title() or "Unknown"
        is_process, is_offer, is_incomplete = False, False, False

    def _val(v):
        return str(v) if v is not None and v != "" else "N/A"

    sections = []

    # ── CALL DETAILS ─────────────────────────────────────────────────────────
    sections.append("\n".join([
        "── CALL DETAILS ──",
        f"Rep Name          : {_val(record.get('user_name'))}",
        f"Lead Owner        : {_val(record.get('lead_owner'))}",
        f"Opportunity Owner : {_val(record.get('opportunity_owner'))}",
        f"Call Type         : {call_type_label}",
        f"Duration          : {_val(record.get('duration'))}",
        f"Overall Score     : {_val(analysis.get('overall_score'))}",
        f"Lead Score        : {_val(analysis.get('lead_score'))}",
    ]))

    # ── CALL SUMMARY ─────────────────────────────────────────────────────────
    call_summary = (analysis.get("call_summary") or "").strip()
    sections.append(f"── CALL SUMMARY ──\n{call_summary or 'N/A'}")

    # ── MISSED QUESTIONS ─────────────────────────────────────────────────────
    missed = analysis.get("missed_questions") or []
    if isinstance(missed, list):
        missed_str = "; ".join(str(q) for q in missed if q) or "None"
    else:
        missed_str = str(missed).strip() or "None"
    sections.append(f"── MISSED QUESTIONS ──\n{missed_str}")

    # ── NEXT BEST ACTION ─────────────────────────────────────────────────────
    sections.append(f"── NEXT BEST ACTION ──\n{(analysis.get('next_best_action') or 'N/A').strip()}")

    # ── FEEDBACK AND RECOMMENDATIONS ─────────────────────────────────────────
    sections.append(f"── FEEDBACK AND RECOMMENDATIONS ──\n{(analysis.get('rep_feedback') or 'N/A').strip()}")

    # ── COACHING SUMMARY ─────────────────────────────────────────────────────
    sections.append(f"── COACHING SUMMARY ──\n{(analysis.get('coaching_summary_for_slack') or 'N/A').strip()}")

    # ── RAPPORT SUMMARY ──────────────────────────────────────────────────────
    sections.append(f"── RAPPORT SUMMARY ──\n{(analysis.get('rapport_connection_summary') or 'N/A').strip()}")

    # ── OBJECTION BOXING (offer calls only) ──────────────────────────────────
    if is_offer:
        sections.append(f"── OBJECTION BOXING ──\n{(analysis.get('objection_boxing_result') or 'N/A').strip()}")

    # ── SCORES ───────────────────────────────────────────────────────────────
    if is_process:
        sections.append("\n".join([
            "── SCORES ──",
            f"Opening              : {_val(analysis.get('opening_score'))}",
            f"Going Deep           : {_val(analysis.get('going_deep_score'))}",
            f"Motivation           : {_val(analysis.get('motivation_score'))}",
            f"Urgency              : {_val(analysis.get('urgency_score'))}",
            f"Condition            : {_val(analysis.get('condition_score'))}",
            f"Price                : {_val(analysis.get('price_score'))}",
            f"Objection            : {_val(analysis.get('objection_score'))}",
            f"Next Step            : {_val(analysis.get('next_step_score'))}",
            f"Rapport & Connection : {_val(analysis.get('rapport_connection_score'))}",
        ]))
    elif is_offer:
        sections.append("\n".join([
            "── SCORES ──",
            f"Expectation Setting      : {_val(analysis.get('expectation_setting_score'))}",
            f"Rapport & Connection     : {_val(analysis.get('rapport_connection_score'))}",
            f"Offer Delivery           : {_val(analysis.get('offer_delivery_score'))}",
            f"Objection Handling       : {_val(analysis.get('objection_handling_declined_score'))}",
            f"Urgency Anchor           : {_val(analysis.get('urgency_anchor_score'))}",
            f"Clear Next Step          : {_val(analysis.get('clear_next_step_score'))}",
        ]))

    # ── INCOMPLETE CALL REASON (incomplete calls only) ───────────────────────
    if is_incomplete:
        sections.append(f"── INCOMPLETE CALL REASON ──\n{(analysis.get('incomplete_call_reason') or 'N/A').strip()}")

    # ── AGENT SCORING ────────────────────────────────────────────────────────
    if agent_analysis:
        ag_missed = agent_analysis.get("missed_items", [])
        if isinstance(ag_missed, list):
            ag_missed_str = "; ".join(str(i) for i in ag_missed if i) or "None"
        else:
            ag_missed_str = str(ag_missed).strip() or "None"
        sections.append("\n".join([
            "── Script Adherence ──",
            f"Score          : {_val(agent_analysis.get('score'))}",
            f"Script Summary : {(agent_analysis.get('script_summary') or 'N/A').strip()}",
            f"Missed Items   : {ag_missed_str}",
        ]))

    return "\n\n".join(sections)


async def _post_to_salesforce_chatter(lead_id: str, analysis: dict, record: dict, agent_analysis: dict | None = None):
    access_token = await get_sf_access_token()
    chatter_url  = f"{SF_INSTANCE_URL}/services/data/v57.0/chatter/feed-elements"
    body_text    = _build_chatter_body(analysis, record, agent_analysis)

    payload = {
        "body": {
            "messageSegments": [
                {"type": "Text", "text": body_text}
            ]
        },
        "feedElementType": "FeedItem",
        "subjectId": lead_id,
    }

    async with get_client() as client:
        res = await safe_request(
            client, "POST", chatter_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
        )

    logger.info("Chatter posted for lead %s | status=%s", lead_id, res.status_code)


async def _update_salesforce_lead_score(sf_id: str, sf_record_url: str, analysis: dict) -> None:
    """PATCH Lead_Score__c on the resolved Salesforce record (Lead or Opportunity)."""
    lead_score = analysis.get("lead_score")
    if lead_score is None or lead_score == "":
        logger.info("No lead_score in analysis — skipping SF Lead_Score__c update for %s", sf_id)
        return

    # Determine object type from the record URL so we hit the right REST endpoint
    if "/r/Opportunity/" in (sf_record_url or ""):
        object_type = "Opportunity"
    elif "/r/Contact/" in (sf_record_url or ""):
        object_type = "Contact"
    else:
        object_type = "Lead"

    update_url   = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/{object_type}/{sf_id}"
    access_token = await get_sf_access_token()

    try:
        score_value = float(lead_score)
    except (TypeError, ValueError):
        logger.warning("lead_score '%s' is not numeric — skipping SF update for %s", lead_score, sf_id)
        return

    async with get_client() as client:
        res = await safe_request(
            client, "PATCH", update_url,
            json={"Ai_Lead_Score__c": score_value},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
        )

    logger.info(
        "Lead_Score__c updated | %s=%s | score=%.1f | status=%s",
        object_type, sf_id, score_value, res.status_code,
    )


async def _send_email(analysis: dict, record: dict) -> None:
    """
    Send call analysis email via Resend API to configured recipients.
    Uses the same formatted content as Salesforce Chatter.
    """
    try:
        body_text = _build_chatter_body(analysis, record)

        resend.api_key = os.environ.get("RESEND_API_KEY")

        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": _EMAIL_RECIPIENTS,
            "subject": _EMAIL_SUBJECT,
            "text": body_text,
        })

        logger.info("Email sent to %s | subject=%s", ", ".join(_EMAIL_RECIPIENTS), _EMAIL_SUBJECT)

    except Exception as exc:
        logger.error("Failed to send email: %s", exc)


def _get_sheets_client() -> gspread.Client:
    creds_source = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if creds_source.strip().startswith("{"):
        try:
            service_account_info = json.loads(creds_source)
            service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON contains invalid JSON") from exc
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(creds_source, scopes=scopes)

    return gspread.authorize(creds)


# updated headers to match new column order and names
_SHEET_HEADERS = [
    "Call ID",
    "Record ID",
    "Call Link",
    "Call From",
    "Call To",
    "Lead Owner",
    "Opportunity Owner",
    "Call Type",
    "Timestamp",
    "Duration",
    "Lead Score",
    "Overall Score",
    "Opening Score",
    "Going Deep Score",
    "Motivation Score",
    "Urgency Score",
    "Condition Score",
    "Price Score",
    "Objection Score",
    "Next Step Score",
    "Rapport & Connection Score (process call)",
    "Seller Motivation",
    "Seller Urgency",
    "Property Condition",
    "Expectation Setting Score",
    "Rapport & Connection Score (offer call)",
    "Offer Delivery Score",
    "Objection Handling Declined Score",
    "Urgency Anchor Score",
    "Clear Next Step Score",
    "Objection Boxing Result",
    "Incomplete Call Reason",
    "Call Summary",
    "Rep Feedback",
    "Rapport Connection Summary",
    "Price Notes",
    "Next Best Action",
    "Coaching Summary For Slack",
    "Missed Questions",
    "Call Transcript",
    "Lead Score Explanation",
    "score",
    "script_summary",
    "missed_items",
]


def _ensure_header_row(worksheet: gspread.Worksheet) -> None:
    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != "Call ID":
        worksheet.insert_row(_SHEET_HEADERS, index=1)
        logger.info("Header row written to '%s'", _SMRT_WORKSHEET_NAME)


def _append_to_google_sheet(record: dict, analysis: dict, transcript: str, agent_analysis: dict | None = None) -> None:
    try:
        gc = _get_sheets_client()
        sh = gc.open_by_key(_SMRT_SHEET_ID)

        try:
            ws = sh.worksheet(_SMRT_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=_SMRT_WORKSHEET_NAME, rows=1000, cols=50)
            logger.info("Created new worksheet '%s'", _SMRT_WORKSHEET_NAME)

    except Exception as exc:
        logger.error("Could not open Google Sheet: %s", exc)
        return

    _ensure_header_row(ws)

    a = analysis
    missed = "; ".join(a.get("missed_questions", []))
    now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PST")

    raw_call_type = str(a.get("call_type", "") or "").strip().lower()
    if "process_call" in raw_call_type:
        call_type_category = "process"
    elif "offer_call" in raw_call_type:
        call_type_category = "offer"
    elif "incomplete_call" in raw_call_type:
        call_type_category = "incomplete"
    else:
        call_type_category = raw_call_type or "unknown"

    call_link = record.get("audio_url") or record.get("call_link", "")
    process_fields = call_type_category == "process"
    offer_fields = call_type_category == "offer"

    row = [
        record.get("call_id", ""),           # Call ID
        # HYPERLINK formula makes the cell a clickable link to the SF Lead/Contact record
        f'=HYPERLINK("{record.get("record_id", "")}", "Open in Salesforce")' if record.get("record_id") else "",  # Record ID
        call_link,                             # Call Link (SMRT recording URL)
        record.get("call_from", ""),          # Call From (renamed from caller)
        record.get("call_to", ""),            # Call To (new)
        record.get("lead_owner", ""),         # Lead Owner (new)
        record.get("opportunity_owner", ""),  # Opportunity Owner (new)
        a.get("call_type", ""),               # Call Type
        now,                                  # Timestamp
        record.get("duration", ""),           # Duration (new)
        a.get("lead_score") if process_fields else "",                        # Lead Score (K)
        a.get("overall_score") if (process_fields or offer_fields) else "",  # Overall Score (L)
        a.get("opening_score") if process_fields else None,               # Opening Score
        a.get("going_deep_score") if process_fields else None,            # Going Deep Score
        a.get("motivation_score") if process_fields else None,            # Motivation Score
        a.get("urgency_score") if process_fields else None,               # Urgency Score
        a.get("condition_score") if process_fields else None,             # Condition Score
        a.get("price_score") if process_fields else None,                 # Price Score
        a.get("objection_score") if process_fields else None,             # Objection Score
        a.get("next_step_score") if process_fields else None,             # Next Step Score
        a.get("rapport_connection_score") if process_fields else None,    # Rapport & Connection Score (process call)
        a.get("seller_motivation", "") if process_fields else "",       # Seller Motivation
        a.get("seller_urgency", "") if process_fields else "",          # Seller Urgency
        a.get("property_condition", "") if process_fields else "",      # Property Condition
        a.get("expectation_setting_score") if offer_fields else None,   # Expectation Setting Score
        a.get("rapport_connection_score") if offer_fields else None,    # Rapport & Connection Score (offer call)
        a.get("offer_delivery_score") if offer_fields else None,        # Offer Delivery Score
        a.get("objection_handling_declined_score") if offer_fields else None,  # Objection Handling Declined Score
        a.get("urgency_anchor_score") if offer_fields else None,        # Urgency Anchor Score
        a.get("clear_next_step_score") if offer_fields else None,       # Clear Next Step Score
        a.get("objection_boxing_result", "") if offer_fields else "", # Objection Boxing Result
        a.get("incomplete_call_reason", "") if call_type_category == "incomplete" else "",  # Incomplete Call Reason
        a.get("call_summary", ""),            # Call Summary
        a.get("rep_feedback", ""),            # Rep Feedback
        a.get("rapport_connection_summary", ""),  # Rapport Connection Summary
        a.get("price_notes", ""),             # Price Notes
        a.get("next_best_action", ""),        # Next Best Action
        a.get("coaching_summary_for_slack", ""),  # Coaching Summary For Slack
        missed,                               # Missed Questions
        transcript,                           # Call Transcript
        a.get("lead_score_explanation", "") if process_fields else "",  # Lead Score Explanation
        # Agent scoring columns (AP, AQ, AR)
        (agent_analysis or {}).get("score", ""),
        (agent_analysis or {}).get("script_summary", ""),
        "; ".join((agent_analysis or {}).get("missed_items", []))
        if isinstance((agent_analysis or {}).get("missed_items"), list)
        else str((agent_analysis or {}).get("missed_items", "")),
    ]
    print(f"DEBUGGING: APPENDING ROW: {row}")

    ws.append_row(row, value_input_option="USER_ENTERED")

    logger.info(
        "Sheet row appended | call_id=%s | sheet=%s | tab=%s",
        record.get("call_id"),
        _SMRT_SHEET_ID,
        _SMRT_WORKSHEET_NAME,
    )
