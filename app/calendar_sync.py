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

from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .caldav_client import CalDAVClient
from .config import Course, Settings
from .ics import VEvent
from .storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED, Store,
)


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
    STATUS_CANCELED_BY_HOST) -- each line shows the same identifying info
    (registration status/position + a cancel link) plus the timestamp of
    that registrant's LAST action: registered_at for an active/waiting row
    (their last action was registering -- they haven't canceled), or
    canceled_at (plus who canceled it, canceled_by) for a canceled row.
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

    existing = {
        u: (ics, etag)
        for u, ics, etag in client.query_events(
            calendar_href,
            datetime.combine(occurrence_date, datetime.min.time(), tzinfo=tz),
            datetime.combine(occurrence_date + timedelta(days=1), datetime.min.time(), tzinfo=tz),
        )
        if u == uid
    }
    etag = existing[uid][1] if uid in existing else None

    if not active:
        # No confirmed registrants -- delete the event even if there's a
        # waitlist. Deliberate: the calendar event represents "this course
        # is actually happening", not "someone is interested in it", so a
        # fully-waitlisted occurrence (0 confirmed) has no calendar entry
        # at all until/unless someone gets promoted into a confirmed spot.
        if uid in existing:
            client.delete_event(calendar_href, uid, etag)
        return

    h, m = course.start_hm()
    start = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day, h, m, tzinfo=tz)
    end = start + timedelta(minutes=course.duration_minutes)

    lines = [f"{course.title} -- {len(active)}/{course.capacity} registered"]
    if waiting:
        lines.append(f"{len(waiting)} on waitlist")
    if canceled:
        lines.append(f"{len(canceled)} canceled")
    lines.append("")
    for r in active:
        lines.append(
            f"- {r.status} | registered {r.registered_at} | "
            f"cancel: {settings.base_url}/admin/cancel/{r.registration_id}"
        )
    for r in waiting:
        lines.append(
            f"- waitlisted #{waiting.index(r) + 1} | registered {r.registered_at} | "
            f"cancel: {settings.base_url}/admin/cancel/{r.registration_id}"
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
        for r in canceled:
            lines.append(
                f"- {r.status} | canceled {r.canceled_at} by {r.canceled_by} | "
                f"cancel: {settings.base_url}/admin/cancel/{r.registration_id}"
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
    )
    client.put_event(calendar_href, uid, event.to_ics(), etag=etag)
