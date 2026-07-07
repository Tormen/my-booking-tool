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


# Shared What/When/Where emoji (2026-07-09, the operator: "Please use yoga emoji
# for the What" -- was the generic pushpin \U0001F4CC before this). Kept as
# module constants so booking_details_text() (plain text) and
# course_recap_html() (HTML, below) can never drift to different emoji or
# ordering -- both read from here.
_WHAT_EMOJI = "\U0001F9D8"  # person in lotus position
_WHEN_EMOJI = "\U0001F550"  # clock face
_WHERE_EMOJI = "\U0001F4CD"  # round pushpin


def booking_details_text(course: Course, occ_date: str) -> str:
    """Shared What/When/Where(+description) block (the operator's requested
    layout, 2026-07-05) for every guest email that tells them about one
    specific confirmed/waitlisted spot -- used by every booking-related
    email (booking confirmed/waitlisted, promoted-from-waitlist,
    cancellation), so none of them can drift apart the way two of them did
    before this was pulled into one function. Description is repeated in
    full via html_to_text() -- this is the plain-text ALTERNATIVE part of
    the email (see emailer.send_mail's html_body param); course_recap_html()
    below is the richer HTML twin most clients will actually render."""
    details = (
        f"{_WHAT_EMOJI} What: {course.title}\n"
        f"{_WHEN_EMOJI} When: {occ_date} {course.time_range_label()}\n"
        f"{_WHERE_EMOJI} Where: {course.location}\n"
    )
    description_text = html_to_text(course.description) if course.description else ""
    return details + (f"\n{description_text}\n" if description_text else "")


def course_recap_html(course: Course, occ_date: str) -> str:
    """HTML twin of booking_details_text() above -- same What/When/Where
    emoji/ordering, plus the operator's own rich-HTML `description` in a
    boxed, background-colored block (2026-07-09, the operator: "format description
    in email as on page ... box the description and put the background
    color (as on the page). ... Can be always the same code that generates
    this for the page or email."). Deliberately INLINE-styled, not
    dependent on any CSS class/`<style>` block: the app's own pages could
    use either, but an HTML email can't rely on a `<style>` block or class
    surviving every mail client's sanitizer, so inline is the one style
    that reliably renders identically in both places. See
    app/webapp.py::_course_recap_html, a thin wrapper around this exact
    function, for the page-side call sites (booking confirmation, the
    cancel-confirmation pages)."""
    esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    desc_html = (
        '<div style="background:#fdf8ef;border:1px solid #eee0c0;border-radius:8px;'
        f'padding:1em 1.2em;margin:.6em 0 0">{course.description}</div>'
    ) if course.description else ""
    return (
        '<div style="background:#f4f7f4;border:1px solid #ddd;border-radius:8px;'
        'padding:1em 1.2em;margin:1em 0;font-family:sans-serif">'
        f'<p style="margin:.3em 0"><b>{_WHAT_EMOJI} What:</b> {esc(course.title)}</p>'
        f'<p style="margin:.3em 0"><b>{_WHEN_EMOJI} When:</b> {esc(occ_date)} {esc(course.time_range_label())}</p>'
        f'<p style="margin:.3em 0"><b>{_WHERE_EMOJI} Where:</b> {esc(course.location)}</p>'
        f"{desc_html}"
        "</div>"
    )


def intro_html(text: str) -> str:
    """The FIRST sentence of every HTML booking/cancellation/promotion
    email (2026-07-10, the operator: "please make the first sentance in email a
    bit more visible (bold mayb and for sure larger font size, compare to
    all other things in the email and email header) ... same of course
    for ALL emails") -- bigger and bolder than the recap/detail text below
    it (and than a typical mail client's own From/Subject header line), so
    the single most important fact (booked, waitlisted, canceled,
    promoted...) is the first thing a skimming reader's eye lands on.

    `text` is a plain string, escaped HERE (not by the caller) -- unlike
    html_email_body()'s own `inner_html` param, which callers assemble
    from several already-escaped pieces (course_recap_html() etc.) and is
    deliberately trusted as-is, this narrower helper always wraps exactly
    ONE sentence built from a mix of fixed wording and (in the admin-
    cancellation and promotion emails) a guest-supplied name -- escaping
    inside this one shared helper means every call site gets it for free
    instead of relying on each one to remember."""
    return f'<p style="font-size:1.25em;font-weight:bold;margin:0 0 .5em">{html.escape(text, quote=True)}</p>'


def html_email_body(inner_html: str) -> str:
    """Minimal, portable HTML shell (2026-07-09) every HTML email in this
    app is wrapped in -- no external stylesheet/JS, just a plain
    font/color/line-height baseline, since mail clients vary wildly in
    what they strip from a `<style>` block. `inner_html` is whatever
    per-email content the caller built (course_recap_html() plus its own
    surrounding paragraphs/links)."""
    return (
        '<html><body style="font-family:sans-serif;color:#222;line-height:1.5;margin:0;padding:0">'
        f"{inner_html}"
        "</body></html>"
    )


def send_cancellation_emails(
    settings: Settings, course: Course, occ_date: str, user, canceled_by: str, message: str,
    ics_attachment: tuple[str, str, str] | None = None,
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

    `ics_attachment` (2026-07-09, the operator: "AND CANCEL-ics as well please.
    Let's be nice :)") is the caller's already-built (filename, ics_text,
    "CANCEL") tuple from app.calendar_sync.guest_cancel_ics() -- built by
    the CALLER, not here, since this module deliberately has no dependency
    on app.calendar_sync (which itself imports FROM this module, for
    html_to_text -- importing back would be a cycle). Only ever attached to
    the PARTICIPANT's copy, never the admin's: the admin's own calendar
    already gets the authoritative update straight from CalDAV
    (calendar_sync.sync_occurrence), so a second, personal "delete this
    from your calendar" attachment on their own admin-copy email would be
    redundant at best, confusing at worst."""
    details = booking_details_text(course, occ_date)
    recap_html = course_recap_html(course, occ_date)
    subject = f"Canceled: {course.title} on {occ_date}"
    reason_block = f"\nMessage: {message}\n" if message else ""
    reason_html = f"<p>Message: {html.escape(message, quote=True)}</p>" if message else ""
    my_url = f"{settings.base_url}/my"
    if user:
        participant_who = "You" if canceled_by == "guest" else "The host"
        # 2026-07-10, the operator: "With the reschedule button the email could
        # also contain it: If this was a mistake... The what can be a link
        # to the booking page for this course" -- participant-facing only,
        # the admin copy below has no need to rebook.
        rebook_url = f"{settings.base_url}/book/{course.shortname}"
        send_mail(
            settings, user.email, subject,
            f"{participant_who} canceled this booking:\n\n{details}\n{reason_block}"
            f"Manage your bookings: {my_url}\n"
            f"If this was a mistake, you can book again here: {rebook_url}\n",
            html_body=html_email_body(
                intro_html(f"{participant_who} canceled this booking:") + f"{recap_html}{reason_html}"
                f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
                f'<p>If this was a mistake, you can book again here: '
                f'<a href="{rebook_url}">{rebook_url}</a></p>'
            ),
            ics_attachment=ics_attachment,
        )
    admin_who = "You" if canceled_by == "host" else (f"{user.name} <{user.email}>" if user else "The guest")
    send_mail(
        settings, settings.admin_email, subject,
        f"{admin_who} canceled this booking:\n\n{details}\n{reason_block}",
        html_body=html_email_body(intro_html(f"{admin_who} canceled this booking:") + f"{recap_html}{reason_html}"),
    )
