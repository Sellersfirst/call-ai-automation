"""Small wrapper around Resend for sending plain-text emails."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage


logger = logging.getLogger(__name__)

DEFAULT_FROM_EMAIL = "onboarding@resend.dev"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def send_email(content: str, to_email: str, subject: str = "call rubrics"):
    """Send ``content`` as a plain-text email using Resend or Gmail SMTP.

    The Resend API key is read from ``RESEND_API_KEY`` and the sender can be
    overridden with ``RESEND_FROM_EMAIL``. When no Resend API key is present,
    Gmail SMTP is used with ``GMAIL_SMTP_EMAIL`` and
    ``GMAIL_SMTP_APP_PASSWORD`` (a Google App Password, not the account
    password). ``SMTP_USERNAME`` and ``SMTP_PASSWORD`` are accepted as generic
    alternatives.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Email content must be a non-empty string")
    if not isinstance(to_email, str) or not to_email.strip():
        raise ValueError("Recipient email must be a non-empty string")

    api_key = os.environ.get("RESEND_API_KEY")
    if api_key:
        # Import only when an email is actually sent so unrelated application
        # paths do not require the Resend client to be initialized.
        import resend

        resend.api_key = api_key
        response = resend.Emails.send({
            "from": os.environ.get("RESEND_FROM_EMAIL", DEFAULT_FROM_EMAIL),
            "to": [to_email.strip()],
            "subject": subject,
            "text": content,
        })
        logger.info("Email sent through Resend to %s | subject=%s", to_email, subject)
        return response

    return _send_via_gmail_smtp(content, to_email.strip(), subject)


def _send_via_gmail_smtp(content: str, to_email: str, subject: str):
    """Send an email through Gmail's TLS SMTP endpoint."""
    username = os.environ.get("GMAIL_SMTP_EMAIL") or os.environ.get("SMTP_USERNAME")
    password = os.environ.get("GMAIL_SMTP_APP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "RESEND_API_KEY is not configured. Set GMAIL_SMTP_EMAIL and "
            "GMAIL_SMTP_APP_PASSWORD to use the Gmail SMTP fallback."
        )

    message = EmailMessage()
    message["From"] = os.environ.get("GMAIL_SMTP_FROM_EMAIL", username)
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
