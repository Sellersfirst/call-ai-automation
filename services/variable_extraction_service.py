import json
import logging
import re

import anthropic

from config.config import ANTHROPIC_API_KEY

logger = logging.getLogger("variable_extraction")

CLAUDE_MODEL = "claude-sonnet-4-6"


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


def extract_variables(prompt_text: str, transcript: str, summary: str, variable_names: list[str]) -> dict:
    """
    Ask Claude to derive the requested variables from a call transcript/summary.
    Returns {variable_name: value_or_None} with exactly the requested keys,
    or {} on any failure (API error, unparseable response).
    """
    if not variable_names:
        return {}

    system = (
        f"{prompt_text}\n\n"
        "You must respond with ONLY a valid JSON object (no markdown fences, no commentary) "
        f"with exactly these keys: {', '.join(variable_names)}. "
        "If a value cannot be determined from the call, use null for that key."
    )
    user_content = f"Call summary:\n{summary or 'N/A'}\n\nTranscript:\n{transcript or 'No transcript'}"

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = (message.content[0].text or "").strip()
        parsed = json.loads(_extract_json_text(raw))
        if not isinstance(parsed, dict):
            raise ValueError("Model did not return a JSON object")
    except Exception as exc:
        logger.error(f"Variable extraction failed: {exc}", exc_info=True)
        return {}

    return {name: parsed.get(name) for name in variable_names}
