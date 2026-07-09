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


def send_mail(
    settings: Settings, to_addr: str, subject: str, body: str,
    html_body: str | None = None,
    ics_attachment: tuple[str, str, str] | None = None,
    bcc_addrs: tuple[str, ...] = (),
) -> None:
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
    and this HTML part are built from.

    `ics_attachment` (2026-07-09, the operator: "attach a calendar invite also in
    the email that is sent to the participant") is `(filename, ics_text,
    method)` -- `method` is "PUBLISH" or "CANCEL" (see
    app/calendar_sync.py's guest_invite_ics()/guest_cancel_ics(), the only
    two builders of this tuple), echoed into the attachment's own
    Content-Type `method` parameter, which is what lets a calendar app
    recognize which kind of .ics this is without parsing the body itself.
    `add_attachment()` after `set_content()`/`add_alternative()` correctly
    promotes the message to multipart/mixed (text+html alternative, plus
    this attachment) -- standard `email.message.EmailMessage` behavior,
    no manual MIME structuring needed.

    `bcc_addrs` (2026-07-09, the operator: "add as BCC the given email address to
    all mails that go out to the attendees ... so that for some time I can
    watch this to ensure that all is OK") -- a plain tuple of zero or more
    addresses, deliberately NOT read from `settings.bcc_attendee_emails`
    HERE: this function has no notion of "this is an attendee-facing
    email" (it's used for admin copies, password resets, the watchdog
    alert, etc. too), so every ATTENDEE-facing call site passes
    `settings.bcc_attendee_email_list` explicitly instead -- see that
    property's own docstring. Set as a real "Bcc" header field on the
    message object rather than a second `send_message(..., to_addrs=...)`
    argument: `smtplib.send_message()` already reads every recipient
    (To/Cc/Bcc) straight off the message's own headers by default, and
    `EmailMessage` conventionally strips a live "Bcc" header back out of
    what's actually transmitted -- so setting it here is both simpler and
    correct, not a privacy leak to the other recipients."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_addr
    if bcc_addrs:
        msg["Bcc"] = ", ".join(bcc_addrs)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if ics_attachment:
        filename, ics_text, method = ics_attachment
        msg.add_attachment(
            ics_text.encode("utf-8"), maintype="text", subtype="calendar",
            filename=filename, params={"method": method, "charset": "UTF-8"},
        )

    log.debug("sending %r to %s", subject, _masked(to_addr))
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
