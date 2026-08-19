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
from .email_templates import load_email_template, render_template
from .emailer import send_mail

# Moved here from app/webapp.py (2026-07-06, alongside the rest of this
# module) since booking_details_text() below is this function's only
# caller -- webapp.py re-exports it as `_html_to_text` (see the comment
# there) so the existing HtmlToTextTest suite keeps working unchanged.
_HTML_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_HTML_A_RE = re.compile(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HTML_BLOCK_RE = re.compile(r"</?(p|div|ul|ol|br)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def host_cancel_url(settings: Settings, registration_id: str) -> str:
    """The no-login "magic link" that cancels ONE registration (see
    app/webapp.py::host_cancel and /host-cancel/<reg_id>) -- gated purely
    by the unguessable uuid4 registration_id, host/operator-only, same
    trust boundary as the calendar event's own per-participant cancel
    lines. Single source of truth for this URL so the calendar event body
    (app/calendar_sync.py) and the host booking-notification email
    (app/webapp.py) can never format it two different ways."""
    return f"{settings.base_url}/host-cancel/{registration_id}"


def host_cancel_occurrence_url(settings: Settings, course_shortname: str, occurrence_date: str) -> str:
    """The no-login "magic link" that cancels an ENTIRE session at once
    (every confirmed/waitlisted/pending registration for one course
    occurrence -- see app/webapp.py::host_cancel_occurrence and
    app.cancel_flow.cancel_occurrence). `occurrence_date` is an ISO date
    string ("YYYY-MM-DD"). Same single-source-of-truth reasoning as
    host_cancel_url() above."""
    return f"{settings.base_url}/host-cancel-occurrence/{course_shortname}/{occurrence_date}"


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


# Shared What/When/Where emoji (2026-07-09: a yoga emoji for the What --
# was the generic pushpin \U0001F4CC before this). Kept as
# module constants so booking_details_text() (plain text) and
# course_recap_html() (HTML, below) can never drift to different emoji or
# ordering -- both read from here.
_WHAT_EMOJI = "\U0001F9D8"  # person in lotus position
_WHEN_EMOJI = "\U0001F550"  # clock face
_WHERE_EMOJI = "\U0001F4CD"  # round pushpin


def booking_details_text(
    course: Course, occ_date: str, message: str = "",
    *, emoji: bool = True, include_description: bool = True,
) -> str:
    """Shared What/When/Where(+message)(+description) block (this
    layout, 2026-07-05) for every guest email that tells them
    about one specific confirmed/waitlisted spot -- used by every booking-
    related email (booking confirmed/waitlisted, promoted-from-waitlist,
    cancellation), so none of them can drift apart the way two of them did
    before this was pulled into one function. Description is repeated in
    full via html_to_text() -- this is the plain-text ALTERNATIVE part of
    the email (see emailer.send_mail's html_body param); course_recap_html()
    below is the richer HTML twin most clients will actually render.

    `message` (2026-07-11: a Reinstated email once showed "Message: you
    are on again" printed AFTER the whole course description -- fixed to
    place the message block ABOVE the description, and leave it out
    entirely when there is no message) is the optional free-text comment Cancel/
    Reinstate's own dialogs collect -- ONLY these two callers pass it;
    every other booking email (confirmed/waitlisted, promoted-from-
    waitlist) has no such concept and simply never passes one, so this
    stays a no-op for them, same as before this parameter existed. Blank
    (the default) omits the line entirely, exactly like the old separate
    `reason_block`/`reason_html` this replaces did in
    send_cancellation_emails()/send_reinstatement_emails()."""
    what, when, where = (
        (f"{_WHAT_EMOJI} ", f"{_WHEN_EMOJI} ", f"{_WHERE_EMOJI} ") if emoji else ("", "", "")
    )
    details = (
        f"{what}What: {course.title}\n"
        f"{when}When: {occ_date} {course.time_range_label_for(occ_date)}\n"
        f"{where}Where: {course.location}\n"
    )
    # 2026-07-16: exceptional per-date time changes (Course.
    # date_overrides) get an automatic ATTENTION line here -- looked up
    # straight from `course`/`occ_date`, so EVERY caller of this function
    # (booking confirmed/waitlisted, promoted-from-waitlist, cancel,
    # reinstate) shows it for free, with no per-call-site plumbing.
    # Deliberately a SEPARATE line from the `message` param above (a
    # human-typed cancel/reinstate reason) -- the two can coexist.
    attention = course.override_message_for(occ_date)
    attention_block = f"\nATTENTION: {attention}\n" if attention else ""
    message_block = f"\nMessage: {message}\n" if message else ""
    description_text = html_to_text(course.description) if include_description and course.description else ""
    return details + attention_block + message_block + (f"\n{description_text}\n" if description_text else "")


def host_details_text(course: Course, occ_date: str) -> str:
    """booking_details_text()'s HOST-ONLY variant (2026-08-19): the same
    What/When/Where(+ATTENTION) block, but ASCII (no emoji) and WITHOUT
    the course description.

    Both differences are the operator's own call, from comparing two real
    emails side by side: emails that only ever land in the operator's own
    inbox should be plain ASCII, and the description is theirs -- they
    wrote it in settings.toml, repeating it back is noise. Deliberately a
    thin wrapper over the same builder rather than a second copy of the
    layout: What/When/Where ordering and the ATTENTION line stay defined
    in exactly ONE place, so the host and participant blocks can never
    drift apart (the same anti-drift rule that made this function shared
    in the first place -- see its docstring).

    Note there is no host twin of course_recap_html(): host-only emails
    are plain-text only now (see send_cancellation_emails)."""
    return booking_details_text(course, occ_date, emoji=False, include_description=False)


def host_subject(prefix: str, course: Course, occ_date: str, spots_taken: int) -> str:
    """The ONE subject-line shape every host-only email uses (2026-08-19):
    "<prefix>: <shortname> on <date> [taken/capacity]".

    The course SHORTNAME, not its title (operator's explicit choice): a
    subject line only has to say which course this is at a glance, and
    the real titles run to 50+ characters. This is deliberately the one
    place a shortname is still shown -- the standing "translate
    shortnames to titles" rule (2026-07-05) is about GUI pages and
    participant-facing text; the body's own What: line still carries the
    full title.

    The [taken/capacity] counter is read as taken-of-capacity (see
    App._occupancy) and is always the state AFTER whatever this email is
    reporting -- so a cancellation's subject already shows the freed-up
    number.

    The matching BODY line lives in the templates themselves
    ("{{spots_taken}} / {{capacity}} spots taken now."), not here -- every
    host template is passed both macros, so its wording and placement are
    editable without touching Python."""
    return f"{prefix}: {course.shortname} on {occ_date} [{spots_taken}/{course.capacity}]"


def course_recap_html(course: Course, occ_date: str, message: str = "") -> str:
    """HTML twin of booking_details_text() above -- same What/When/Where
    emoji/ordering, plus the operator's own rich-HTML `description` in a
    boxed, background-colored block (2026-07-09: format the description
    in an email the same as on the page -- box it, same background
    color, generated by the same code for both page and email).
    Deliberately INLINE-styled, not
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
    # 2026-07-16: automatic red ATTENTION callout for an exceptional
    # per-date time change -- see attention_html()/Course.
    # override_message_for's own docstrings. Looked up straight from
    # `course`/`occ_date`, so every caller of this function gets it for
    # free, same reasoning as booking_details_text()'s plain-text twin
    # above.
    attention_block = attention_html(course.override_message_for(occ_date))
    message_block = message_html(message) if message else ""
    desc_html = (
        '<div style="background:#fdf8ef;border:1px solid #eee0c0;border-radius:8px;'
        f'padding:1em 1.2em;margin:.6em 0 0">{course.description}</div>'
    ) if course.description else ""
    return (
        '<div style="background:#f4f7f4;border:1px solid #ddd;border-radius:8px;'
        'padding:1em 1.2em;margin:1em 0;font-family:sans-serif">'
        f'<p style="margin:.3em 0"><b>{_WHAT_EMOJI} What:</b> {esc(course.title)}</p>'
        f'<p style="margin:.3em 0"><b>{_WHEN_EMOJI} When:</b> {esc(occ_date)} {esc(course.time_range_label_for(occ_date))}</p>'
        f'<p style="margin:.3em 0"><b>{_WHERE_EMOJI} Where:</b> {esc(course.location)}</p>'
        f"{attention_block}"
        f"{message_block}"
        f"{desc_html}"
        "</div>"
    )


def intro_html(text: str) -> str:
    """The FIRST sentence of every HTML booking/cancellation/promotion
    email (2026-07-10: made more visible -- bold and a larger font size
    than everything else in the email, applied consistently across ALL
    emails) -- bigger and bolder than the recap/detail text below
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
    BEFORE intro_html()'s bold status sentence -- 2026-07-08: after
    _send_confirm_email() got a "Dear NAME," greeting (2026-07-07), the
    guest-facing booking-result/cancellation/reinstatement emails still
    didn't; this closes that gap for those three, deliberately NOT for the
    admin-facing copies (admin_email) or the party-admin summary, which are
    receipts to the operator's own inbox, not letters to a guest -- see
    each call site's own comment. Kept separate from intro_html() rather
    than folded into it: the greeting is a normal-weight salutation, not
    the bold, larger "most important fact" line intro_html() exists for,
    and not every intro_html() caller (e.g. the admin-facing emails above)
    wants a greeting at all."""
    return f'<p style="margin:0 0 .5em">Dear {html.escape(name, quote=True)},</p>'


def message_html(message: str, label: str = "Message:") -> str:
    """The optional free-text comment collected by Cancel's and Reinstate's
    own confirm dialogs (2026-07-10: Reinstate gained the same optional
    COMMENT-to-the-other-side field Cancel already had, and the message
    from the comment field is always displayed with a light grey
    background) -- boxed the same way
    course_recap_html() boxes the operator's own `description` (border +
    radius + padding), but in a neutral light grey rather than that box's
    cream (`#fdf8ef`), so a guest/host-typed comment reads as visually
    distinct from the operator-authored course description. Blank message
    renders nothing at all, same as the old plain-`<p>` version this
    replaces -- every call site already only calls this when there IS a
    message to show.

    `label` (2026-07-09: send_cancellation_emails's participant copy
    should read "Message you sent to the host:" when the attendee
    canceled, or "Message from the host:" when the host canceled)
    defaults to the plain "Message:" every other caller
    (send_reinstatement_emails, the admin copy of a cancellation) still
    wants -- only send_cancellation_emails's participant copy passes a
    direction-aware one."""
    return (
        '<div style="background:#f2f2f2;border:1px solid #ddd;border-radius:8px;'
        f'padding:.8em 1.2em;margin:.6em 0"><b>{html.escape(label, quote=True)}</b> '
        f'{html.escape(message, quote=True)}</div>'
    )


def attention_html(message_html_inner: str) -> str:
    """Boxed, RED "ATTENTION" callout for an exceptional per-date time
    change (Course.date_overrides, 2026-07-16), automatically displayed
    in red. Same boxed shape as
    message_html() above, but red rather than grey/cream, so a schedule
    exception reads as visually distinct (and more urgent) than an
    ordinary human-typed comment.

    UNLIKE message_html() (guest/host-typed free text -- always esc()'d),
    `message_html_inner` here is NOT escaped: a date-override's `message`
    comes from settings.toml, the exact same operator-authored trust
    boundary as Course.description (see course_recap_html's own
    desc_html, rendered raw for the same reason) -- whoever can edit
    settings.toml already has full control of the server, so this isn't
    a new privilege boundary. This also lets app/webapp.py's own
    _course_date_overrides_html build one combined banner (multiple
    dates, its own `<b>`/`<br>` markup) through this same function
    instead of a second copy of the box styling.

    Blank input renders nothing -- every call site only has something to
    show when Course.override_message_for()/date_overrides actually
    found one for the date(s) in question."""
    if not message_html_inner:
        return ""
    return (
        '<div style="background:#fdecea;border:1px solid #f5c2c0;border-radius:8px;'
        'padding:.8em 1.2em;margin:.6em 0;color:#a61b1b">'
        f'<b>⚠ ATTENTION:</b> {message_html_inner}</div>'
    )


def join_attention_sections(*parts: str) -> str:
    """Joins non-empty ATTENTION-box sections with a `<hr>` between them --
    2026-07-13, added alongside [site].custom_attention_message
    (app/config.py): the ONE red box can now show up to two kinds of
    notice at once (the automatic per-course/site-wide schedule-exception
    text, plus an optional operator-authored site-wide message), and they
    need a visible separator when both are present, but no stray `<hr>`
    when only one is. Every caller still passes the combined result
    through attention_html() above for the actual box/prefix -- this only
    joins the pieces that go inside it. "" (renders nothing) when every
    part passed in is empty."""
    return "<hr>".join(p for p in parts if p)


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
    registration_id: str, spots_taken: int, reinstate_token: str | None = None,
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
    magic link (2026-07-10: /my and /admin use the same confirm-popup
    pattern, but the email instead links to a single WHAT/WHEN/WHERE
    page, like the confirmation email; both participant AND admin
    copies get a reinstate link) -- same trust
    model as the existing `/host-cancel/<id>` link (gated purely by this
    being an unguessable uuid4, no separate secret; see host_cancel()'s
    own docstring). `reinstate_token` is the PLAINTEXT of a token the
    caller freshly minted right before calling this (see Store.cancel()'s
    own `reinstate_token_hash` param for why it can't be the guest's
    original cancel token) -- builds the participant's `/reinstate/<token>`
    link the same way `/cancel/<token>` itself is built. `None` (the
    default) omits the participant's reinstate line entirely, e.g. for a
    caller that has no user/email to send it to anyway.

    `ics_attachment` (2026-07-09: also attach a CANCEL-ics as a courtesy)
    is the caller's already-built (filename, ics_text,
    "CANCEL") tuple from app.calendar_sync.guest_cancel_ics() -- built by
    the CALLER, not here, since this module deliberately has no dependency
    on app.calendar_sync (which itself imports FROM this module, for
    html_to_text -- importing back would be a cycle). Only ever attached to
    the PARTICIPANT's copy, never the admin's: the admin's own calendar
    already gets the authoritative update straight from CalDAV
    (calendar_sync.sync_occurrence), so a second, personal "delete this
    from your calendar" attachment on their own admin-copy email would be
    redundant at best, confusing at worst.

    2026-07-13: any cancellation the HOST initiates (canceled_by=
    "host" -- a single booking via `/admin`/`my-bt cancel`, or an entire
    occurrence via cancel_flow.cancel_occurrence) gets a short apology +
    "this is the exception" line, plus a link to book the course's NEXT
    occurrence, appended to the PARTICIPANT's copy only (the host does
    not need the link). Guest-initiated self-cancels (canceled_by="guest")
    never get this -- there's nothing to apologize for when the guest made
    their own choice. Kept to a plain `/book/<shortname>` link (the
    course's normal booking page, which lists whichever occurrence is next)
    rather than computing one specific date here -- this module has no
    calendar-conflict/capacity-lookup machinery to compute that itself (see
    app/slots.py), and the booking page already does that correctly.

    2026-07-09, three-part wording redesign (from a real
    host-initiated cancellation email):
    (a) a host-initiated cancel's email should never read as if the
    HOST personally canceled the meeting -- traced to the ADMIN copy's
    intro, which named the attendee when the GUEST canceled but just said
    "You" for a host-initiated cancel, never actually naming who got
    canceled. Fixed: the admin copy's intro now always names the attendee
    ("You canceled NAME <email>'s booking:") when canceled_by=="host", same
    as it already did (from the other direction) when canceled_by=="guest".
    Confirmed separately that `my-bt`/`/admin` can never masquerade as an
    attendee cancel -- every host-side call site is hardcoded to
    canceled_by="host" (see Store.cancel()'s own vocabulary) -- so this was
    purely a wording gap, not a real permission bug.
    (b) the message box should sit right after the "You canceled the
    below meeting." line, labeled direction-aware for the participant
    copy ("Message you sent to the host:" when the attendee canceled,
    "Message from the host:" when the host canceled) -- the participant copy's message block
    (message_html(), given a direction-aware label) now sits right after
    the intro line, before the What/When/Where recap, instead of being
    baked into course_recap_html() between Where and the description. The
    admin copy's message block moves the same way, but keeps the plain
    "Message:" label -- there's no "direction" to convey to the operator
    themselves.
    (c) host cancellations should NOT show a Reinstate link, and attendee
    cancellations should show a more prominent one, as its own sentence
    right after "You canceled the below meeting." ("In case this was a
    mistake with this link you can easily resubscribe: ...") -- the participant's reinstate link is now omitted entirely when
    canceled_by=="host" (even though `reinstate_token` is still minted by
    every caller, same as before -- see _send_cancellation_emails's own
    docstring), and reworded into its own prominent sentence placed right
    after the intro when canceled_by=="guest". The ADMIN copy's own
    `/host-reinstate/<id>` link is untouched either way -- the operator
    undoing their own mistake is a separate, still-useful action
    regardless of who initiated the cancellation."""
    details = booking_details_text(course, occ_date)
    recap_html = course_recap_html(course, occ_date)
    subject = f"Canceled: {course.title} on {occ_date}"
    my_url = f"{settings.base_url}/my"
    # `spots_taken` is the count AFTER this cancellation (every caller
    # reads it once the row is already persisted), so the host's copy
    # reports the freed-up number -- see host_subject().
    host_reinstate_url = f"{settings.base_url}/host-reinstate/{registration_id}"
    if user:
        participant_who = "You" if canceled_by == "guest" else "The host"
        intro_text = f"{participant_who} canceled this booking:"

        # 2026-07-09 (c): a prominent standalone sentence for a guest's OWN
        # cancellation only -- host-initiated cancels get no participant
        # reinstate link at all (see this function's own docstring).
        resubscribe_line = ""
        resubscribe_line_html = ""
        if canceled_by == "guest" and reinstate_token:
            guest_reinstate_url = f"{settings.base_url}/reinstate/{reinstate_token}"
            resubscribe_line = f"In case this was a mistake, you can easily resubscribe: {guest_reinstate_url}\n"
            resubscribe_line_html = (
                f'<p>In case this was a mistake, you can easily resubscribe: '
                f'<a href="{guest_reinstate_url}">{guest_reinstate_url}</a></p>'
            )

        # 2026-07-09 (b): direction-aware label, right after the intro line.
        message_label = "Message you sent to the host:" if canceled_by == "guest" else "Message from the host:"
        message_line = f"\n{message_label} {message}\n" if message else ""
        message_line_html = message_html(message, label=message_label) if message else ""

        apology_line = ""
        apology_line_html = ""
        if canceled_by == "host":
            next_occurrence_url = f"{settings.base_url}/book/{course.shortname}"
            apology_text = (
                "We're sorry for the inconvenience -- canceling a course is rare, "
                "the exception rather than the rule."
            )
            apology_line = (
                f"\n{apology_text}\n"
                f"Book the next occurrence of this course: {next_occurrence_url}\n"
            )
            apology_line_html = (
                f"<p>{html.escape(apology_text, quote=True)}</p>"
                f'<p>Book the next occurrence of this course: '
                f'<a href="{next_occurrence_url}">{next_occurrence_url}</a></p>'
            )
        # 2026-07-08: guest-facing emails should greet by name, same
        # as _send_confirm_email() already did -- see greeting_html()'s own
        # docstring. Participant copy only, never the admin copy below.
        #
        # 2026-07-09: templates gained support for macros/variables, so
        # cancel_email.html/.txt can DEFINE how the final email is
        # assembled -- this participant copy is the
        # pilot conversion: every piece below is computed exactly as
        # before (nothing about WHAT each macro renders changed), but the
        # ASSEMBLY ORDER now lives in email_templates/cancel_email.txt/
        # .html (see app/email_templates.py) instead of being hardcoded
        # Python string concatenation, so it can be edited without
        # touching this file at all. The admin copy just below is NOT
        # converted yet -- see this module's own note in SOLUTION-DESIGN.md.
        manage_link_html = f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
        send_mail(
            settings, user.email, subject,
            render_template(
                load_email_template(settings, "cancel_email.txt"),
                name=user.name, intro=intro_text, resubscribe_line=resubscribe_line,
                message_line=message_line, details=details, manage_url=my_url,
                apology_line=apology_line,
            ),
            html_body=html_email_body(render_template(
                load_email_template(settings, "cancel_email.html"),
                greeting=greeting_html(user.name), intro=intro_html(intro_text),
                resubscribe_line=resubscribe_line_html, message_line=message_line_html,
                recap=recap_html, manage_link=manage_link_html, apology_line=apology_line_html,
            )),
            ics_attachment=ics_attachment,
            bcc_addrs=settings.bcc_attendee_email_list,
        )
    # 2026-07-09 (a): host-initiated cancels now name the attendee here too
    # (unless there's no user to name at all), instead of the previous bare
    # "You canceled this booking:" that never said WHO. Guest-initiated
    # cancels are unchanged -- that branch already named the attendee.
    if canceled_by == "host":
        admin_intro = f"You canceled {user.name} <{user.email}>'s booking:" if user else "You canceled this booking:"
    else:
        admin_who = f"{user.name} <{user.email}>" if user else "The attendee"
        admin_intro = f"{admin_who} canceled this booking:"
    # 2026-07-09 (b): same reposition as the participant copy above, kept
    # as the plain "Message:" label -- there's no "direction" to convey to
    # the operator's own inbox.
    admin_message_line = f"\nMessage: {message}\n" if message else ""
    # 2026-07-14: looked for a simpler, more intuitive word than
    # "reinstate" -- picked "Rebook" (visible text only; the
    # underlying route/function/variable names -- reinstate_token,
    # /host-reinstate/<id>, send_reinstatement_emails, reinstated_by,
    # etc. -- are deliberately UNCHANGED, since /host-reinstate/<id> and
    # /reinstate/<token> are real URLs already sitting in guests'
    # already-sent emails; renaming those would break old links).
    # 2026-08-19: host-only copies are plain-text ASCII now -- no HTML
    # part, no emoji, no description repeated back, plus the occupancy
    # counter every other host email carries. See host_details_text()/
    # host_subject() and SOLUTION-DESIGN #40.
    send_mail(
        settings, settings.admin_email, host_subject("Canceled", course, occ_date, spots_taken),
        render_template(
            load_email_template(settings, "cancel_email_admin.txt"),
            intro=admin_intro, message_line=admin_message_line,
            details=host_details_text(course, occ_date),
            spots_taken=str(spots_taken), capacity=str(course.capacity),
            reinstate_url=host_reinstate_url,
        ),
        reply_to=user.email if user else None,
    )


def send_reinstatement_emails(
    settings: Settings, course: Course, occ_date: str, user, confirmed: bool, reinstated_by: str, message: str,
    spots_taken: int, ics_attachment: tuple[str, str, str] | None = None,
) -> None:
    """The "undo a cancel" twin of send_cancellation_emails() above
    (2026-07-10: a reschedule button for
    canceled meetings whose time (WHEN) is still in the future -- meaning
    undoing the cancellation for the SAME
    occurrence, not moving to a different one, with both the guest's
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

    `message` (2026-07-10: Reinstate gained the same optional COMMENT
    field, sent to the other side, that Cancel already had) is the
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
    subject = f"Rebooked: {course.title} on {occ_date}"
    my_url = f"{settings.base_url}/my"
    status_phrase = "you're confirmed again" if confirmed else "you're back on the waitlist"
    if user:
        participant_who = "You" if reinstated_by == "guest" else "The host"
        intro = f"{participant_who} rebooked this booking -- {status_phrase}:"
        # 2026-07-08: same "Dear NAME," greeting as
        # send_cancellation_emails' own participant copy above.
        manage_link_html = f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
        send_mail(
            settings, user.email, subject,
            render_template(
                load_email_template(settings, "reinstate_email.txt"),
                name=user.name, intro=intro, details=details, manage_url=my_url,
            ),
            html_body=html_email_body(render_template(
                load_email_template(settings, "reinstate_email.html"),
                greeting=greeting_html(user.name), intro=intro_html(intro),
                recap=recap_html, manage_link=manage_link_html,
            )),
            ics_attachment=ics_attachment,
            bcc_addrs=settings.bcc_attendee_email_list,
        )
    admin_who = "You" if reinstated_by == "host" else (f"{user.name} <{user.email}>" if user else "The attendee")
    admin_intro = f"{admin_who} rebooked this booking -- {status_phrase}:"
    # Plain-text ASCII host copy, same shape as every other host email
    # (see send_cancellation_emails' own note).
    send_mail(
        settings, settings.admin_email, host_subject("Rebooked", course, occ_date, spots_taken),
        render_template(
            load_email_template(settings, "reinstate_email_admin.txt"),
            intro=admin_intro,
            # 2026-08-19: the optional comment is its OWN macro here now,
            # like the cancellation admin copy already had -- host_details_text()
            # deliberately never carries it, so a template can place it
            # without ever risking it appearing twice. (It used to ride
            # inside `details`; keeping it visible to the host matters --
            # that's how someone acting on a guest's behalf gets noticed.)
            message_line=f"\nMessage: {message}\n" if message else "",
            details=host_details_text(course, occ_date),
            spots_taken=str(spots_taken), capacity=str(course.capacity),
        ),
        reply_to=user.email if user else None,
    )
