"""SMTP sender via mailbox.org. Stdlib smtplib only."""
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


def send_mail(settings: Settings, to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_addr
    msg.set_content(body)

    log.debug("sending %r to %s", subject, _masked(to_addr))
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
