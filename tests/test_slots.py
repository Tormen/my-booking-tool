import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.slots import build_occurrences, candidate_dates

from .helpers import make_course, make_settings


def no_conflict(start, end):
    return False


def always_conflict(start, end):
    return True


def zero_capacity(shortname, d):
    return 0


class CandidateDatesTest(unittest.TestCase):
    def test_picks_next_matching_weekday(self):
        course = make_course(weekday="wed")
        # 2026-07-04 is a Saturday
        dates = candidate_dates(course, __import__("datetime").date(2026, 7, 4), 21)
        self.assertEqual(dates[0].isoformat(), "2026-07-08")  # next Wednesday
        self.assertEqual(len(dates), 3)  # 3 Wednesdays in a 21-day horizon from Sat

    def test_same_day_included_if_matching(self):
        course = make_course(weekday="sat")
        dates = candidate_dates(course, __import__("datetime").date(2026, 7, 4), 0)
        self.assertEqual(dates, [__import__("datetime").date(2026, 7, 4)])


class BuildOccurrencesTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(show_next_slots=3, show_next_days=42, min_notice_hours=2)
        self.course = make_course(weekday="wed", start_time="17:15", duration_minutes=100, capacity=2)

    def test_returns_up_to_show_next_slots(self):
        now = datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc)
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        self.assertEqual(len(occs), 3)
        self.assertEqual(occs[0].date.isoformat(), "2026-07-08")

    def test_conflict_hides_occurrence_entirely(self):
        now = datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc)
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, always_conflict)
        self.assertEqual(occs, [])

    def test_full_occurrence_still_shown_but_marked_full(self):
        now = datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc)
        occs = build_occurrences(self.course, self.settings, now, lambda s, d: 2, no_conflict)
        self.assertEqual(len(occs), 3)
        self.assertTrue(all(o.is_full for o in occs))
        self.assertEqual(occs[0].spots_left, 0)

    def test_stays_bookable_right_up_to_start_despite_min_notice_hours(self):
        # min_notice_hours no longer hides anything here (it only gates
        # LATE bookings in app/webapp.py::book) -- an occurrence just
        # minutes from starting must still be offered.
        tz = ZoneInfo(self.settings.timezone)
        now = datetime(2026, 7, 8, 17, 0, tzinfo=tz)  # 15 min before start, notice=2h
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        self.assertIn("2026-07-08", [o.date.isoformat() for o in occs])

    def test_disappears_once_actually_started(self):
        tz = ZoneInfo(self.settings.timezone)
        now = datetime(2026, 7, 8, 17, 16, tzinfo=tz)  # 1 min after 17:15 start
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        self.assertNotIn("2026-07-08", [o.date.isoformat() for o in occs])
        self.assertEqual(occs[0].date.isoformat(), "2026-07-15")

    def test_naive_now_is_treated_as_local(self):
        now = datetime(2026, 7, 4, 8, 0)  # naive
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        self.assertEqual(occs[0].date.isoformat(), "2026-07-08")


class BuildOccurrencesDateOverrideTest(unittest.TestCase):
    """2026-07-16: the per-course date_overrides feature -- occurrences
    for an overridden date must use the shifted start/end (and duration,
    when the override sets one), while every OTHER date on the same course
    stays completely unaffected."""

    def setUp(self):
        from app.config import CourseDateOverride

        self.settings = make_settings(show_next_slots=3, show_next_days=42, min_notice_hours=2)
        self.course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120, capacity=10,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )

    def test_overridden_date_gets_the_shifted_start_and_kept_duration(self):
        tz = ZoneInfo(self.settings.timezone)
        now = datetime(2026, 7, 12, 8, 0, tzinfo=tz)
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        overridden = next(o for o in occs if o.date.isoformat() == "2026-07-18")
        self.assertEqual(overridden.start.strftime("%H:%M"), "09:45")
        self.assertEqual(overridden.end.strftime("%H:%M"), "11:45")  # 120min kept

    def test_other_dates_on_the_same_course_are_unaffected(self):
        tz = ZoneInfo(self.settings.timezone)
        now = datetime(2026, 7, 12, 8, 0, tzinfo=tz)
        occs = build_occurrences(self.course, self.settings, now, zero_capacity, no_conflict)
        other = next(o for o in occs if o.date.isoformat() == "2026-07-25")
        self.assertEqual(other.start.strftime("%H:%M"), "10:45")
        self.assertEqual(other.end.strftime("%H:%M"), "12:45")

    def test_override_with_explicit_duration_changes_the_end_too(self):
        from app.config import CourseDateOverride

        course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120, capacity=10,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45", duration_minutes=60),),
        )
        tz = ZoneInfo(self.settings.timezone)
        now = datetime(2026, 7, 12, 8, 0, tzinfo=tz)
        occs = build_occurrences(course, self.settings, now, zero_capacity, no_conflict)
        overridden = next(o for o in occs if o.date.isoformat() == "2026-07-18")
        self.assertEqual(overridden.end.strftime("%H:%M"), "10:45")


if __name__ == "__main__":
    unittest.main()
