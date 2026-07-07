"""SMTP sender -- works with any SMTP provider configured in settings.toml
(host/port/username/password), e.g. mailbox.org, Gmail, etc. Stdlib
smtplib only."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger("my_booking.emailer")


def _masked(addr: str) -> str:
    """For DEBUG logging only -- enough to spot which domain/pattern is
    involved without writing a guest's actual address into the log."""
    local, _, domain = addr.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def send_mail(settings: Settings, to_addr: str, subject: str, body: str, html_body: str | None = None) -> None:
    """`html_body` (2026-07-09, the operator: "format description in email as on
    page ... box the description and put the background color (as on the
    page)") is optional -- omitting it (every pre-existing call site that
    hasn't been updated yet) sends the exact same plain-text-only email as
    before. When given, this becomes a standard multipart/alternative
    message: `body` stays the plain-text part (still shown by text-only
    clients, or if a recipient's client prefers plain text), and
    `html_body` is added as the richer alternative most clients will
    actually render -- see app/cancellation.py's course_recap_html()/
    html_email_body() for the shared generator both the app's own pages
    and this HTML part are built from."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_addr
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    log.debug("sending %r to %s", subject, _masked(to_addr))
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
