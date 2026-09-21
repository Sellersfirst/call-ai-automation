"""Small wrapper around Resend for sending plain-text emails."""

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage


logger = logging.getLogger(__name__)

DEFAULT_FROM_EMAIL = "onboarding@resend.dev"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def send_email(content: str, to_email: str, subject: str = "call rubrics"):
    """Send ``content`` as a plain-text email using Resend, falling back to
    Gmail SMTP if Resend is not configured or the send fails.

    The Resend API key is read from ``RESEND_API_KEY`` and the sender can be
    overridden with ``RESEND_FROM_EMAIL``. When no Resend API key is present,
    or when a Resend send raises an error (e.g. the sandbox sender rejecting
    a recipient), Gmail SMTP is used with ``GMAIL_SENDER_EMAIL`` and
    ``GMAIL_APP_PASSWORD`` (a Google App Password, not the account password).
    The older ``GMAIL_SMTP_EMAIL``/``GMAIL_SMTP_APP_PASSWORD`` and generic
    SMTP variable names are also accepted.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Email content must be a non-empty string")
    if not isinstance(to_email, str) or not to_email.strip():
        raise ValueError("Recipient email must be a non-empty string")

    to_email = to_email.strip()

    # Brevo goes over HTTPS, so it works on hosts (e.g. Railway) that block SMTP.
    if os.environ.get("BREVO_API_KEY"):
        try:
            return _send_via_brevo(content, to_email, subject)
        except Exception as exc:
            logger.error("Brevo send to %s failed: %s", to_email, exc)

    api_key = os.environ.get("RESEND_API_KEY")
    if api_key:
        # Import only when an email is actually sent so unrelated application
        # paths do not require the Resend client to be initialized.
        import resend

        resend.api_key = api_key
        try:
            response = resend.Emails.send({
                "from": os.environ.get("RESEND_FROM_EMAIL", DEFAULT_FROM_EMAIL),
                "to": [to_email],
                "subject": subject,
                "text": content,
            })
            logger.info("Email sent through Resend to %s | subject=%s", to_email, subject)
            return response
        except Exception as exc:
            logger.warning(
                "Resend send to %s failed, falling back to Gmail SMTP: %s",
                to_email, exc,
            )

    return _send_via_gmail_smtp(content, to_email, subject)


def _send_via_brevo(content: str, to_email: str, subject: str):
    """Send a plain-text email through Brevo's HTTPS API.

    Needs ``BREVO_API_KEY`` and ``BREVO_SENDER_EMAIL`` (a sender verified in
    Brevo); ``BREVO_SENDER_NAME`` is optional.
    """
    sender_email = os.environ.get("BREVO_SENDER_EMAIL")
    if not sender_email:
        raise RuntimeError("BREVO_SENDER_EMAIL is not set")

    payload = {
        "sender": {
            "email": sender_email,
            "name": os.environ.get("BREVO_SENDER_NAME", "Sellers First"),
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": content,
    }
    request = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": os.environ["BREVO_API_KEY"],
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8") or "{}")

    logger.info("Email sent through Brevo to %s | subject=%s", to_email, subject)
    return body


def _send_via_gmail_smtp(content: str, to_email: str, subject: str):
    """Send an email through Gmail's TLS SMTP endpoint."""
    username = (
        os.environ.get("GMAIL_SENDER_EMAIL")
        or os.environ.get("GMAIL_SMTP_EMAIL")
        or os.environ.get("SMTP_USERNAME")
    )
    password = (
        os.environ.get("GMAIL_APP_PASSWORD")
        or os.environ.get("GMAIL_SMTP_APP_PASSWORD")
        or os.environ.get("SMTP_PASSWORD")
    )
    if not username or not password:
        raise RuntimeError(
            "Gmail SMTP fallback is not configured. Set GMAIL_SENDER_EMAIL and "
            "GMAIL_APP_PASSWORD to send email."
        )

    message = EmailMessage()
    message["From"] = os.environ.get("GMAIL_SENDER_EMAIL", username)
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(content)

    host = os.environ.get("SMTP_HOST", GMAIL_SMTP_HOST)
    port = int(os.environ.get("SMTP_PORT", str(GMAIL_SMTP_PORT)))
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.send_message(message)

    logger.info("Email sent through Gmail SMTP to %s | subject=%s", to_email, subject)
    return {"provider": "gmail_smtp", "recipient": to_email}
