"""Cancellation email composition -- factored out of app/webapp.py's App
class (2026-07-06) so both the web admin's /admin/cancel path AND the
`my-bt cancel` CLI command (scripts/my-bt) trigger the EXACT same emails
from ONE place, rather than the CLI reimplementing what App._booking_details_text/
_send_cancellation_emails already do. App methods take no arguments beyond
`self` (settings/store live on the instance), which is awkward to reuse from
a standalone CLI script that has no App/WSGI machinery at all -- these
module-level functions take `settings` explicitly instead, so they work
identically whether called from within App or from scripts/my-bt.

webapp.py's App._booking_details_text/_send_cancellation_emails are now thin
wrappers that just forward to these -- see their docstrings. This refactor
changes no behavior: same emails, same recipients, same content.
"""
from __future__ import annotations

import html
import re

from .config import Course, Settings
from .emailer import send_mail

# Moved here from app/webapp.py (2026-07-06, alongside the rest of this
# module) since booking_details_text() below is this function's only
# caller -- webapp.py re-exports it as `_html_to_text` (see the comment
# there) so the existing HtmlToTextTest suite keeps working unchanged.
_HTML_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_HTML_A_RE = re.compile(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HTML_BLOCK_RE = re.compile(r"</?(p|div|ul|ol|br)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Best-effort HTML -> plain text for course.description (operator-
    authored rich text -- see app/config.py's docstring on that field) when
    it needs to go into a plain-text email: app/emailer.py's send_mail only
    ever calls msg.set_content(body), there's no HTML alternative part. This
    is NOT a general HTML sanitizer/renderer -- it only handles the tags a
    course description realistically uses (p/div/ul/ol/li/br/b/i/u/a), which
    is all settings.toml.example and every real course description in
    the maintainer's local notes's deployment actually contain.
    """
    text = _HTML_A_RE.sub(lambda m: f"{_HTML_TAG_RE.sub('', m.group(2))} ({m.group(1)})", markup)
    text = _HTML_LI_RE.sub(lambda m: f"- {_HTML_TAG_RE.sub('', m.group(1)).strip()}\n", text)
    text = _HTML_BLOCK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def booking_details_text(course: Course, occ_date: str) -> str:
    """Shared What/When/Where(+description) block (the operator's requested
    layout, 2026-07-05) for every guest email that tells them about one
    specific confirmed/waitlisted spot -- used by every booking-related
    email (booking confirmed/waitlisted, promoted-from-waitlist,
    cancellation), so none of them can drift apart the way two of them did
    before this was pulled into one function. Description is repeated in
    full via html_to_text(), since send_mail is plain-text only -- so a
    guest never has to go back to the booking page to see what they signed
    up for."""
    details = (
        f"\U0001F4CC What: {course.title}\n"
        f"\U0001F550 When: {occ_date} {course.time_range_label()}\n"
        f"\U0001F4CD Where: {course.location}\n"
    )
    description_text = html_to_text(course.description) if course.description else ""
    return details + (f"\n{description_text}\n" if description_text else "")


def send_cancellation_emails(
    settings: Settings, course: Course, occ_date: str, user, canceled_by: str, message: str
) -> None:
    """Every cancellation -- whichever of the four paths triggers it (the
    guest's one-click link from their booking email, the guest's own /my
    dialog, the host's /admin dialog, or `my-bt cancel`) -- emails BOTH the
    participant and the admin, always, using the same What/When/Where
    layout as every other booking email (booking_details_text()). This is
    the standing default for any email about one specific booking (see
    SOLUTION-DESIGN.md's comment log, 2026-07-05).

    Notifying both sides regardless of who acted isn't just politeness:
    it's the only way either side would notice a cancellation made on
    their behalf without their knowledge -- e.g. someone getting into a
    guest's /my account, or into /admin, and canceling something that
    isn't theirs to cancel. `canceled_by` is "guest" or "host", same
    vocabulary as Store.cancel()'s own parameter -- `my-bt cancel` uses
    "host", the same as the web admin's /admin/cancel, since both are the
    operator acting on a guest's behalf.
    """
    details = booking_details_text(course, occ_date)
    subject = f"Canceled: {course.title} on {occ_date}"
    reason_block = f"\nMessage: {message}\n" if message else ""
    if user:
        participant_who = "You" if canceled_by == "guest" else "The host"
        send_mail(
            settings, user.email, subject,
            f"{participant_who} canceled this booking:\n\n{details}\n{reason_block}"
            f"Manage your bookings any time: {settings.base_url}/my\n",
        )
    admin_who = "You" if canceled_by == "host" else (f"{user.name} <{user.email}>" if user else "The guest")
    send_mail(
        settings, settings.admin_email, subject,
        f"{admin_who} canceled this booking:\n\n{details}\n{reason_block}",
    )
