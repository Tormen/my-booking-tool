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
from datetime import date
from typing import Callable

from . import calendar_sync
from .caldav_client import CalDAVClient, CalDAVError
from .cancellation import booking_details_text, course_recap_html, html_email_body
from .config import Settings
from .emailer import send_mail
from .storage import Store

log = logging.getLogger("my_booking.cancel_flow")


def build_caldav_client(settings: Settings) -> CalDAVClient:
    """Same construction app/webapp.py::App.__init__ does for its own
    self.caldav -- factored out here so a standalone caller (scripts/my-bt,
    or any test wanting the real thing) builds an identical client rather
    than re-deriving the three settings fields involved a second time
    somewhere else."""
    return CalDAVClient(settings.caldav_url, settings.caldav_username, settings.caldav_password)


def _calendar_href(caldav: CalDAVClient, settings: Settings) -> str:
    """One-off equivalent of App._href(settings.booking_calendar): a single
    PROPFIND, then look up the configured booking calendar's href. App
    itself caches list_calendars() per-process (see App._calendars) since it
    serves many requests; a standalone call here (one cancellation, then
    done) has no such long-lived process to cache across, so it simply
    fetches fresh every time."""
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
            # link the operator didn't specifically ask for -- /my already lists
            # this booking with its own Cancel button now that every guest
            # has an account, so that's the invite instead.
            intro = "A spot opened up and you were next on the waitlist -- you're now confirmed:"
            my_url = f"{settings.base_url}/my"
            ics_filename, ics_text = calendar_sync.guest_invite_ics(
                settings, course, date.fromisoformat(occurrence_date_str)
            )
            send_mail(
                settings, user.email, f"You're in! {course.title} on {occurrence_date_str}",
                f"{intro}\n\n"
                + booking_details_text(course, occurrence_date_str)
                + f"\nManage or cancel this booking: {my_url}\n",
                html_body=html_email_body(
                    f"<p>{intro}</p>"
                    + course_recap_html(course, occurrence_date_str)
                    + f'<p>Manage or cancel this booking: <a href="{my_url}">{my_url}</a></p>'
                ),
                ics_attachment=(ics_filename, ics_text, "PUBLISH"),
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
            send_mail(
                settings, settings.admin_email,
                f"Promoted from waitlist: {course.title} on {occurrence_date_str}",
                f"{admin_intro}\n\n" + booking_details_text(course, occurrence_date_str),
                html_body=html_email_body(
                    f"<p>{admin_intro}</p>" + course_recap_html(course, occurrence_date_str)
                ),
            )
    if sync_fn is not None:
        sync_fn(course_shortname, occurrence_date_str)
        return
    href = _calendar_href(caldav, settings)
    calendar_sync.sync_occurrence(caldav, href, store, settings, course, date.fromisoformat(occurrence_date_str))
