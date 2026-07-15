"""The full post-cancellation flow -- factored out of app/webapp.py's App
class (2026-07-06) so every caller that cancels a registration (the web
guest/admin paths, `my-bt cancel`, and `my-bt erase`/the `/my` self-erasure
path) drives the exact SAME sequence, rather than each reimplementing (or,
worse, only partially implementing) it. Before this module existed,
App._cancel_and_promote() had the full sequence (promote + calendar sync +
promotion emails) but app/cli_cancel.py's cancel_registration() and
app/erasure.py's erase_user_by_email() each only did a subset (email, no
promotion, no calendar sync) -- a documented, known gap. See
SOLUTION-DESIGN.md's "unify backend" standing rule.

App methods take no arguments beyond `self` (settings/store/caldav all live
on the instance), which is awkward to reuse from a standalone CLI script
that has no App/WSGI machinery at all -- this module-level function takes
`store`/`settings`/`caldav` explicitly instead, so it works identically
whether called from within App or from scripts/my-bt.

app/webapp.py's App._cancel_and_promote is now a thin wrapper that just
forwards to cancel_and_promote() below -- see its own docstring. This
refactor changes no behavior for any existing web caller: same promotion
logic, same emails, same calendar sync.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import calendar_sync
from .caldav_client import CalDAVClient, CalDAVError
from .cancellation import (
    booking_details_text, course_recap_html, greeting_html, html_email_body, intro_html,
    send_cancellation_emails,
)
from .config import Settings
from .email_templates import load_email_template, render_template
from .emailer import send_mail
from .security import hash_token, new_token
from .storage import (
    STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Registration, Store,
)

log = logging.getLogger("my_booking.cancel_flow")

# The three LIVE, still-cancelable statuses (2026-07-13: extended to include
# STATUS_PENDING_CONFIRMATION -- see Store.cancel()'s own docstring for why
# a guest who hasn't yet clicked their account-confirmation link used to be
# stuck, uncancelable, by any path). Shared by cancel_occurrence() below and
# by anything (scripts/my-bt, app/webapp.py) that needs to know which live
# registrations on one occurrence WOULD be affected before actually acting --
# see find_cancelable_registrations_for_occurrence().
CANCELABLE_STATUSES = (STATUS_CONFIRMED, STATUS_WAITLISTED, STATUS_PENDING_CONFIRMATION)


def find_cancelable_registrations_for_occurrence(
    store: Store, course_shortname: str, occurrence_date_str: str,
) -> list[Registration]:
    """Every LIVE registration for one (course_shortname, occurrence_date)
    that cancel_occurrence() below would act on -- i.e. everyone who'd be
    notified by a "cancel this entire session" action. Read-only: used both
    by the `/host-cancel-occurrence` confirmation page (GET, before the
    host commits) and by cancel_occurrence() itself (which re-derives this
    same list right before acting, since time may have passed between the
    two)."""
    return [
        Registration(**r) for r in store.read_registrations(scope="live")
        if r["course_shortname"] == course_shortname
        and r["occurrence_date"] == occurrence_date_str
        and r["status"] in CANCELABLE_STATUSES
    ]


@dataclass
class CanceledParticipant:
    """One row cancel_occurrence() actually canceled -- enough for a caller
    (scripts/my-bt, the /host-cancel-occurrence confirmation page) to report
    who was affected without a second store lookup."""
    registration_id: str
    user_id: str
    user_name: str
    user_email: str
    status_before: str


@dataclass
class CancelOccurrenceResult:
    course_shortname: str
    occurrence_date: str
    canceled: list[CanceledParticipant] = field(default_factory=list)


def build_caldav_client(settings: Settings) -> CalDAVClient:
    """Same construction app/webapp.py::App.__init__ does for its own
    self.caldav -- factored out here so a standalone caller (scripts/my-bt,
    or any test wanting the real thing) builds an identical client rather
    than re-deriving the three settings fields involved a second time
    somewhere else."""
    return CalDAVClient(settings.caldav_url, settings.caldav_username, settings.caldav_password)


def calendar_href(caldav: CalDAVClient, settings: Settings) -> str:
    """One-off equivalent of App._href(settings.booking_calendar): a single
    PROPFIND, then look up the configured booking calendar's href. App
    itself caches list_calendars() per-process (see App._calendars) since it
    serves many requests; a standalone call here (one cancellation, then
    done) has no such long-lived process to cache across, so it simply
    fetches fresh every time.

    2026-07-08: made public (was `_calendar_href`) so
    app.calendar_sync.resync_after_course_rename can reuse it too, rather
    than a second copy of this same PROPFIND-then-lookup logic."""
    calendars = caldav.list_calendars()
    if settings.booking_calendar not in calendars:
        raise CalDAVError(
            f"calendar '{settings.booking_calendar}' not found among {list(calendars)} -- "
            "check settings.toml [calendar].booking_calendar / conflict_calendars"
        )
    return calendars[settings.booking_calendar]


def cancel_and_promote(
    store: Store,
    settings: Settings,
    caldav: CalDAVClient,
    course_shortname: str,
    occurrence_date_str: str,
    sync_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Call right after store.cancel(): if that freed a confirmed spot,
    promote the longest-waiting person on the waitlist and email them (plus
    an admin copy), then re-sync the calendar event once with the final
    state (see calendar_sync.sync_occurrence) -- active/waitlisted/canceled
    participants, in one PUT (or, if now zero-active, one DELETE).

    Every one of the four cancellation paths (guest email link, guest's own
    /my, host's /admin, `my-bt cancel`) calls this exact function, as does
    account erasure's own pre-archival force-cancel (`/my` self-erasure and
    `my-bt erase`) -- see app/erasure.py::erase_user_by_email. None of them
    reimplement any piece of it.

    `sync_fn`, if given, replaces the default "look up the booking
    calendar's href, then calendar_sync.sync_occurrence" pair with a
    caller-supplied `(course_shortname, occurrence_date_str) -> None`
    callable -- this is exactly what App._cancel_and_promote passes
    (App._sync, which reuses App's own cached calendar-href lookup) so web
    callers keep their existing caching/mocking behavior unchanged. Standalone
    callers (scripts/my-bt, app/erasure.py) simply omit it and get the
    straightforward one-off PROPFIND-then-sync described above.
    """
    course = settings.course(course_shortname)
    if course is None:
        # The course was removed from settings.toml since this registration
        # was made -- nothing sensible to promote-into (no known capacity)
        # or sync to the calendar (no title/location/description). Flagged
        # during the 2026-07-06 unification: every caller of this function
        # used to call it unconditionally, so a stale course_shortname would
        # crash here on course.capacity -- now a clean no-op instead.
        log.warning(
            "cancel_and_promote: course %r not found in settings.toml (occurrence %s) -- "
            "skipping promotion/calendar sync", course_shortname, occurrence_date_str,
        )
        return
    promoted = store.promote_next_waitlisted(course_shortname, occurrence_date_str, course.capacity)
    if promoted:
        # promoted is every row in ONE promoted party (see
        # Store.promote_next_waitlisted's docstring) -- a solo booking's
        # party is just itself, so this loop runs once in the common case,
        # and behavior here is unchanged for anyone not using guest
        # bookings.
        promoted_users = []
        for reg in promoted:
            user = store.find_user_by_id(reg.user_id)
            if user is None:
                continue
            promoted_users.append(user)
            # Same What/When/Where(+description) layout as every other
            # booking-related email (see booking_details_text() in
            # app/cancellation.py) -- this used to be a plain one-liner with
            # only start_time, which drifted out of sync the moment that
            # method got the richer layout (caught in the 2026-07-05
            # consistency review). No fresh cancel link here: the original
            # waitlist-join token's plaintext isn't recoverable from its
            # stored hash, and regenerating one would need a DB write for a
            # link that wasn't specifically requested -- /my already lists
            # this booking with its own Cancel button now that every guest
            # has an account, so that's the invite instead.
            intro = "A spot opened up and you were next on the waitlist -- you're now confirmed:"
            my_url = f"{settings.base_url}/my"
            ics_filename, ics_text = calendar_sync.guest_invite_ics(
                settings, course, date.fromisoformat(occurrence_date_str)
            )
            details = booking_details_text(course, occurrence_date_str)
            recap_html = course_recap_html(course, occurrence_date_str)
            manage_link_html = f'<p>Manage or cancel this booking: <a href="{my_url}">{my_url}</a></p>'
            # greeting (2026-07-14, repo-review wording pass): this was the
            # ONE guest-facing booking email still missing the "Dear NAME,"
            # greeting every other one has carried since 2026-07-08 (see
            # cancellation.greeting_html's own docstring / SOLUTION-DESIGN
            # #19) -- an oversight, not a decision; this module was
            # factored out before that rule landed.
            send_mail(
                settings, user.email, f"You're in! {course.title} on {occurrence_date_str}",
                render_template(
                    load_email_template(settings, "promoted_email.txt"),
                    greeting=f"Dear {user.name},\n\n", intro=intro, details=details, manage_url=my_url,
                ),
                html_body=html_email_body(render_template(
                    load_email_template(settings, "promoted_email.html"),
                    greeting=greeting_html(user.name), intro=intro_html(intro),
                    recap=recap_html, manage_link=manage_link_html,
                )),
                ics_attachment=(ics_filename, ics_text, "PUBLISH"),
                bcc_addrs=settings.bcc_attendee_email_list,
            )
        if promoted_users:
            # One combined admin email for the whole promoted party (not one
            # per person) -- same standing default as every other booking/
            # cancellation email (both sides notified, see
            # _send_booking_result_email/send_cancellation_emails), just
            # consolidated so promoting a party of 3 doesn't send admin 3
            # near-identical emails at once.
            names = ", ".join(f"{u.name} <{u.email}>" for u in promoted_users)
            verb = "were" if len(promoted_users) > 1 else "was"
            admin_intro = f"{names} {verb} promoted from the waitlist to confirmed for:"
            admin_details = booking_details_text(course, occurrence_date_str)
            admin_recap_html = course_recap_html(course, occurrence_date_str)
            send_mail(
                settings, settings.admin_email,
                f"Promoted from waitlist: {course.title} on {occurrence_date_str}",
                render_template(
                    load_email_template(settings, "promoted_admin_email.txt"),
                    intro=admin_intro, details=admin_details,
                ),
                html_body=html_email_body(render_template(
                    load_email_template(settings, "promoted_admin_email.html"),
                    intro=intro_html(admin_intro), recap=admin_recap_html,
                )),
                # 2026-07-16: reply-to the first (leader) promoted user --
                # same "who does 'reply' go to for a party" call as
                # webapp.py's own _send_party_admin_email (new bookings).
                reply_to=promoted_users[0].email,
            )
    if sync_fn is not None:
        sync_fn(course_shortname, occurrence_date_str)
        return
    href = calendar_href(caldav, settings)
    calendar_sync.sync_occurrence(caldav, href, store, settings, course, date.fromisoformat(occurrence_date_str))


def cancel_occurrence(
    store: Store,
    settings: Settings,
    caldav: CalDAVClient | None,
    course_shortname: str,
    occurrence_date_str: str,
    message: str = "",
    sync_fn: Callable[[str, str], None] | None = None,
) -> CancelOccurrenceResult:
    """"Cancel the entire session" (2026-07-13: lets the operator cancel
    one whole course occurrence at once -- illness, venue unavailable, ...
    -- rather than one guest's booking). Cancels EVERY live confirmed/
    waitlisted/pending-confirmation registration for this (course_shortname,
    occurrence_date) -- see find_cancelable_registrations_for_occurrence()
    and CANCELABLE_STATUSES above -- and emails each participant, exactly
    the same app.cancellation.send_cancellation_emails every single-
    registration cancel path already uses (canceled_by="host", so each
    participant's copy gets the standard apology + next-occurrence-booking-
    link addition too -- see that function's own docstring).

    Unlike cancel_and_promote() above, this does NOT call
    store.promote_next_waitlisted(): there's no one left on this occurrence
    to promote INTO -- confirmed, waitlisted, AND pending-confirmation
    registrations are all being canceled together, so nobody's spot is
    "freed up" for anybody else. The calendar is still re-synced exactly
    ONCE at the end (not once per canceled row) via the same
    calendar_sync.sync_occurrence() cancel_and_promote() itself calls --
    with zero live participants left, this normally means a single DELETE
    of the operator's own event rather than a PUT.

    `message`, if given, is the optional free-text note included in every
    participant's cancellation email (same `host_message` field/dialog
    Cancel already supports for a single registration).

    `caldav`/`sync_fn`: same two ways to drive the calendar sync as
    cancel_and_promote() above -- pass `caldav` for a fresh one-off
    PROPFIND-then-sync (standalone callers: scripts/my-bt, the
    /host-cancel-occurrence route building its own client), or `sync_fn`
    to reuse a caller's own cached calendar-href lookup (e.g.
    App._sync). Neither is required if `course` isn't in settings.toml
    anymore -- see the "course removed" branch below, same as
    cancel_and_promote()'s own.

    Returns a CancelOccurrenceResult listing exactly who was canceled (empty
    `canceled` list, still a valid result, if nobody was live on this
    occurrence to begin with -- not an error, e.g. a double-submit of the
    same host-cancel-occurrence link)."""
    course = settings.course(course_shortname)
    live_regs = find_cancelable_registrations_for_occurrence(store, course_shortname, occurrence_date_str)

    # 2026-07-14, verified live: canceling every registration alone did
    # NOT block the date -- it reappeared on /book/<shortname> as bookable
    # with full capacity (build_occurrences regenerates it from the
    # weekly schedule, and deleting the calendar event also deletes the
    # only "conflict" that could have hidden it). Mark the occurrence
    # itself, unconditionally and first -- even a double-submit or an
    # empty occurrence stays a real "this session is not happening"
    # statement. Cleared only by a host reinstate on this occurrence
    # (see Store.clear_occurrence_canceled).
    store.mark_occurrence_canceled(course_shortname, occurrence_date_str, message=message)

    canceled: list[CanceledParticipant] = []
    for reg in live_regs:
        user = store.find_user_by_id(reg.user_id)
        # Fresh reinstate token per row, same as every other cancel path --
        # see Store.cancel()'s own `reinstate_token_hash` docstring for why
        # the original booking-confirmation token can't be reused here.
        reinstate_token = new_token()
        changed = store.cancel(
            reg.registration_id, canceled_by="host", host_message=message,
            reinstate_token_hash=hash_token(reinstate_token),
        )
        if not changed:  # pragma: no cover - raced by something else between the read above and here
            continue
        canceled.append(CanceledParticipant(
            registration_id=reg.registration_id,
            user_id=reg.user_id,
            user_name=user.name if user else "",
            user_email=user.email if user else "",
            status_before=reg.status,
        ))
        if course:
            ics_filename, ics_text = calendar_sync.guest_cancel_ics(
                settings, course, date.fromisoformat(occurrence_date_str)
            )
            send_cancellation_emails(
                settings, course, occurrence_date_str, user, canceled_by="host", message=message,
                registration_id=reg.registration_id, reinstate_token=reinstate_token,
                ics_attachment=(ics_filename, ics_text, "CANCEL"),
            )

    if course and canceled:
        # One calendar sync for the whole occurrence, not one per canceled
        # row -- calendar_sync.sync_occurrence() always recomputes the FULL
        # current participant list itself (it doesn't take a delta), so
        # calling it N times in a row would just do N-1 redundant
        # PUTs/DELETEs of the exact same final state.
        if sync_fn is not None:
            sync_fn(course_shortname, occurrence_date_str)
        elif caldav is not None:
            href = calendar_href(caldav, settings)
            calendar_sync.sync_occurrence(
                caldav, href, store, settings, course, date.fromisoformat(occurrence_date_str)
            )

    return CancelOccurrenceResult(
        course_shortname=course_shortname, occurrence_date=occurrence_date_str, canceled=canceled,
    )
