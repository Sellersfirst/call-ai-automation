"""
NEPQ rubric: scores the same SMRT call transcripts as the main rubric, using the
active 'rubric_nepq' prompt, and writes the result to the 'Rubric NEPQ' tab only.
No Chatter, Salesforce, email or WhatsApp output.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

from config.config import ANTHROPIC_API_KEY_RUBRIC
from config.database import get_active_prompt_text
from services.llm_service import complete_text

logger = logging.getLogger(__name__)

NEPQ_PROMPT_TYPE = "rubric_nepq"
NEPQ_SHEET_ID = "1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok"
NEPQ_WORKSHEET_NAME = "Rubric NEPQ"

# Must match the header row of the 'Rubric NEPQ' tab (column order matters).
NEPQ_SHEET_HEADERS = [
    "Call ID", "Record ID", "Call Link", "Call From", "Call To", "Address",
    "Lead Owner", "Opportunity Owner", "Call Type", "Timestamp", "Duration",
    "Asking Price", "Zesitmate", "Total Debt / Liens", "Lead Score Explanation",
    "Lead Score", "Motivation", "Distress", "Urgency", "Timing", "Equity",
    "Overall Score", "Trust", "Pacing & Delivery", "Tonality", "Active Listening",
    "Clarifying and Probing", "Problem Impact", "Urgency", "Future State",
    "Objection Prevention", "Needs & Anchor", "Commitment Before Close",
    "Objection Handling", "Seller Motivation", "Seller Urgency", "Property Condition",
    "Expectation Setting Score", "Rapport & Connection Score (offer call)",
    "Offer Delivery Score", "Objection Handling Declined Score", "Urgency Anchor Score",
    "Clear Next Step Score", "Objection Boxing Result", "Incomplete Call Reason",
    "Call Summary", "Rep Feedback", "Rapport Connection Summary", "missed_items",
    "Price Notes", "Next Best Action", "Call Transcript",
]

_OUTPUT_FORMAT = """
OUTPUT FORMAT (MANDATORY — overrides the 'REP PERFORMANCE OUTPUT' layout above)

Return ONLY one valid JSON object, no markdown fences and no commentary. Apply every rule above when deciding each value; only the layout changes. Use these exact keys:

{
  "call_type": "process_call" | "offer_call",
  "call_type_mismatch": "<short note or empty string>",

  "lead_score": <integer 1-10 or null>,
  "lead_classification": "Hot" | "Warm" | "Cold" | "Unclassified — Provisional" | "Insufficient information",
  "lead_assessment": "Confirmed" | "Provisional" | "Insufficient information",
  "lead_motivation": <integer 1-10 or "Unknown">,
  "lead_distress": <integer 1-10 or "Unknown">,
  "lead_urgency": <integer 1-10 or "Unknown">,
  "lead_timing": <integer 1-10 or "Unknown">,
  "lead_equity": <integer 1-10 or "">,
  "lead_score_explanation": "<per-category evidence, calculation, excluded categories, known deal blockers, missing information>",

  "overall_score": <Process Call Score or Offer Call Score as a number, one decimal, per the matching call_type>,

  "trust_score": <1-10 | "N/A" | "Not observable">,
  "pacing_delivery_score": <same>,
  "tonality_score": <same>,
  "active_listening_score": <same>,
  "clarifying_probing_score": <same>,
  "problem_impact_score": <same>,
  "urgency_score": <same>,
  "future_state_score": <same>,
  "objection_prevention_score": <same>,
  "needs_anchor_score": <same>,
  "commitment_before_close_score": <same>,
  "objection_handling_score": <same>,

  "expectation_setting_score": <1-10 | "N/A" | "Not observable">,
  "rapport_connection_score": <same, offer call only>,
  "offer_delivery_score": <same>,
  "objection_handling_declined_score": <same>,
  "urgency_anchor_score": <same>,
  "clear_next_step_score": <same>,
  "objection_boxing_result": "<four-step checklist result and any score cap; empty for process calls>",

  "seller_motivation": "<reason for selling>",
  "seller_urgency": "<urgency and timeline>",
  "property_condition": "<condition/repairs, or 'Not established'>",
  "incomplete_call_reason": "<only if the call could not be evaluated, else empty>",
  "call_summary": "<concise summary of the call and Seller Assessment>",
  "rep_feedback": "<per-section evidence/explanations, plus two strengths and two coaching priorities with example wording>",
  "rapport_connection_summary": "<trust/rapport evidence>",
  "missed_items": ["<specific missed opportunity>", "..."],
  "price_notes": "<prices, asking/offer/target figures discussed>",
  "next_best_action": "<agreed or recommended next step>"
}

For a process_call fill the 12 process keys and leave the offer keys as "N/A". For an offer_call fill the offer keys and set the 12 process keys to "N/A" (except urgency_score etc. which are process-only). If the call is a substantial mixed call, fill both sets and set overall_score to the Process Call Score.
"""


_PROCESS_KEYS = [
    "trust_score", "pacing_delivery_score", "tonality_score", "active_listening_score",
    "clarifying_probing_score", "problem_impact_score", "urgency_score", "future_state_score",
    "objection_prevention_score", "needs_anchor_score", "commitment_before_close_score",
    "objection_handling_score",
]
_OFFER_KEYS = [
    "expectation_setting_score", "rapport_connection_score", "offer_delivery_score",
    "objection_handling_declined_score", "urgency_anchor_score", "clear_next_step_score",
]


def _compute_overall(a: dict, call_type: str | None) -> None:
    """Equal-weight mean of numeric section scores (N/A / Not observable excluded), 1 decimal."""
    ct = (call_type or a.get("call_type") or "").lower()
    keys = _OFFER_KEYS if "offer" in ct else _PROCESS_KEYS
    nums = []
    for k in keys:
        try:
            n = float(a.get(k))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 10:
            nums.append(n)
    if nums:
        a["overall_score"] = round(sum(nums) / len(nums) + 1e-9, 1)


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in NEPQ response")
    return json.loads(text[start:end + 1])


def score_with_nepq(transcript: str, record: dict, call_type: str | None) -> dict:
    """Score a transcript with the active 'rubric_nepq' prompt. Raises if no prompt exists."""
    system_prompt = get_active_prompt_text(NEPQ_PROMPT_TYPE)
    if not system_prompt:
        raise ValueError("No active 'rubric_nepq' prompt found in DB")

    context = [
        f"call_type (supplied): {call_type}" if call_type else "call_type: not supplied",
        f"Call duration (seconds): {record.get('duration') or 'unknown'}",
        "Input: transcript only (no audio). Tonality is 'Not observable'; Pacing and Delivery must still be scored on conversation structure and question delivery.",
        # Sheet data for the equity rules. Not wired to a source yet, so left blank.
        "Sheet data — Zestimate: ",
        "Sheet data — Total Liens: ",
    ]
    user_turn = (
        "Evaluate this call.\n\n" + "\n".join(context) + "\n\n" + _OUTPUT_FORMAT +
        f"\n\nTranscript:\n\n{transcript}"
    )
    raw = complete_text(
        system_prompt,
        [{"role": "user", "content": user_turn}],
        8192,
        anthropic_api_key=ANTHROPIC_API_KEY_RUBRIC,
        json_mode=True,
    )
    try:
        result = _extract_json(raw)
        _compute_overall(result, call_type)
        return result
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("NEPQ returned invalid JSON: %s", raw[:1000])
        raise ValueError(f"NEPQ JSON parse error: {exc}") from exc


def _ensure_header_row(ws: gspread.Worksheet) -> None:
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != "Call ID":
        ws.insert_row(NEPQ_SHEET_HEADERS, index=1)


def append_nepq_to_sheet(gc: gspread.Client, record: dict, a: dict, transcript: str) -> None:
    sh = gc.open_by_key(NEPQ_SHEET_ID)
    try:
        ws = sh.worksheet(NEPQ_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=NEPQ_WORKSHEET_NAME, rows=1000, cols=len(NEPQ_SHEET_HEADERS))
    _ensure_header_row(ws)

    def v(key):
        val = a.get(key)
        return "" if val is None else val

    missed = a.get("missed_items", "")
    if isinstance(missed, list):
        missed = "; ".join(str(i) for i in missed if i)

    classification = a.get("lead_classification") or ""
    lead_expl = a.get("lead_score_explanation") or ""
    if classification:
        lead_expl = f"{classification} ({a.get('lead_assessment') or 'n/a'}). {lead_expl}"

    now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PST")
    record_url = record.get("record_id", "")

    row = [
        record.get("call_id", ""),
        f'=HYPERLINK("{record_url}", "Open in Salesforce")' if record_url else "",
        record.get("audio_url") or record.get("call_link", ""),
        record.get("call_from", ""),
        record.get("call_to", ""),
        "",  # Address — no source wired yet
        record.get("lead_owner", ""),
        record.get("opportunity_owner", ""),
        v("call_type"),
        now,
        record.get("duration", ""),
        "",  # Asking Price — no source wired yet
        "",  # Zestimate — sheet-only per prompt rules; no source wired yet
        "",  # Total Debt / Liens — same
        lead_expl,
        v("lead_score"),
        v("lead_motivation"),
        v("lead_distress"),
        v("lead_urgency"),
        v("lead_timing"),
        v("lead_equity"),
        v("overall_score"),
        v("trust_score"),
        v("pacing_delivery_score"),
        v("tonality_score"),
        v("active_listening_score"),
        v("clarifying_probing_score"),
        v("problem_impact_score"),
        v("urgency_score"),
        v("future_state_score"),
        v("objection_prevention_score"),
        v("needs_anchor_score"),
        v("commitment_before_close_score"),
        v("objection_handling_score"),
        v("seller_motivation"),
        v("seller_urgency"),
        v("property_condition"),
        v("expectation_setting_score"),
        v("rapport_connection_score"),
        v("offer_delivery_score"),
        v("objection_handling_declined_score"),
        v("urgency_anchor_score"),
        v("clear_next_step_score"),
        v("objection_boxing_result"),
        v("incomplete_call_reason"),
        v("call_summary"),
        v("rep_feedback"),
        v("rapport_connection_summary"),
        missed,
        v("price_notes"),
        v("next_best_action"),
        transcript,
    ]
    assert len(row) == len(NEPQ_SHEET_HEADERS), (len(row), len(NEPQ_SHEET_HEADERS))
    ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info("NEPQ row appended | call_id=%s | tab=%s", record.get("call_id"), NEPQ_WORKSHEET_NAME)
