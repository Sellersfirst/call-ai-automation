from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import gspread
import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from google.oauth2.service_account import Credentials

from config.config import ANTHROPIC_API_KEY
from config.database import (
    add_conversation_message,
    get_active_prompt_with_id,
    get_recent_conversation_messages,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ghl", tags=["ghl"])

_GHL_SHEET_ID = "1ZjARCtxcGIzr0ydRutLGYVQ94Ey3u37fUZoACgzEgP8"
_GHL_WORKSHEET_ID = 1446116535
_GHL_API_BASE_URL = "https://services.leadconnectorhq.com"
_GHL_API_VERSION = "2021-07-28"
_GHL_OWNER_FALLBACK = "None assigned"

_SHEET_HEADERS = [
    "Call ID",
    "Record ID",
    "Call From",
    "Call To",
    "Lead Owner",
    "Timestamp",
    "Rep Name",
    "Duration",
    "Lead Score",
    "Overall Score",
    "Evidence Pitch Score",
    "Screenshot Coaching Score",
    "Retainer Score",
    "DNC Score",
    "Closing Score",
    "Rapport Score",
    "Evidence Collection",
    "Call Summary",
    "Rep Feedback",
    "Evidence Pitch Score Evaluation",
    "Next Best Action",
    "Missed Questions",
]


@router.post("/call-transcript")
async def ghl_call_transcript(request: Request, background_tasks: BackgroundTasks):
    try:
        payload: dict = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    record = _build_record(payload)
    if not record["transcript"]:
        raise HTTPException(status_code=400, detail="Missing customData.transcript")
    if not record["call_id"]:
        raise HTTPException(status_code=400, detail="Missing customData.call_id")

    logger.info("GHL call transcript received | call_id=%s contact_id=%s", record["call_id"], record["contact_id"])
    background_tasks.add_task(_run_pipeline, record)

    return {"status": "ok", "call_id": record["call_id"], "pipeline_triggered": True}


def _build_record(payload: dict) -> dict[str, Any]:
    custom_data = payload.get("customData") or payload.get("custom_data") or {}

    return {
        "contact_id": payload.get("contact_id") or payload.get("contactId") or custom_data.get("contact_id"),
        "call_id": custom_data.get("call_id") or custom_data.get("callId") or payload.get("call_id"),
        "call_from": custom_data.get("call_from") or custom_data.get("callFrom") or payload.get("call_from"),
        "call_to": custom_data.get("call_to") or custom_data.get("callTo") or payload.get("call_to"),
        "user_id": custom_data.get("user_id") or custom_data.get("userId") or payload.get("user_id"),
        "duration": custom_data.get("call_duration") or custom_data.get("callDuration") or payload.get("call_duration"),
        "timestamp": payload.get("timestamp") or payload.get("date") or payload.get("createdAt"),
        "transcript": custom_data.get("transcript") or payload.get("transcript"),
    }


async def _run_pipeline(record: dict[str, Any]) -> None:
    call_id = record["call_id"]
    logger.info("GHL pipeline starting | call_id=%s", call_id)

    try:
        analysis = await _score_with_claude(record["transcript"])
        record["lead_owner"] = await get_lead_owner_name(record.get("contact_id"))
        _append_to_google_sheet(record, analysis)
        logger.info("GHL pipeline complete | call_id=%s", call_id)
    except Exception as exc:
        logger.exception("GHL pipeline failed | call_id=%s error=%s", call_id, exc)


async def _score_with_claude(transcript: str) -> dict[str, Any]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt, prompt_id = get_active_prompt_with_id("ghl_rubrics")
    if not system_prompt or system_prompt == "insert prompt here":
        raise ValueError("No active 'ghl_rubrics' prompt found in DB")

    current_prompt = (
        "Score this GHL call transcript and return ONLY valid JSON using this exact shape: "
        '{"scores":{"evidence_pitch":0,"screenshot_coaching":0,"retainer_agreement":0,'
        '"dnc":0,"evidence_coaching":0,"closing":0,"rapport":0,"overall":0,"lead":0},'
        '"call_summary":"","next_best_action":"","rep_feedback":"","missed_questions":[],'
        '"rep_name":null,"evidence_collection":"","evidence_pitch_evaluation":""}. '
        "rep_name: the name of the agent/rep making the call, NOT the lead/prospect being called — "
        "look for the caller's self-introduction (e.g. \"Hi, this is Alex calling from...\") and use that name. "
        "Ignore any name belonging to the lead/prospect. Use null if the rep's name is never mentioned. "
        "evidence_collection: a 2-3 sentence explanation of the client's evidence status — whether they confirmed "
        "having evidence, agreed or were willing to send it, and whether they said they'd submit it during the call "
        "or later. Be concrete, quote or paraphrase the relevant exchange, do not infer unstated information. "
        "evidence_pitch_evaluation: a 2-3 sentence explanation of why the evidence_pitch score was given, citing "
        "specific transcript moments — what the rep did or didn't do relative to the rubric, whether the client "
        "agreed, whether an objection was handled, and whether live evidence/screenshot submission actually started. "
        "Do not include commentary, markdown fences, or extra text.\n\n "
        f"Transcript:\n\n{transcript}"
    )

    messages: list[dict[str, str]] = []
    if prompt_id is not None:
        for row in get_recent_conversation_messages(prompt_id=prompt_id):
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

    if prompt_id is not None:
        try:
            add_conversation_message("user", current_prompt, prompt_id=prompt_id)
            add_conversation_message("assistant", raw, prompt_id=prompt_id)
        except Exception as exc:
            logger.error("Failed to persist GHL scoring conversation messages: %s", exc)

    try:
        return json.loads(_extract_json_text(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Claude returned invalid GHL JSON: %s", raw[:1000])
        raise ValueError(f"GHL Claude JSON parse error: {exc}") from exc


def _normalise_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    for msg in messages:
        if normalised and normalised[-1]["role"] == msg["role"]:
            normalised[-1]["content"] += "\n\n" + msg["content"]
        else:
            normalised.append({"role": msg["role"], "content": msg["content"]})
    return normalised


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
            json.loads(candidate)
            return candidate

    raise ValueError("Unable to parse valid JSON from Claude response")


def _ghl_api_key() -> str | None:
    # return os.getenv("GHL_API_KEY") or os.getenv("LEADCONNECTOR_API_KEY")
    return "pit-a138ab6f-44f5-4aea-973f-9ef51658b2e4"


def _ghl_headers() -> dict[str, str]:
    api_key = _ghl_api_key()
    if not api_key:
        raise RuntimeError("Missing GHL_API_KEY or LEADCONNECTOR_API_KEY")

    return {
        "Authorization": f"Bearer {api_key}",
        "Version": _GHL_API_VERSION,
        "Accept": "application/json",
    }


async def get_contact_by_contact_id(contact_id: str | None) -> dict[str, Any] | None:
    if not contact_id:
        return None

    url = f"{_GHL_API_BASE_URL}/contacts/{contact_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=_ghl_headers())
        response.raise_for_status()
        return response.json().get("contact") or {}


async def get_user_by_user_id(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None

    url = f"{_GHL_API_BASE_URL}/users/{user_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=_ghl_headers())
        response.raise_for_status()
        return response.json()


async def get_lead_owner_name(contact_id: str | None) -> str:
    try:
        contact = await get_contact_by_contact_id(contact_id)
        assigned_to = (contact or {}).get("assignedTo")
        if not assigned_to:
            return _GHL_OWNER_FALLBACK

        user = await get_user_by_user_id(assigned_to)
        name = _extract_user_name(user or {})
        return name or _GHL_OWNER_FALLBACK
    except Exception as exc:
        logger.warning("Failed to resolve GHL lead owner for contact_id=%s: %s", contact_id, exc)
        return _GHL_OWNER_FALLBACK


def _extract_user_name(data: dict[str, Any]) -> str | None:
    user = data.get("user") if isinstance(data.get("user"), dict) else data

    name = user.get("name")
    if name:
        return str(name)

    first_name = user.get("firstName") or user.get("first_name") or ""
    last_name = user.get("lastName") or user.get("last_name") or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or None


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


def _ensure_header_row(worksheet: gspread.Worksheet) -> None:
    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != "Call ID":
        worksheet.insert_row(_SHEET_HEADERS, index=1)
        logger.info("GHL header row written to worksheet id=%s", _GHL_WORKSHEET_ID)


def _append_to_google_sheet(record: dict[str, Any], analysis: dict[str, Any]) -> None:
    gc = _get_sheets_client()
    spreadsheet = gc.open_by_key(_GHL_SHEET_ID)
    worksheet = spreadsheet.get_worksheet_by_id(_GHL_WORKSHEET_ID)
    if worksheet is None:
        raise RuntimeError(f"Worksheet id {_GHL_WORKSHEET_ID} not found")

    _ensure_header_row(worksheet)

    scores = analysis.get("scores") or {}
    missed_questions = analysis.get("missed_questions") or []
    if isinstance(missed_questions, list):
        missed_questions_text = "; ".join(str(item) for item in missed_questions if item)
    else:
        missed_questions_text = str(missed_questions)

    row = [
        record.get("call_id", ""),
        record.get("contact_id", ""),
        record.get("call_from", ""),
        record.get("call_to", ""),
        record.get("lead_owner", _GHL_OWNER_FALLBACK),
        _format_timestamp(record.get("timestamp")),
        analysis.get("rep_name") or "",
        record.get("duration", ""),
        scores.get("lead", ""),
        scores.get("overall", ""),
        scores.get("evidence_pitch", ""),
        scores.get("screenshot_coaching", ""),
        scores.get("retainer_agreement", ""),
        scores.get("dnc", ""),
        scores.get("closing", ""),
        scores.get("rapport", ""),
        analysis.get("evidence_collection") or "",
        analysis.get("call_summary", ""),
        analysis.get("rep_feedback", ""),
        analysis.get("evidence_pitch_evaluation") or "",
        analysis.get("next_best_action", ""),
        missed_questions_text,
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info("GHL analysis appended to sheet | call_id=%s", record.get("call_id"))


def _format_timestamp(raw_timestamp: Any) -> str:
    if raw_timestamp:
        return str(raw_timestamp)

    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PST")
