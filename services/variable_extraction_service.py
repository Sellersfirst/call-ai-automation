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


def extract_variables(
    prompt_text: str,
    transcript: str,
    summary: str,
    variable_names: list[str],
    variable_descriptions: dict[str, str] | None = None,
) -> dict:
    """
    Ask Claude to derive the requested variables from a call transcript/summary.
    Returns {variable_name: value_or_None} with exactly the requested keys,
    or {} on any failure (API error, unparseable response).
    """
    if not variable_names:
        return {}

    variable_descriptions = variable_descriptions or {}
    variables_block = "\n".join(
        f"- {name}: {variable_descriptions[name]}" if variable_descriptions.get(name) else f"- {name}"
        for name in variable_names
    )

    user_content = (
        f"Variables to extract:\n{variables_block}\n\n"
        f"Call summary:\n{summary or 'N/A'}\n\n"
        f"Transcript:\n{transcript or 'No transcript'}\n\n"
        "Respond with ONLY a valid JSON object (no markdown fences, no commentary) "
        f"with exactly these keys: {', '.join(variable_names)}. "
        "If a value cannot be determined from the call, use null for that key."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=prompt_text,
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
