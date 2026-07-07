"""Keeps the single course-occurrence VEVENT in sync with registrations.csv.

UID pattern `<slug>-<shortname>-<date>@<domain>` (slug/domain derived from
this deployment's own `settings.base_url`, see `_uid_parts` below) lets the
conflict checker (slots.py, via `is_own_event`) recognize and skip our own
generated events -- otherwise a course would always "conflict with
itself". Deriving this from `base_url` (rather than a hardcoded domain)
matters once more than one deployment of this tool exists: two installs
sharing a calendar (or one install's test/staging calendar reusing a
production one) must never recognize each other's events as "our own".
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .caldav_client import CalDAVClient, CalDAVConflictError
from .cancellation import html_to_text
from .config import Course, Settings
from .ics import VEvent
from .storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED,
    Registration, Store, User, format_display_timestamp,
)

log = logging.getLogger("my_booking.calendar_sync")

# 2026-07-07, the operator (a real production 500 on /my/confirm, root-caused via
# journalctl to "PUT ... -> HTTP 412 ... a newer version of the appointment
# already exists"): two near-simultaneous requests touching the SAME
# occurrence (e.g. two guests booking/confirming for the same course+date
# within a few seconds of each other) can each read this event's current
# ETag, then race to PUT/DELETE it -- whichever loses gets a 412. A plain
# retry a few seconds later succeeded on its own in that incident, i.e.
# genuinely transient, so this many attempts (re-reading a fresh ETag each
# time) is cheap insurance against exactly that, not a sign of a deeper
# problem if it occasionally takes 2.
_SYNC_CONFLICT_MAX_ATTEMPTS = 3


def _self_or_guest(r: Registration, users_by_id: dict[str, User]) -> str:
    """"self" for whoever filled out the booking form, "guest of <name>" for
    everyone they brought along -- same invited_by_user_id convention as
    admin_overview's Party column (see Registration's own docstring)."""
    if r.invited_by_user_id:
        leader = users_by_id.get(r.invited_by_user_id)
        return f"guest of {leader.name if leader else '(unknown)'}"
    return "self"


def _name_email(r: Registration, users_by_id: dict[str, User]) -> tuple[str, str]:
    user = users_by_id.get(r.user_id)
    if user is None:
        # Shouldn't normally happen (every registration_id points at a real
        # or archived user row), but a calendar invite is exactly the wrong
        # place to raise over it -- show a placeholder instead.
        return "(unknown)", "(unknown)"
    return user.name, user.email


def _uid_parts(settings: Settings) -> tuple[str, str]:
    """(slug, domain) for this deployment, from settings.base_url's
    hostname -- e.g. https://booking.example.org -> ("booking-example-org",
    "booking.example.org"). Falls back to a fixed placeholder domain if
    base_url is somehow unparseable, rather than raising, since this is
    only ever used to build/recognize a UID, not to make network calls."""
    host = urlparse(settings.base_url).hostname or "my-booking-tool.invalid"
    return host.replace(".", "-"), host


def event_uid(settings: Settings, course_shortname: str, occurrence_date: date) -> str:
    slug, domain = _uid_parts(settings)
    return f"{slug}-{course_shortname}-{occurrence_date.isoformat()}@{domain}"


def is_own_event(uid: str, settings: Settings) -> bool:
    slug, domain = _uid_parts(settings)
    return uid.startswith(f"{slug}-") and uid.endswith(f"@{domain}")


def occurrence_start_end(course: Course, occurrence_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Shared start/end datetime pair for one course occurrence -- factored
    out of sync_occurrence() (2026-07-09) so guest_invite_ics()/
    guest_cancel_ics() below build the EXACT same window the operator's own
    synced calendar event uses, rather than a second, easy-to-drift-apart
    copy of this three-line calculation."""
    h, m = course.start_hm()
    start = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day, h, m, tzinfo=tz)
    return start, start + timedelta(minutes=course.duration_minutes)


def sync_occurrence(
    client: CalDAVClient,
    calendar_href: str,
    store: Store,
    settings: Settings,
    course: Course,
    occurrence_date: date,
) -> None:
    """Call this after every registration/cancellation for the occurrence.

    The invite lists THREE groups: active (confirmed), waiting
    (waitlisted), and canceled (STATUS_CANCELED_BY_GUEST or
    STATUS_CANCELED_BY_HOST). Each line is a table row: status/position,
    Name, Email, Self/Guest (whether they booked themselves or came along
    as someone else's guest -- see _self_or_guest), the timestamp of that
    registrant's LAST action (registered_at for active/waiting, canceled_at
    + canceled_by for canceled), and a cancel link (2026-07-06: added the
    Name/Email/Self-Guest columns so the operator can see WHO is on his calendar
    without cross-referencing the CSV -- previously the invite only showed
    counts and cancel links, no identities at all).
    Canceled registrants are never dropped from the invite -- they stay
    visible, separately labeled, so the host can see who left and when
    without needing to cross-reference the CSV. Only when there are ZERO
    active (confirmed) registrants left is the event actually REMOVED from
    the calendar, regardless of how many are canceled or still waitlisted
    (see the `if not active:` branch below) -- a course with any confirmed
    spot filled always keeps its calendar entry, updated in place.
    """
    tz = ZoneInfo(settings.timezone)
    regs = store.registrations_for_occurrence(course.shortname, occurrence_date.isoformat())
    active = [r for r in regs if r.status == STATUS_CONFIRMED]
    waiting = [r for r in regs if r.status == STATUS_WAITLISTED]
    canceled = [r for r in regs if r.status in (STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST)]
    uid = event_uid(settings, course.shortname, occurrence_date)

    def current_etag() -> str | None:
        """(Re-)reads this occurrence's own event's CURRENT ETag, fresh --
        called once up front, and again by the retry loop below each time
        a 412 shows the etag we had was already stale."""
        for u, _ics, etag in client.query_events(
            calendar_href,
            datetime.combine(occurrence_date, datetime.min.time(), tzinfo=tz),
            datetime.combine(occurrence_date + timedelta(days=1), datetime.min.time(), tzinfo=tz),
        ):
            if u == uid:
                return etag
        return None

    etag = current_etag()

    if not active:
        # No confirmed registrants -- delete the event even if there's a
        # waitlist. Deliberate: the calendar event represents "this course
        # is actually happening", not "someone is interested in it", so a
        # fully-waitlisted occurrence (0 confirmed) has no calendar entry
        # at all until/unless someone gets promoted into a confirmed spot.
        if etag is not None:
            for attempt in range(1, _SYNC_CONFLICT_MAX_ATTEMPTS + 1):
                try:
                    client.delete_event(calendar_href, uid, etag)
                    break
                except CalDAVConflictError:
                    if attempt == _SYNC_CONFLICT_MAX_ATTEMPTS:
                        raise
                    log.warning(
                        "stale ETag deleting calendar event %s (attempt %d/%d) -- retrying with a fresh one",
                        uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS,
                    )
                    etag = current_etag()
                    if etag is None:
                        break  # someone else's concurrent change already removed it -- nothing left to do
        return

    start, end = occurrence_start_end(course, occurrence_date, tz)

    # One lookup covering everyone on this occurrence, live or archived (an
    # erased registrant can still be a CANCELED row here) -- read_users(
    # scope="all") so a since-erased participant still resolves to their
    # (now "[erased]"/hashed) row instead of "(unknown)".
    users_by_id = {u["user_id"]: User(**u) for u in store.read_users(scope="all")}

    lines = [f"{course.title} -- {len(active)}/{course.capacity} registered"]
    if waiting:
        lines.append(f"{len(waiting)} on waitlist")
    if canceled:
        lines.append(f"{len(canceled)} canceled")

    if active or waiting:
        lines.append("")
        lines.append("Participants:")
        lines.append("Status | Name | Email | Self/Guest | Registered | Cancel")
        for r in active:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- {r.status} | {name} | {email} | {_self_or_guest(r, users_by_id)} | "
                f"registered {format_display_timestamp(r.registered_at)} | "
                f"cancel: {settings.base_url}/host-cancel/{r.registration_id}"
            )
        for r in waiting:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- waitlisted #{waiting.index(r) + 1} | {name} | {email} | "
                f"{_self_or_guest(r, users_by_id)} | registered {format_display_timestamp(r.registered_at)} | "
                f"cancel: {settings.base_url}/host-cancel/{r.registration_id}"
            )
    if canceled:
        # Separate group, listed last -- kept OUT of the active/waiting
        # counts/lines above (this is display-only context on who left and
        # when, not part of "who currently holds a spot"). Shows
        # canceled_at (their last action -- NOT registered_at, which would
        # be stale/misleading here) and canceled_by ("guest" or "host") for
        # context, same vocabulary as Store.cancel()'s own parameter.
        lines.append("")
        lines.append("Canceled:")
        lines.append("Status | Name | Email | Self/Guest | Canceled | Cancel")
        for r in canceled:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- {r.status} | {name} | {email} | {_self_or_guest(r, users_by_id)} | "
                f"canceled {format_display_timestamp(r.canceled_at)} by {r.canceled_by} | "
                f"cancel: {settings.base_url}/host-cancel/{r.registration_id}"
            )
    summary = f"{course.title} ({len(active)}/{course.capacity}"
    summary += f"+{len(waiting)}wl)" if waiting else ")"
    event = VEvent(
        uid=uid,
        summary=summary,
        description="\n".join(lines),
        location=course.location,
        start=start,
        end=end,
        alarms_minutes_before=settings.trainer_calendar_reminder_minutes,
    )
    for attempt in range(1, _SYNC_CONFLICT_MAX_ATTEMPTS + 1):
        try:
            client.put_event(calendar_href, uid, event.to_ics(), etag=etag)
            return
        except CalDAVConflictError:
            if attempt == _SYNC_CONFLICT_MAX_ATTEMPTS:
                raise
            log.warning(
                "stale ETag updating calendar event %s (attempt %d/%d) -- retrying with a fresh one",
                uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS,
            )
            etag = current_etag()


# -- Emailed guest invite/cancel attachments (2026-07-09, the operator: "Can you
# please attach a calendar invite also in the email that is sent to the
# participant?") ------------------------------------------------------------
#
# Deliberately separate from sync_occurrence() above: that function builds
# ONE shared operator-facing event (all participants + a "Participants:"
# table) and PUTs it to the operator's own CalDAV calendar. These two
# functions instead build a personal, single-guest .ics MEANT AS AN EMAIL
# ATTACHMENT -- never sent to CalDAV, never containing anyone else's name/
# email. Same UID as the operator's own event (via event_uid()) since it's
# the same real-world occurrence, so if a client ever DOES correlate by UID
# across the two, the identity still matches correctly.


def guest_invite_ics(settings: Settings, course: Course, occurrence_date: date) -> tuple[str, str]:
    """Returns (filename, ics_text) for the "add this to your calendar"
    attachment on a CONFIRMED booking's own email (see
    app/webapp.py::_send_booking_result_guest_email and
    app/cancel_flow.py's promoted-from-waitlist email) -- both are the only
    two points where a guest goes from "not confirmed" to "actually holds a
    real spot", which is the only time there's a real event worth adding to
    a personal calendar. Deliberately NOT sent on a waitlisted email: there's
    no confirmed slot yet, nothing real to add.

    METHOD:PUBLISH, not REQUEST: this is a plain "here's your booking, add
    it if you like" notice, not a meeting invite with ORGANIZER/ATTENDEE/
    RSVP tracking. A REQUEST's Accept/Decline buttons (rendered natively by
    Outlook/Google Calendar) would imply declining changes something -- it
    wouldn't, canceling still only ever happens via the real cancel link --
    so offering that button would be actively misleading. This is exactly
    what real booking confirmations (train tickets, cinema, restaurant
    reservations) use for the same reason.
    """
    tz = ZoneInfo(settings.timezone)
    start, end = occurrence_start_end(course, occurrence_date, tz)
    description = html_to_text(course.description) if course.description else course.title
    event = VEvent(
        uid=event_uid(settings, course.shortname, occurrence_date),
        summary=course.title,
        description=description,
        location=course.location,
        start=start,
        end=end,
        method="PUBLISH",
        alarms_minutes_before=settings.guest_calendar_reminder_minutes,
    )
    return f"{course.shortname}-{occurrence_date.isoformat()}.ics", event.to_ics()


def guest_cancel_ics(settings: Settings, course: Course, occurrence_date: date) -> tuple[str, str]:
    """Returns (filename, ics_text) for a METHOD:CANCEL attachment on a
    cancellation email (2026-07-09, the operator: "AND CANCEL-ics as well please.
    Let's be nice :)") -- same UID as the original guest_invite_ics() above,
    SEQUENCE bumped to 1 and STATUS:CANCELLED set, so a calendar app that
    both (a) previously imported the PUBLISH invite via UID and (b) honors
    plain (non-REQUEST) CANCEL/SEQUENCE semantics will remove or gray out
    the entry on its own. Support for that varies by client -- there's no
    ORGANIZER/ATTENDEE relationship to formally correlate through (see
    guest_invite_ics()'s own docstring on why this deliberately isn't a
    REQUEST/RSVP flow) -- but it's the standard, correct thing to send
    regardless, and costs nothing for a client that ignores it: worst case
    the guest just deletes the (now stale) entry themselves, exactly as if
    this attachment didn't exist. No VALARMs on a cancellation -- nothing
    left to be reminded about."""
    tz = ZoneInfo(settings.timezone)
    start, end = occurrence_start_end(course, occurrence_date, tz)
    event = VEvent(
        uid=event_uid(settings, course.shortname, occurrence_date),
        summary=course.title,
        description=f"Canceled: {course.title}",
        location=course.location,
        start=start,
        end=end,
        method="CANCEL",
        sequence=1,
        status="CANCELLED",
        alarms_minutes_before=(),
    )
    return f"{course.shortname}-{occurrence_date.isoformat()}-canceled.ics", event.to_ics()
