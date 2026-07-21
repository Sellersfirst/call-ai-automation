import logging, pytz
from fastapi import APIRouter, Request
from datetime import datetime
from config.config import DEFAULT_PHONE
from services.sheets_workflow_service import get_leads, normalize_phone, update_row
from services.area_service import get_area_mapping
from services.call_service import make_call
from repositories.google_sheets_repository import get_client, find_row_by_phone, append_extracted_variables
from services.variable_extraction_service import extract_variables
from utils.phone_utils import remove_plus, phones_match
from utils.sheet_utils import extract_sheet_id
from config.database import (get_connection, get_row_limit, create_call_log,update_call_log, get_call_log,can_retry_on_voicemail, increment_voicemail_retry_count, get_prompt_text_by_id,)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(funcName)s | %(message)s"
)
Router = APIRouter()
logger = logging.getLogger(__name__)


#  TRIGGER CALLS (CELERY ENTRY) 

async def trigger_calls(sheet_id: int):
    try:
        logger.info(f"Trigger started for sheet_id={sheet_id}")

        with get_connection() as conn:
            sheet_data = conn.execute(
                "SELECT * FROM sheets WHERE id=%s AND type='google_sheet_job'",
                (sheet_id,)
            ).fetchone()

        if not sheet_data:
            logger.error(f"{sheet_id} Sheet not found in DB")
            return

        sheet_data     = dict(sheet_data)
        sheet_url      = sheet_data["google_sheet_url"]
        worksheet_name = sheet_data["worksheet_name"]
        agent_id       = sheet_data.get("agent_id")

        if not agent_id:
            logger.error(f"No agent_id found for sheet {sheet_id}")
            return {"error": "Agent ID not configured"}

        client    = get_client()
        sheet_key = extract_sheet_id(sheet_url)
        sheet     = client.open_by_key(sheet_key).worksheet(worksheet_name)

        limit = sheet_data.get("batch_size") or get_row_limit()
        leads = get_leads(sheet, limit=limit)

        if not leads:
            logger.info(f"No leads found for sheet {sheet_id}")
            return {"message": "No leads found"}

        results = []

        for lead in leads:
            try:
                phone, area = normalize_phone(
                    lead.get("VALID_PHONES"),
                    lead.get("MOBILE_PHONE")
                )

                if not phone:
                    logger.warning(f"Skipping lead — invalid phone: {lead.get('VALID_PHONES') or lead.get('MOBILE_PHONE')}")
                    continue

                phone_id, called_from = get_area_mapping(area)
                if not phone_id:
                    logger.warning(f"No mapping for {area}, using default phone")
                    phone_id    = DEFAULT_PHONE
                    called_from = DEFAULT_PHONE

                call_resp = await make_call(phone_id, phone, lead.get("Address"), agent_id)

                if not call_resp:
                    logger.warning(f"Skipping lead — call failed: {phone}")
                    continue

                conv_id = call_resp.get("conversation_id")

                if conv_id:
                    create_call_log(
                        conversation_id = conv_id,
                        to_number       = phone,
                        from_number     = called_from,
                        sheet_id        = sheet_id,
                    )

                clean_phone = remove_plus(phone)
                call_count  = int(lead.get("Call_Count") or 0) + 1
                row_id      = find_row_by_phone(sheet, clean_phone)

                if not row_id:
                    logger.warning("Skipping update — row not found")
                    continue

                update_row(sheet, row_id, call_count, called_from, phone)
                results.append({"phone": phone, "status": "called"})

            except Exception as e:
                logger.error(f"Error processing lead: {e}", exc_info=True)

        logger.info(f"Completed sheet {sheet_id} | processed={len(results)}")
        return {"processed": len(results), "results": results}

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return {"error": str(e)}


#  POST CALL WEBHOOK 

@Router.post("/post-call")
async def post_call_update(request: Request):
    try:
        logger.info("Post-call webhook received")

        data    = await request.json()
        payload = data.get("data", {})

        called_number = (
            payload.get("conversation_initiation_client_data", {})
                   .get("dynamic_variables", {})
                   .get("system__called_number")
        )

        if not called_number:
            return {"error": "No called_number found"}

        phone   = str(called_number).replace("+", "")
        conv_id = payload.get("conversation_id")

        #  DATA FROM PAYLOAD 
        analysis = payload.get("analysis", {}).get("data_collection_results", {})
        metadata = payload.get("metadata", {})
        duration = float(metadata.get("call_duration_secs", 0) or 0)

        #  VOICEMAIL DETECTION 
        # Old code read from data_collection_results["voicemail_detected"] which
        # doesn't exist in ElevenLabs payloads. Fixed to read termination_reason
        # and features_usage — the two fields ElevenLabs actually sends.
        termination_reason = metadata.get("termination_reason", "")
        voicemail_flag     = "voicemail" in termination_reason.lower()

        if not voicemail_flag:
            voicemail_flag = (
                metadata.get("features_usage", {})
                        .get("voicemail_detection", {})
                        .get("used", False)
            )

        #  TRANSFER DETECTION 
        transfer_used = str(
            metadata.get("features_usage", {})
                    .get("transfer_to_number", {})
                    .get("used", "")
        ).lower() == "true"

        logger.info(
            f"conv_id={conv_id} | duration={duration} | "
            f"termination_reason='{termination_reason}' | "
            f"voicemail_flag={voicemail_flag} | transfer_used={transfer_used}"
        )

        #  FIND SHEET + ROW 
        client = get_client()

        with get_connection() as conn:
            sheets = conn.execute(
                "SELECT * FROM sheets WHERE status=TRUE AND type='google_sheet_job'"
            ).fetchall()

        row_id     = None
        sheet      = None
        sheet_db   = None   # the DB row for the matched sheet

        for s in sheets:
            s         = dict(s)
            sheet_key = extract_sheet_id(s["google_sheet_url"])
            temp_sheet = client.open_by_key(sheet_key).worksheet(s["worksheet_name"])
            records    = temp_sheet.get_all_records()

            for idx, r in enumerate(records, start=2):
                if phones_match(phone, r.get("VALID_PHONES", "")) or phones_match(phone, r.get("MOBILE_PHONE", "")):
                    row_id   = idx
                    sheet    = temp_sheet
                    sheet_db = s
                    logger.info(f"Match found in sheet {s['id']} row {row_id}")
                    break

            if row_id:
                break

        if not row_id or not sheet:
            logger.warning("No matching lead found in any sheet")
            return {"message": "No matching lead"}

        #  RESOLVE RETRY CONFIG 
        retries_on_voicemail = (sheet_db.get("retries_on_voicemail") or 0) if sheet_db else 0
        agent_id             = sheet_db.get("agent_id") if sheet_db else None
        sheet_id             = sheet_db.get("id") if sheet_db else None

        #  DETERMINE DISPOSITION + FIRE RETRY IF NEEDED 
        if duration <= 0:
            disposition = "Not Answered"

        elif voicemail_flag:
            if conv_id and can_retry_on_voicemail(conv_id, retries_on_voicemail):
                # Increment BEFORE placing the retry call
                increment_voicemail_retry_count(conv_id)
                retry_count = (get_call_log(conv_id) or {}).get("voicemail_retry_count", 1)

                logger.info(
                    f"Voicemail retry {retry_count}/{retries_on_voicemail} "
                    f"→ placing retry call | phone={phone} conv_id={conv_id}"
                )

                #  PLACE RETRY CALL 
                try:
                    area_code        = phone[1:4] if len(phone) >= 4 else phone[:3]
                    phone_id, called_from_retry = get_area_mapping(area_code)
                    if not phone_id:
                        phone_id        = DEFAULT_PHONE
                        called_from_retry = DEFAULT_PHONE

                    if not agent_id:
                        logger.error("Cannot retry — agent_id missing")
                        disposition = "Voicemail"
                    else:
                        retry_resp = await make_call(phone_id, phone, "See Sheet", agent_id)

                        if retry_resp:
                            new_conv_id = retry_resp.get("conversation_id")
                            if new_conv_id:
                                # Create fresh call_log for the retry
                                create_call_log(
                                    conversation_id = new_conv_id,
                                    to_number       = phone,
                                    from_number     = called_from_retry,
                                    sheet_id        = sheet_id,
                                )
                                # Copy retry count forward so the limit is
                                # respected across the whole call chain
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
                                    f"attempt={retry_count}/{retries_on_voicemail}"
                                )
                        else:
                            logger.warning(f"Retry call failed for phone={phone}")

                        disposition = "Voicemail"

                except Exception as retry_err:
                    logger.error(f"Retry call error: {retry_err}", exc_info=True)
                    disposition = "Voicemail"

            else:
                # Limit reached or retries not configured
                disposition = "Voicemail"
                logger.info(
                    f"Voicemail limit reached ({retries_on_voicemail}) "
                    f"→ final Voicemail | conv_id={conv_id}"
                )

        elif transfer_used:
            disposition = "Transferred"

        else:
            disposition = "Answered"

        #  TIME CONVERSION 
        timestamp    = payload.get("event_timestamp")
        pacific_time = ""
        if timestamp:
            dt           = datetime.utcfromtimestamp(timestamp)
            pacific      = pytz.timezone("America/Los_Angeles")
            pacific_time = dt.replace(tzinfo=pytz.utc).astimezone(pacific).strftime("%m/%d/%Y %H:%M:%S")

        #  UPDATE GOOGLE SHEET ROW 
        sheet.update(f"L{row_id}", [[disposition]])
        sheet.update(f"M{row_id}", [[pacific_time]])
        sheet.update(f"O{row_id}", [[analysis.get("wrong_call", {}).get("value")]])
        sheet.update(f"P{row_id}", [[analysis.get("Do they want to sell?", {}).get("value")]])
        sheet.update(f"Q{row_id}", [[analysis.get("call_back_time", {}).get("value")]])
        sheet.update(f"R{row_id}", [[str(metadata.get("features_usage", {}).get("transfer_to_number", {}).get("used"))]])
        sheet.update(f"T{row_id}", [[metadata.get("call_duration_secs")]])
        sheet.update(f"U{row_id}", [[analysis.get("lead_score", {}).get("value")]])

        logger.info(f"Post-call updated row {row_id} | disposition={disposition}")

        #  UPDATE CALL LOG 
        if conv_id:
            update_call_log(
                conversation_id = conv_id,
                call_disposition = disposition,
                duration_secs    = metadata.get("call_duration_secs"),
                call_status      = str(payload.get("status", "")),
                wrong_call       = str(analysis.get("wrong_call", {}).get("value", "") or ""),
                wants_to_sell    = str(analysis.get("Do they want to sell?", {}).get("value", "") or ""),
                callback_time    = str(analysis.get("call_back_time", {}).get("value", "") or ""),
                lead_score       = str(analysis.get("lead_score", {}).get("value", "") or ""),
                transfer_used    = str(metadata.get("features_usage", {}).get("transfer_to_number", {}).get("used", "") or ""),
            )

        #  POST-CALL VARIABLE EXTRACTION → OUTPUT SHEET (optional, per-job)
        try:
            if (duration > 0 and sheet_db.get("output_sheet_url") and sheet_db.get("output_worksheet_name")
                    and sheet_db.get("variables_to_record") and sheet_db.get("extraction_prompt_id")):

                prompt_text = get_prompt_text_by_id(sheet_db["extraction_prompt_id"])

                if prompt_text:
                    var_names = [v.strip() for v in sheet_db["variables_to_record"].split(",") if v.strip()]
                    transcript_text = "\n".join(
                        f"{m.get('role', '').capitalize()}: {m.get('message', '')}"
                        for m in payload.get("transcript", []) if m.get("message")
                    )
                    summary = payload.get("analysis", {}).get("transcript_summary") or ""

                    extracted = extract_variables(
                        prompt_text, transcript_text, summary, var_names, sheet_db.get("variable_descriptions")
                    )

                    if extracted:
                        data_map = {
                            **extracted,
                            "Phone": phone,
                            "Conversation ID": conv_id or "",
                            "Timestamp": pacific_time,
                        }
                        append_extracted_variables(
                            client, sheet_db["output_sheet_url"], sheet_db["output_worksheet_name"], data_map
                        )
                else:
                    logger.warning(
                        f"extraction_prompt_id={sheet_db['extraction_prompt_id']} not found — skipping extraction"
                    )
            elif any(sheet_db.get(f) for f in
                     ("output_sheet_url", "output_worksheet_name", "variables_to_record", "extraction_prompt_id")):
                logger.info(f"sheet_id={sheet_db.get('id')} extraction config incomplete — skipping")
        except Exception as extract_err:
            logger.error(f"Variable extraction error: {extract_err}", exc_info=True)

        return {"status": "updated", "row": row_id, "disposition": disposition}

    except Exception as e:
        logger.error(f"Post-call error: {e}", exc_info=True)
        return {"error": str(e)}