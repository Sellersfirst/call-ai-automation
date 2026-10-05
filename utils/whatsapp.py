"""Small wrapper around Twilio's WhatsApp API for sending approved-template messages."""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request


logger = logging.getLogger(__name__)

TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def send_whatsapp(variables: dict[str, str], to_number: str):
    """Send the approved template to ``to_number`` over WhatsApp.

    ``variables`` maps the template placeholders to their values, e.g.
    ``{"1": "Shelby", "2": "Paul Kim"}`` for ``{{1}}`` and ``{{2}}``.

    Needs ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_WHATSAPP_FROM``
    (the sender) and ``TWILIO_CONTENT_SID`` (the template). ``to_number`` is an
    E.164 number; the ``whatsapp:`` prefix is added when missing.
    """
    if not variables:
        raise ValueError("WhatsApp template variables must be a non-empty dict")
    if not isinstance(to_number, str) or not to_number.strip():
        raise ValueError("Recipient number must be a non-empty string")

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM")
    content_sid = os.environ.get("TWILIO_CONTENT_SID")
    if not (account_sid and auth_token and from_number and content_sid):
        raise RuntimeError(
            "Twilio WhatsApp is not configured. Set TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM and TWILIO_CONTENT_SID."
        )

    to_number = to_number.strip()
    data = urllib.parse.urlencode({
        "From": _as_whatsapp(from_number),
        "To": _as_whatsapp(to_number),
        "ContentSid": content_sid,
        "ContentVariables": json.dumps({k: _clean(v) for k, v in variables.items()}),
    }).encode("utf-8")
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        TWILIO_API_URL.format(sid=account_sid),
        data=data,
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio send to {to_number} failed: {exc.code} {detail}") from exc

    logger.info("WhatsApp sent through Twilio to %s | sid=%s", to_number, result.get("sid"))
    return result


def _clean(value) -> str:
    """Make a value valid as a template variable: WhatsApp rejects newlines,
    tabs and runs of 4+ spaces, and empty values."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text or "N/A"


def _as_whatsapp(number: str) -> str:
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"
