import asyncio
import json
import logging
import re
from datetime import datetime

import pytz
from fastapi import APIRouter, Request
import csv
import os
import uuid

from clients.client import get_client
from services.llm_service import complete_text
from config.config import DEFAULT_PHONE, SF_INSTANCE_URL, ELEVEN_LABS_KEY, ANTHROPIC_API_KEY
from config.database import (
    create_call_log,
    get_active_prompt_text,
    get_connection,
    get_row_limit,
    update_call_log,
    get_call_log,
    can_retry_on_voicemail,
    increment_voicemail_retry_count,
    add_conversation_message,
    get_recent_conversation_messages,
    CONVERSATION_HISTORY_LIMIT,
)
from repositories.google_sheets_repository import log_to_sheets, update_sheet_row
from services.area_service import get_area_mapping
from services.salesforce_service import get_sf_access_token
from utils.retry import safe_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(funcName)s | %(message)s",
)

Router = APIRouter()
logger = logging.getLogger(__name__)

# Temporary CSV for debugging Claude (Anthropic) I/O. User will remove manually.
CLAUDE_IO_CSV = "/tmp/claude_io_debug.csv"


#  HELPER: normalise a raw Salesforce phone string 

def _clean_phone(raw: str) -> str:
    """Strip non-digits; return empty string if unusable."""
    if not raw or str(raw).strip().lower() in ("", "restricted", "none"):
        return ""
    return re.sub(r"\D", "", raw)


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

    raise ValueError("Unable to parse valid JSON from model response")


def _load_active_analysis_prompt() -> str:
    return get_active_prompt_text("extraction")


def _normalise_messages(messages: list[dict]) -> list[dict]:
    """
    Ensure strict user/assistant alternation by merging consecutive same-role messages.
    """
    if not messages:
        return []

    normalised: list[dict] = []
    for msg in messages:
        if normalised and normalised[-1]["role"] == msg["role"]:
            normalised[-1]["content"] += "\n\n" + msg["content"]
        else:
            normalised.append({"role": msg["role"], "content": msg["content"]})

    return normalised


async def _generate_analysis_from_transcript(transcript: str, conv_id: str = None, lead_id: str = None) -> dict:
    prompt_text = _load_active_analysis_prompt()
    if not prompt_text:
        logger.warning("No active analysis prompt found")
        return {}


    # Build the conversation messages from persistent history for extraction (prompt_id=2)
    history = get_recent_conversation_messages(limit=CONVERSATION_HISTORY_LIMIT, prompt_id=2)
    messages: list[dict] = []
    for row in history:
        role = "user" if row["message_from"] in ("user", "system") else "assistant"
        messages.append({"role": role, "content": row["message"]})

    current_prompt = (
        "Transcript:\n\n" + transcript +
        "\n\nReturn only valid JSON using the schema defined in the system prompt. "
        "Do not include markdown fences or commentary."
    )

    # Append current user turn
    messages.append({"role": "user", "content": current_prompt})
    messages = _normalise_messages(messages)

    raw = complete_text(prompt_text, messages, 2048, json_mode=True)
    parsed_payload = {}
    parse_success = False
    parse_error = None
    try:
        json_text = _extract_json_text(raw)
        parsed = json.loads(json_text)
        if isinstance(parsed, dict):
            parsed_payload = parsed
            parse_success = True
    except Exception as exc:
        parse_error = str(exc)
        logger.error("Failed to parse transcript-generated analysis: %s", exc)
        logger.debug("Transcript analysis raw response: %s", raw)

    # Persist both turns to conversation history (prompt_id=2) and append debug row to CSV
    try:
        try:
            add_conversation_message("user", current_prompt, prompt_id=2)
        except Exception:
            logger.exception("Failed to persist user conversation message for extraction prompt")
        write_header = not os.path.exists(CLAUDE_IO_CSV)
        row_id = str(uuid.uuid4())
        with open(CLAUDE_IO_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow([
                    "timestamp",
                    "row_id",
                    "conv_id",
                    "lead_id",
                    "system_prompt",
                    "transcript",
                    "raw_response",
                    "parse_success",
                    "parse_error",
                    "parsed_json",
                ])

            writer.writerow([
                datetime.utcnow().isoformat(),
                row_id,
                conv_id or "",
                lead_id or "",
                (prompt_text[:2000] + "...") if prompt_text and len(prompt_text) > 2000 else (prompt_text or ""),
                (transcript[:2000] + "...") if transcript and len(transcript) > 2000 else (transcript or ""),
                (raw[:4000] + "...") if raw and len(raw) > 4000 else raw,
                "1" if parse_success else "0",
                parse_error or "",
                json.dumps(parsed_payload, ensure_ascii=False) if parsed_payload else "",
            ])
    except Exception as csv_exc:
        logger.error("Failed to write Claude IO debug CSV: %s", csv_exc, exc_info=True)

    # Persist assistant response as well (best-effort)
    try:
        add_conversation_message("assistant", raw, prompt_id=2)
    except Exception:
        logger.exception("Failed to persist assistant conversation message for extraction prompt")

    return parsed_payload


def _analysis_field_from_payload(payload: dict, key: str):
    key_map = {
        "is_looking_to_sell": "Are they looking to sell?",
        "is_interested": "is_interested",
        "motivation": "Motivation",
        "fair_cash_price": "Fair Cash Price",
        "roadblocks": "Roadblocks",
        "influencer": "Influencer",
        "timeline": "timeline",
        "condition": "condition",
        "next_steps": "next_steps",
        "change_of_mind_reason": "change_of_mind_reason",
        "checkback_time": "checkback_time",
        "lead_score": "lead_score",
        "wrong_call": "wrong_call",
    }
    field_name = key_map.get(key, key)

    data_collection = payload.get("analysis", {}).get("data_collection_results", {})
    if field_name in data_collection:
        field = data_collection.get(field_name, {})
        return field.get("value"), True

    structured = payload.get("analysis", {}).get("structured_data", {})
    if field_name in structured:
        return structured.get(field_name), True

    return None, False


def _build_analysis(payload: dict, generated: dict) -> dict:
    keys = [
        "is_looking_to_sell",
        "is_interested",
        "motivation",
        "fair_cash_price",
        "roadblocks",
        "influencer",
        "timeline",
        "condition",
        "next_steps",
        "change_of_mind_reason",
        "checkback_time",
        "lead_score",
        "wrong_call",
    ]

    analysis = {}
    for key in keys:
        value, present = _analysis_field_from_payload(payload, key)
        if present:
            if value is None or (isinstance(value, str) and value.strip() == ""):
                analysis[key] = None
            else:
                analysis[key] = value
        else:
            analysis[key] = generated.get(key)

    # preserve metadata-based transfer if present, otherwise use generated if any
    transfer_used = (
        payload.get("metadata", {})
               .get("features_usage", {})
               .get("transfer_to_number", {})
               .get("used")
    )
    if transfer_used is not None:
        analysis["call_transferred"] = str(transfer_used)
    else:
        analysis["call_transferred"] = (
            None if generated.get("call_transferred") is None else str(generated.get("call_transferred"))
        )

    transcript_summary = payload.get("analysis", {}).get("transcript_summary")
    if transcript_summary is not None:
        analysis["call_summary"] = transcript_summary or None
    else:
        analysis["call_summary"] = generated.get("call_summary")

    evaluation_results = payload.get("analysis", {}).get("evaluation_criteria_results", {})
    call_interrupted = evaluation_results.get("call_interupted", {}).get("result", "")
    frustrated_with_ai = evaluation_results.get("frustrated_with_ai", {}).get("result", "")
    analysis["call_interrupted"] = call_interrupted
    analysis["frustrated_with_ai"] = frustrated_with_ai

    return analysis


def _needs_generated_analysis(payload: dict) -> bool:
    if not payload.get("analysis"):
        return True

    keys = [
        "is_looking_to_sell",
        "is_interested",
        "motivation",
        "fair_cash_price",
        "roadblocks",
        "influencer",
        "timeline",
        "condition",
        "next_steps",
        "change_of_mind_reason",
        "checkback_time",
        "lead_score",
        "wrong_call",
    ]

    for key in keys:
        if not _analysis_field_from_payload(payload, key)[1]:
            return True

    if "transcript_summary" not in payload.get("analysis", {}):
        return True

    evaluation_results = payload.get("analysis", {}).get("evaluation_criteria_results", {})
    if "call_interupted" not in evaluation_results or "frustrated_with_ai" not in evaluation_results:
        return True

    return False


#  CELERY ENTRY: called by process_sheet() for salesforce_job 

async def trigger_sf_calls(sheet_id: int):
    logger.info(f"SF trigger started for sheet_id={sheet_id}")

    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sheets WHERE id=%s AND type='salesforce_job'",
                (sheet_id,),
            ).fetchone()

        if not row:
            logger.error(f"sheet_id={sheet_id} not found or not a salesforce_job")
            return

        row         = dict(row)
        soql_query  = row.get("query")
        soql_query2 = row.get("query2")
        agent_id    = row.get("agent_id")

        if not soql_query:
            logger.error(f"sheet_id={sheet_id} has no SOQL query configured")
            return
        if not agent_id:
            logger.error(f"sheet_id={sheet_id} has no agent_id configured")
            return

        batch_size   = row.get("batch_size")
        limit        = batch_size if batch_size else get_row_limit()
        access_token = await get_sf_access_token()
        sf_headers   = {"Authorization": f"Bearer {access_token}"}
        query_url    = f"{SF_INSTANCE_URL}/services/data/v57.0/query"

        soql_with_limit = soql_query
        if "limit" not in soql_query.lower():
            soql_with_limit = soql_query.rstrip().rstrip(";") + f" LIMIT {limit}"

        async with get_client() as client:
            res = await safe_request(
                client, "GET", query_url,
                params={"q": soql_with_limit},
                headers=sf_headers,
            )
            leads = res.json().get("records", [])

        if not leads and soql_query2:
            logger.info(f"No leads for sheet_id={sheet_id}. Checking fallback query (query2)...")
            soql_with_limit2 = soql_query2
            if "limit" not in soql_query2.lower():
                soql_with_limit2 = soql_query2.rstrip().rstrip(";") + f" LIMIT {limit}"

            async with get_client() as client:
                res = await safe_request(
                    client, "GET", query_url,
                    params={"q": soql_with_limit2},
                    headers=sf_headers,
                )
                leads = res.json().get("records", [])

        if not leads:
            logger.info(f"No leads returned for sheet_id={sheet_id}")
            return

        results = []

        async with get_client() as client:
            for lead in leads:
                try:
                    await _process_sf_lead(
                        client     = client,
                        lead       = lead,
                        agent_id   = agent_id,
                        sheet_id   = sheet_id,
                        sf_headers = sf_headers,
                        query_url  = query_url,
                    )
                    results.append({"id": lead.get("Id"), "status": "called"})
                except Exception as e:
                    logger.error(f"Error processing lead {lead.get('Id')}: {e}", exc_info=True)

        logger.info(f"SF job sheet_id={sheet_id} done | processed={len(results)}")
        return {"processed": len(results), "results": results}

    except Exception as e:
        logger.error(f"Fatal error in trigger_sf_calls sheet_id={sheet_id}: {e}", exc_info=True)
        return {"error": str(e)}


#  PLACE ONE OUTBOUND CALL 

async def _place_outbound_call(client, agent_id, phone_id, digits, lead_id, address):
    """
    Fires one outbound call via ElevenLabs and returns conv_id.
    Does NOT create a call_log — caller is responsible for that.
    """
    call_res = await safe_request(
        client,
        "POST",
        "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
        json={
            "agent_id": agent_id,
            "agent_phone_number_id": phone_id,
            "to_number": digits,
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "lead_id": lead_id,
                    "address": address,
                }
            },
        },
        headers={"xi-api-key": ELEVEN_LABS_KEY},
    )
    conv_id = call_res.json().get("conversation_id")
    logger.info(f"Outbound call placed → digits={digits} lead_id={lead_id} conv_id={conv_id}")
    return conv_id


async def _process_sf_lead(client, lead, agent_id, sheet_id, sf_headers, query_url):
    """Place one call, insert a placeholder sheet row, and stamp the SF lead."""
    lead_id   = lead.get("Id")
    raw_phone = lead.get("Phone", "")
    digits    = _clean_phone(raw_phone)

    if not digits:
        logger.warning(f"Lead {lead_id}: no usable phone, skipping")
        return

    area_code             = digits[1:4] if len(digits) >= 4 else digits[:3]
    phone_id, called_from = get_area_mapping(area_code)

    if not phone_id:
        logger.info(f"Lead {lead_id}: no area mapping for {area_code}, using default")
        phone_id    = DEFAULT_PHONE
        called_from = DEFAULT_PHONE

    address = lead.get("Street") or lead.get("Address") or "See CRM"
    conv_id = await _place_outbound_call(client, agent_id, phone_id, digits, lead_id, address)

    if conv_id:
        create_call_log(
            conversation_id = conv_id,
            to_number       = digits,
            from_number     = called_from,
            lead_id         = lead_id,
            sheet_id        = sheet_id,
        )

    #  INSERT PLACEHOLDER ROW IN GOOGLE SHEET AT TRIGGER TIME 
    try:
        with get_connection() as conn:
            job_row = conn.execute(
                "SELECT postcall_sheet_url, postcall_worksheet_name FROM sheets WHERE id=%s",
                (sheet_id,),
            ).fetchone()

        if job_row:
            sheet_url      = job_row.get("postcall_sheet_url")
            worksheet_name = job_row.get("postcall_worksheet_name")

            if sheet_url and worksheet_name:
                lead_url = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"
                async with get_client() as c:
                    lead_res  = await c.get(lead_url, headers=sf_headers)
                    lead_info = lead_res.json()

                await asyncio.to_thread(
                    log_to_sheets,
                    lead_info,
                    lead_id,
                    0,
                    conv_id,
                    call_count     = 1,
                    called_from    = called_from,
                    called_to      = digits,
                    analysis       = None,
                    sheet_url      = sheet_url,
                    worksheet_name = worksheet_name,
                )
                logger.info(f"Trigger: placeholder row added for lead {lead_id}")
    except Exception as e:
        logger.error(f"Trigger: error adding sheet row for lead {lead_id}: {e}", exc_info=True)

    #  STAMP SF LEAD 
    pacific_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    update_url  = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"

    await safe_request(
        client,
        "PATCH",
        update_url,
        json={"AI_Bot_Last_Modified_Date_Time__c": pacific_now},
        headers=sf_headers,
    )
    logger.info(f"SF lead {lead_id} stamped, conv_id={conv_id}")


#  POST-CALL WEBHOOK 

@Router.post("/sf-post-call")
async def sf_post_call(request: Request):
    call_disposition = "Unknown"

    try:
        data = await request.json()
        logger.info("SF post-call webhook received")

        if not isinstance(data, dict):
            return {"error": "Invalid payload"}

        payload     = data.get("data", {})
        metadata    = payload.get("metadata", {})
        duration    = int(metadata.get("call_duration_secs", 0))
        call_status = str(payload.get("status", "unknown"))

        #  TRANSCRIPT 
        transcript_lines = []
        for msg in payload.get("transcript", []):
            role = msg.get("role", "").capitalize()
            text = msg.get("message", "")
            if text:
                transcript_lines.append(f"{role}: {text}")
        transcript_str = "\n".join(transcript_lines) or "No transcript"

        #  IDs 
        custom_data = (
            payload.get("conversation_initiation_client_data", {})
                   .get("dynamic_variables", {})
        )
        lead_id    = custom_data.get("lead_id")
        conv_id    = payload.get("conversation_id")
        call_count = custom_data.get("call_count", 0)

        if not lead_id:
            logger.error("SF post-call: missing lead_id")
            return {"error": "Missing lead_id"}

        #  VOICEMAIL DETECTION 
        termination_reason = metadata.get("termination_reason", "")
        voicemail_detected = "voicemail" in termination_reason.lower()

        if not voicemail_detected:
            voicemail_detected = (
                metadata.get("features_usage", {})
                        .get("voicemail_detection", {})
                        .get("used", False)
            )

        logger.info(
            f"conv_id={conv_id} | duration={duration} | "
            f"termination_reason='{termination_reason}' | voicemail_detected={voicemail_detected}"
        )

        #  RESOLVE sheet_id, called_from 
        called_from             = DEFAULT_PHONE
        sheet_id                = None
        postcall_sheet_url      = None
        postcall_worksheet_name = None
        agent_id                = None
        retries_on_voicemail    = 0

        if conv_id:
            log = get_call_log(conv_id)
            print(f"[SHEET DEBUG] conv_id={conv_id} | call_log found={log is not None} | log={log}")
            if log:
                called_from = log.get("from_number") or DEFAULT_PHONE
                sheet_id    = log.get("sheet_id")
            print(f"[SHEET DEBUG] sheet_id={sheet_id} | called_from={called_from}")
        else:
            print(f"[SHEET DEBUG] conv_id is empty — cannot look up call_log")

        if sheet_id:
            with get_connection() as conn:
                job_row = conn.execute(
                    """SELECT postcall_sheet_url, postcall_worksheet_name,
                              agent_id, retries_on_voicemail
                       FROM sheets WHERE id=%s""",
                    (sheet_id,),
                ).fetchone()
                print(f"[SHEET DEBUG] sheets row for sheet_id={sheet_id}: {dict(job_row) if job_row else None}")
                if job_row:
                    postcall_sheet_url      = job_row.get("postcall_sheet_url")
                    postcall_worksheet_name = job_row.get("postcall_worksheet_name")
                    agent_id                = job_row.get("agent_id")
                    retries_on_voicemail    = job_row.get("retries_on_voicemail") or 0
        else:
            print(f"[SHEET DEBUG] sheet_id is None — skipping sheet config lookup")

        #  DETERMINE CALL DISPOSITION + RETRY LOGIC 
        if duration <= 0:
            call_disposition = "Not Answered"

        elif voicemail_detected:
            # Check how many retries have already been used for this conv
            if can_retry_on_voicemail(conv_id, retries_on_voicemail):
                #    Increment BEFORE placing the retry so the new call
                #    starts with an accurate count already in the DB.
                increment_voicemail_retry_count(conv_id)
                retry_count = (get_call_log(conv_id) or {}).get("voicemail_retry_count", 1)
                logger.info(
                    f"Voicemail retry {retry_count}/{retries_on_voicemail} "
                    f"→ placing retry call | lead_id={lead_id} conv_id={conv_id}"
                )

                #  PLACE THE RETRY CALL 
                try:
                    # Resolve phone details from the original call log
                    original_log = get_call_log(conv_id)
                    digits       = original_log.get("to_number", "") if original_log else ""
                    area_code    = digits[1:4] if len(digits) >= 4 else digits[:3]
                    phone_id, _  = get_area_mapping(area_code)

                    if not phone_id:
                        phone_id = DEFAULT_PHONE

                    if not agent_id:
                        logger.error(f"Cannot retry: agent_id missing for sheet_id={sheet_id}")
                        call_disposition = "Voicemail"
                    elif not digits:
                        logger.error(f"Cannot retry: to_number missing for conv_id={conv_id}")
                        call_disposition = "Voicemail"
                    else:
                        access_token_retry = await get_sf_access_token()
                        sf_headers_retry   = {"Authorization": f"Bearer {access_token_retry}"}
                        lead_url           = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"

                        async with get_client() as retry_client:
                            # Place the new outbound call
                            new_conv_id = await _place_outbound_call(
                                retry_client, agent_id, phone_id,
                                digits, lead_id, "See CRM",
                            )

                            if new_conv_id:
                                # Create a fresh call_log for the retry call
                                # linked to the SAME sheet_id so the webhook
                                # can find all the job config again.
                                create_call_log(
                                    conversation_id = new_conv_id,
                                    to_number       = digits,
                                    from_number     = called_from,
                                    lead_id         = lead_id,
                                    sheet_id        = sheet_id,
                                )
                                #    Copy the retry count so the new conv_id
                                #    inherits the same retry state and the limit
                                #    is respected across the chain.
                                with get_connection() as conn:
                                    conn.execute(
                                        """UPDATE call_logs
                                           SET voicemail_retry_count = %s
                                           WHERE conversation_id = %s""",
                                        (retry_count, new_conv_id),
                                    )
                                    conn.commit()

                                logger.info(
                                    f"Retry call placed | new_conv_id={new_conv_id} "
                                    f"lead_id={lead_id} attempt={retry_count}/{retries_on_voicemail}"
                                )

                            # Stamp SF so the lead isn't picked up again immediately
                            pacific_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
                            await retry_client.patch(
                                lead_url,
                                json={"AI_Bot_Last_Modified_Date_Time__c": pacific_now},
                                headers=sf_headers_retry,
                            )

                        call_disposition = "Voicemail"   # current call was voicemail

                except Exception as retry_err:
                    logger.error(f"Retry call failed: {retry_err}", exc_info=True)
                    call_disposition = "Voicemail"

            else:
                # Retry limit reached — mark final and stop
                call_disposition = "Voicemail"
                logger.info(
                    f"Voicemail retry limit reached ({retries_on_voicemail}) "
                    f"→ final Voicemail | conv_id={conv_id}"
                )

        else:
            call_disposition = "Answered"

        #  ANALYSIS FIELDS 
        generated_analysis = {}
        if _needs_generated_analysis(payload):
            generated_analysis = await _generate_analysis_from_transcript(transcript_str, conv_id=conv_id, lead_id=lead_id)

        analysis = _build_analysis(payload, generated_analysis)
        analysis["voicemail_detected"] = voicemail_detected
        analysis["call_disposition"] = call_disposition

        #  UPDATE SALESFORCE 
        access_token = await get_sf_access_token()
        sf_headers   = {"Authorization": f"Bearer {access_token}"}
        update_url   = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"

        sf_payload = {
            "Call_Duration__c":                  float(duration),
            "Call_Status__c":                    call_status,
            "Call_Transcript__c":                transcript_str,
            "AI_Bot_Last_Modified_Date_Time__c": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        }

        async with get_client() as client:
            patch_res = await client.patch(update_url, json=sf_payload, headers=sf_headers)
            if patch_res.status_code >= 400:
                logger.error(f"Salesforce PATCH error: {patch_res.text}")
                raise Exception(f"Salesforce PATCH error: {patch_res.text}")

            get_res   = await client.get(update_url, headers=sf_headers)
            lead_info = get_res.json()

        #  UPDATE CALL LOG
        if conv_id:
            update_call_log(
                conversation_id  = conv_id,
                call_disposition = call_disposition,
                duration_secs    = duration,
                call_status      = call_status,
                transcript       = transcript_str,
                wrong_call       = str(analysis.get("wrong_call")) if analysis.get("wrong_call") is not None else None,
                transfer_used    = analysis.get("call_transferred"),
                lead_score       = str(analysis.get("lead_score")) if analysis.get("lead_score") is not None else None,
            )

        #  UPDATE GOOGLE SHEET ROW 
        called_to = ""
        if conv_id:
            log = get_call_log(conv_id)
            if log:
                called_to = log.get("to_number", "")

        # ── LEAD SCORE DEBUG: trace exactly what the payload returned ──────────
        raw_lead_score_dc = (
            payload.get("analysis", {})
                   .get("data_collection_results", {})
                   .get("lead_score", {})
        )
        raw_lead_score_sd = (
            payload.get("analysis", {})
                   .get("structured_data", {})
                   .get("lead_score")
        )
        logger.info(
            f"[LEAD_SCORE DEBUG] conv_id={conv_id} | lead_id={lead_id} | "
            f"data_collection_results['lead_score'] raw block = {raw_lead_score_dc} | "
            f"structured_data['lead_score'] = {raw_lead_score_sd} | "
            f"resolved analysis['lead_score'] = {analysis.get('lead_score')!r}"
        )
        # ── END LEAD SCORE DEBUG ────────────────────────────────────────────────

        # ── FULL SHEET-UPDATE PAYLOAD LOG ───────────────────────────────────────
        logger.info(
            f"[SHEET UPDATE] conv_id={conv_id} | lead_id={lead_id} | "
            f"sheet_url={postcall_sheet_url!r} | worksheet={postcall_worksheet_name!r} | "
            f"called_from={called_from!r} | called_to={called_to!r} | "
            f"duration={duration} | call_count={call_count} | "
            f"call_disposition={call_disposition!r} | voicemail_detected={voicemail_detected} | "
            f"analysis={analysis}"
        )
        # ── END SHEET-UPDATE PAYLOAD LOG ────────────────────────────────────────

        print(f"[SHEET DEBUG] Final check — postcall_sheet_url={postcall_sheet_url!r} | postcall_worksheet_name={postcall_worksheet_name!r} | called_to={called_to!r}")

        if postcall_sheet_url and postcall_worksheet_name and called_to:
            await asyncio.to_thread(
                update_sheet_row,
                lead_info          = lead_info,
                lead_id            = lead_id,
                duration           = duration,
                conv_id            = conv_id,
                call_count         = call_count,
                called_from        = called_from,
                called_to          = called_to,
                analysis           = analysis,
                sheet_url          = postcall_sheet_url,
                worksheet_name     = postcall_worksheet_name,
                call_disposition   = call_disposition,
                voicemail_detected = voicemail_detected,
            )
        else:
            missing = [k for k, v in {
                "postcall_sheet_url":      postcall_sheet_url,
                "postcall_worksheet_name": postcall_worksheet_name,
                "called_to":               called_to,
            }.items() if not v]
            logger.warning(f"Post-call: skipping sheet update, missing: {missing}")

        logger.info(f"SF post-call completed | lead_id={lead_id} | disposition={call_disposition}")
        return {"status": "success", "duration": duration, "disposition": call_disposition}

    except Exception as e:
        logger.error(f"SF post-call error: {e}", exc_info=True)
        return {"status": "error", "message": "Internal error"}


#  CALL-END WEBHOOK (mid-call SF update for FUS agent)

_SF_CALL_END_FIELDS = {
    "reason":     "Change_of_Mind_Reason__c",
    "interested": "Is_Interested_in_Selling__c",
    "callback":   "Check_Back_Time__c",
}

@Router.post("/sf-call-end")
async def sf_call_end(request: Request):
    try:
        data = await request.json()
        logger.info(f"SF call-end webhook received: {data}")

        if not isinstance(data, dict):
            return {"status": "error", "message": "Invalid payload"}

        params    = data.get("parameters", {})
        variables = (
            data.get("conversation_initiation_client_data", {})
                .get("dynamic_variables", {})
        )

        lead_id = variables.get("lead_id") or data.get("lead_id")
        if not lead_id:
            logger.error("SF call-end: missing lead_id")
            return {"status": "error", "message": "Missing lead_id"}

        reason     = str(params.get("what_changed", "No reason provided"))[:255]
        interested = str(params.get("is_interested", "Unknown"))[:50]
        callback   = str(params.get("callback_time", ""))[:50]

        sf_payload = {
            _SF_CALL_END_FIELDS["reason"]:     reason,
            _SF_CALL_END_FIELDS["interested"]: interested,
            _SF_CALL_END_FIELDS["callback"]:   callback,
        }

        access_token = await get_sf_access_token()
        sf_headers   = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
        }
        update_url = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"

        async with get_client() as client:
            await safe_request(client, "PATCH", update_url, json=sf_payload, headers=sf_headers)

        logger.info(f"SF call-end: lead {lead_id} updated successfully")
        return {"status": "success"}

    except Exception as e:
        logger.error(f"SF call-end error: {e}", exc_info=True)
        return {"status": "error", "message": "Internal server error"}