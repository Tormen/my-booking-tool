"""Compute bookable occurrences for a course: weekday/time math, capped by
show_next_slots / show_next_days, minus calendar conflicts. Conflict-checking
is injected as a callable so this module has no network dependency and is
fully unit-testable.

Occurrences stay visible and bookable right up until they start -- only a
truly past occurrence (start already gone) is dropped. `min_notice_hours`
no longer hides anything here; it instead gates LATE bookings against
`min_required_participants` inside app/webapp.py::book (a late booking is
still allowed if it's the one that reaches quorum, only rejected if quorum
still wouldn't be met).

Full occurrences are NOT hidden (unlike calendar conflicts) -- they still
appear, marked `is_full`, so a guest can join the waitlist instead of a
confirmed spot. Only a genuine calendar conflict means "no session" and
disappears entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .config import Course, Settings


@dataclass(frozen=True)
class Occurrence:
    course_shortname: str
    date: date
    start: datetime
    end: datetime
    spots_taken: int
    capacity: int

    @property
    def spots_left(self) -> int:
        return max(0, self.capacity - self.spots_taken)

    @property
    def is_full(self) -> bool:
        return self.spots_left <= 0


class ConflictChecker(Protocol):
    def __call__(self, start: datetime, end: datetime) -> bool:
        """Return True if something else is already on the calendar during
        [start, end) -- meaning the session cannot take place."""
        ...


class CapacityLookup(Protocol):
    def __call__(self, course_shortname: str, occurrence_date: date) -> int:
        """Return the number of confirmed registrations for that occurrence."""
        ...


def _next_weekday_on_or_after(d: date, weekday_index: int) -> date:
    delta = (weekday_index - d.weekday()) % 7
    return d + timedelta(days=delta)


def candidate_dates(course: Course, from_date: date, horizon_days: int) -> list[date]:
    """All dates for this course's weekday within [from_date, from_date+horizon_days]."""
    first = _next_weekday_on_or_after(from_date, course.weekday_index())
    out = []
    d = first
    last = from_date + timedelta(days=horizon_days)
    while d <= last:
        out.append(d)
        d += timedelta(days=7)
    return out


def build_occurrences(
    course: Course,
    settings: Settings,
    now: datetime,
    capacity_lookup: CapacityLookup,
    conflict_checker: ConflictChecker,
) -> list[Occurrence]:
    """Returns up to `show_next_slots` occurrences, in order. Calendar
    conflicts are skipped entirely -- "no slot shown = no session". Full
    occurrences DO appear (with is_full=True) so guests can join the
    waitlist. Occurrences stay bookable until they start -- only a
    genuinely past `start` is dropped; there's no separate notice-period
    cutoff here (see module docstring).

    Course times in settings.toml are local time in `settings.timezone`
    (e.g. Europe/Berlin); `now` may be in any timezone (aware) or naive
    (assumed already local) -- both are normalized to `settings.timezone`
    via zoneinfo so DST transitions are handled correctly rather than by
    naive UTC-offset math.
    """
    tz = ZoneInfo(settings.timezone)
    if now.tzinfo is not None:
        now = now.astimezone(tz)
    else:
        now = now.replace(tzinfo=tz)

    out: list[Occurrence] = []
    for d in candidate_dates(course, now.date(), settings.show_next_days):
        # 2026-07-16: per-date exceptional time changes -- see
        # Course.date_overrides/start_hm_for/duration_minutes_for. A date
        # with no override behaves exactly as before (its own start_hm()/
        # duration_minutes).
        occ_date_str = d.isoformat()
        h, m = course.start_hm_for(occ_date_str)
        start = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
        end = start + timedelta(minutes=course.duration_minutes_for(occ_date_str))
        if start < now:
            continue
        if conflict_checker(start, end):
            continue
        taken = capacity_lookup(course.shortname, d)
        out.append(Occurrence(course.shortname, d, start, end, taken, course.capacity))
        if len(out) >= settings.show_next_slots:
            break
    return out
