"""Shared provider selection and text completion for Claude and OpenAI."""

import logging
import os

import anthropic
import httpx

from config.config import ANTHROPIC_API_KEY
from config.database import get_connection

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def get_ai_provider() -> str:
    """Read the configured provider from the backend's PostgreSQL config row."""
    with get_connection() as conn:
        row = conn.execute("SELECT ai_provider FROM config WHERE id=1").fetchone()
    provider = row["ai_provider"] if row else "claude"
    return provider if provider in ("claude", "openai") else "claude"


def save_ai_provider(provider: str) -> str:
    if provider not in ("claude", "openai"):
        raise ValueError("provider must be 'claude' or 'openai'")
    with get_connection() as conn:
        conn.execute("UPDATE config SET ai_provider=%s WHERE id=1", (provider,))
        conn.commit()
    return provider


def get_provider_setting() -> str:
    """Return the current setting for the authenticated settings endpoint."""
    return get_ai_provider()


def complete_text(
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    *,
    anthropic_api_key: str | None = None,
    json_mode: bool = False,
) -> str:
    """Run equivalent system + chat messages through the configured provider."""
    provider = get_ai_provider()
    model = OPENAI_MODEL if provider == "openai" else CLAUDE_MODEL
    logger.info("AI completion provider=%s model=%s", provider, model)
    if provider == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        payload = {
            "model": OPENAI_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
            timeout=120,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            code = error.get("code") or "unknown"
            error_type = error.get("type") or "unknown"
            message = error.get("message") or response.reason_phrase
            request_id = response.headers.get("x-request-id")
            logger.error(
                "OpenAI API error status=%s code=%s type=%s request_id=%s",
                response.status_code, code, error_type, request_id,
            )
            raise RuntimeError(
                f"OpenAI API returned HTTP {response.status_code} "
                f"(code={code}, type={error_type}): {message}"
            ) from exc
        data = response.json()
        return (data["choices"][0]["message"].get("content") or "").strip()

    api_key = anthropic_api_key or ANTHROPIC_API_KEY
    response = anthropic.Anthropic(api_key=api_key).messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )
    return (response.content[0].text or "").strip()
