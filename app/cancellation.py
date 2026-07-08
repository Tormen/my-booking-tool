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


def booking_details_text(course: Course, occ_date: str, message: str = "") -> str:
    """Shared What/When/Where(+message)(+description) block (the operator's
    requested layout, 2026-07-05) for every guest email that tells them
    about one specific confirmed/waitlisted spot -- used by every booking-
    related email (booking confirmed/waitlisted, promoted-from-waitlist,
    cancellation), so none of them can drift apart the way two of them did
    before this was pulled into one function. Description is repeated in
    full via html_to_text() -- this is the plain-text ALTERNATIVE part of
    the email (see emailer.send_mail's html_body param); course_recap_html()
    below is the richer HTML twin most clients will actually render.

    `message` (2026-07-11, the operator, screenshot of a Reinstated email showing
    "Message: you are on again" printed AFTER the whole course description:
    "please place the msg block ABOVE the description and if there is no
    message, leave it out") is the optional free-text comment Cancel/
    Reinstate's own dialogs collect -- ONLY these two callers pass it;
    every other booking email (confirmed/waitlisted, promoted-from-
    waitlist) has no such concept and simply never passes one, so this
    stays a no-op for them, same as before this parameter existed. Blank
    (the default) omits the line entirely, exactly like the old separate
    `reason_block`/`reason_html` this replaces did in
    send_cancellation_emails()/send_reinstatement_emails()."""
    details = (
        f"{_WHAT_EMOJI} What: {course.title}\n"
        f"{_WHEN_EMOJI} When: {occ_date} {course.time_range_label()}\n"
        f"{_WHERE_EMOJI} Where: {course.location}\n"
    )
    message_block = f"\nMessage: {message}\n" if message else ""
    description_text = html_to_text(course.description) if course.description else ""
    return details + message_block + (f"\n{description_text}\n" if description_text else "")


def course_recap_html(course: Course, occ_date: str, message: str = "") -> str:
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
    cancel-confirmation pages) -- none of which pass `message` (there's
    nothing to show yet on a page asking the guest to type one).

    `message` (2026-07-11, same request as booking_details_text()'s own
    docstring above -- see that one for the full quote) is rendered via
    message_html() and placed between the Where line and the description
    box, i.e. ABOVE the description, not after it like the old separate
    `reason_html` concatenation used to put it. Blank omits it entirely."""
    esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    message_block = message_html(message) if message else ""
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
        f"{message_block}"
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


def greeting_html(name: str) -> str:
    """"Dear NAME," as its own plain (non-bold) paragraph, meant to sit
    BEFORE intro_html()'s bold status sentence -- 2026-07-08, the operator: after
    _send_confirm_email() got a "Dear NAME," greeting (2026-07-07) and the
    guest-facing booking-result/cancellation/reinstatement emails didn't,
    asked "they should now all start with 'Dear <NAME>', correct?". They
    didn't; this closes that gap for those three, deliberately NOT for the
    admin-facing copies (admin_email) or the party-admin summary, which are
    receipts to the operator's own inbox, not letters to a guest -- see
    each call site's own comment. Kept separate from intro_html() rather
    than folded into it: the greeting is a normal-weight salutation, not
    the bold, larger "most important fact" line intro_html() exists for,
    and not every intro_html() caller (e.g. the admin-facing emails above)
    wants a greeting at all."""
    return f'<p style="margin:0 0 .5em">Dear {html.escape(name, quote=True)},</p>'


def message_html(message: str) -> str:
    """The optional free-text comment collected by Cancel's and Reinstate's
    own confirm dialogs (2026-07-10, the operator: "Reinstate should, LIKE CANCEL,
    also ask for a COMMENT to be sent with the email to the other" then
    "the message from the comment field should always be displayed like
    this: light grey background with the message") -- boxed the same way
    course_recap_html() boxes the operator's own `description` (border +
    radius + padding), but in a neutral light grey rather than that box's
    cream (`#fdf8ef`), so a guest/host-typed comment reads as visually
    distinct from the operator-authored course description. Blank message
    renders nothing at all, same as the old plain-`<p>` version this
    replaces -- every call site already only calls this when there IS a
    message to show."""
    return (
        '<div style="background:#f2f2f2;border:1px solid #ddd;border-radius:8px;'
        f'padding:.8em 1.2em;margin:.6em 0"><b>Message:</b> {html.escape(message, quote=True)}</div>'
    )


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
    registration_id: str, reinstate_token: str | None = None,
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

    `registration_id` builds the host's no-login `/host-reinstate/<id>`
    magic link (2026-07-10, the operator: "for /my and /admin ... this POPUP
    should be used ... Only from the email there will be a single page
    for this ... WHAT, WHEN, WHERE like in the confirmation email", "Both"
    -- participant AND admin copies get a reinstate link) -- same trust
    model as the existing `/host-cancel/<id>` link (gated purely by this
    being an unguessable uuid4, no separate secret; see host_cancel()'s
    own docstring). `reinstate_token` is the PLAINTEXT of a token the
    caller freshly minted right before calling this (see Store.cancel()'s
    own `reinstate_token_hash` param for why it can't be the guest's
    original cancel token) -- builds the participant's `/reinstate/<token>`
    link the same way `/cancel/<token>` itself is built. `None` (the
    default) omits the participant's reinstate line entirely, e.g. for a
    caller that has no user/email to send it to anyway.

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
    redundant at best, confusing at worst.

    `message`, if any, is threaded straight into booking_details_text()/
    course_recap_html() (2026-07-11) rather than concatenated on
    afterward -- see those two functions' own docstrings for why (the operator:
    the old layout put "Message: ..." AFTER the whole course description;
    it belongs ABOVE it instead, and should vanish entirely when blank)."""
    details = booking_details_text(course, occ_date, message)
    recap_html = course_recap_html(course, occ_date, message)
    subject = f"Canceled: {course.title} on {occ_date}"
    my_url = f"{settings.base_url}/my"
    host_reinstate_url = f"{settings.base_url}/host-reinstate/{registration_id}"
    if user:
        participant_who = "You" if canceled_by == "guest" else "The host"
        # 2026-07-10: superseded the earlier plain "book again" link with a
        # real reinstate-this-exact-booking one, now that a dedicated
        # no-login page exists for it -- reinstating (same registration,
        # same party) is strictly better than starting a fresh booking
        # from scratch, so there's no reason to offer both.
        reinstate_line = ""
        reinstate_line_html = ""
        if reinstate_token:
            guest_reinstate_url = f"{settings.base_url}/reinstate/{reinstate_token}"
            reinstate_line = f"If this was a mistake, you can reinstate it here: {guest_reinstate_url}\n"
            reinstate_line_html = (
                f'<p>If this was a mistake, you can reinstate it here: '
                f'<a href="{guest_reinstate_url}">{guest_reinstate_url}</a></p>'
            )
        # 2026-07-08, the operator: guest-facing emails should greet by name, same
        # as _send_confirm_email() already did -- see greeting_html()'s own
        # docstring. Participant copy only, never the admin copy below.
        send_mail(
            settings, user.email, subject,
            f"Dear {user.name},\n\n"
            f"{participant_who} canceled this booking:\n\n{details}\n"
            f"Manage your bookings: {my_url}\n"
            f"{reinstate_line}",
            html_body=html_email_body(
                greeting_html(user.name) + intro_html(f"{participant_who} canceled this booking:") + recap_html
                + f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
                + reinstate_line_html
            ),
            ics_attachment=ics_attachment,
        )
    admin_who = "You" if canceled_by == "host" else (f"{user.name} <{user.email}>" if user else "The guest")
    send_mail(
        settings, settings.admin_email, subject,
        f"{admin_who} canceled this booking:\n\n{details}\n"
        f"Reinstate this booking: {host_reinstate_url}\n",
        html_body=html_email_body(
            intro_html(f"{admin_who} canceled this booking:") + recap_html
            + f'<p>Reinstate this booking: <a href="{host_reinstate_url}">{host_reinstate_url}</a></p>'
        ),
    )


def send_reinstatement_emails(
    settings: Settings, course: Course, occ_date: str, user, confirmed: bool, reinstated_by: str, message: str,
    ics_attachment: tuple[str, str, str] | None = None,
) -> None:
    """The "undo a cancel" twin of send_cancellation_emails() above
    (2026-07-10, the operator: "there should be then a reschedule button for
    canceled meetings which time (WHEN) is in the future"; clarified in
    discussion that this means undoing the cancellation for the SAME
    occurrence, not moving to a different one, and that both the guest's
    own /my page and the host's /admin page should offer it). Same
    notify-both-sides standing default as every other registration-status
    email (see send_cancellation_emails's own docstring) -- whoever DIDN'T
    click the button is the one most likely to be surprised by this.

    `confirmed` is a plain bool (True = re-admitted straight to confirmed,
    False = landed back on the waitlist) rather than one of
    app.storage's STATUS_* strings, deliberately -- this module has no
    other dependency on app.storage and importing just for this one
    comparison isn't worth the coupling. `reinstated_by` is "guest" or
    "host", same vocabulary as send_cancellation_emails's `canceled_by`.

    `message` (2026-07-10, the operator: "Reinstate should, LIKE CANCEL, also ask
    for a COMMENT to be sent with the email to the other [side]") is the
    same optional free-text reason Cancel's own dialog collects -- threaded
    straight into booking_details_text()/course_recap_html() (2026-07-11,
    same as send_cancellation_emails's own -- see that function's
    docstring and those two functions' own docstrings for why: it belongs
    ABOVE the course description, not after it), blank omits it entirely.

    `ics_attachment`, like send_cancellation_emails's own, is built by the
    CALLER (app.calendar_sync.guest_invite_ics, only when `confirmed` is
    True -- a still-waitlisted reinstatement has no real calendar slot to
    hand out yet, same rule the original booking flow already follows) and
    only ever attached to the participant's copy."""
    details = booking_details_text(course, occ_date, message)
    recap_html = course_recap_html(course, occ_date, message)
    subject = f"Reinstated: {course.title} on {occ_date}"
    my_url = f"{settings.base_url}/my"
    status_phrase = "you're confirmed again" if confirmed else "you're back on the waitlist"
    if user:
        participant_who = "You" if reinstated_by == "guest" else "The host"
        intro = f"{participant_who} reinstated this booking -- {status_phrase}:"
        # 2026-07-08, the operator: same "Dear NAME," greeting as
        # send_cancellation_emails' own participant copy above.
        send_mail(
            settings, user.email, subject,
            f"Dear {user.name},\n\n{intro}\n\n{details}\nManage your bookings: {my_url}\n",
            html_body=html_email_body(
                greeting_html(user.name) + intro_html(intro) + recap_html
                + f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
            ),
            ics_attachment=ics_attachment,
        )
    admin_who = "You" if reinstated_by == "host" else (f"{user.name} <{user.email}>" if user else "The guest")
    admin_intro = f"{admin_who} reinstated this booking -- {status_phrase}:"
    send_mail(
        settings, settings.admin_email, subject,
        f"{admin_intro}\n\n{details}\n",
        html_body=html_email_body(intro_html(admin_intro) + recap_html),
    )
