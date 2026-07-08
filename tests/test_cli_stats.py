import unittest
from datetime import date

from app.cli_stats import (
    compute_last_and_next_slot, compute_totals_with_distinct, compute_year_period_stats,
)


def _reg(course: str, status: str, occurrence_date: str, user_id: str) -> dict:
    return {"course_shortname": course, "status": status, "occurrence_date": occurrence_date, "user_id": user_id}


class ComputeTotalsWithDistinctTest(unittest.TestCase):
    def test_counts_and_distinct_users_per_course_and_status(self):
        rows = [
            _reg("yoga", "confirmed", "2026-01-01", "u1"),
            _reg("yoga", "confirmed", "2026-02-01", "u1"),  # same user, second booking
            _reg("yoga", "confirmed", "2026-03-01", "u2"),
            _reg("yoga", "canceled_by_guest", "2026-01-01", "u3"),
        ]
        result = compute_totals_with_distinct(rows)
        self.assertEqual(result[("yoga", "confirmed")], (3, 2))
        self.assertEqual(result[("yoga", "canceled_by_guest")], (1, 1))

    def test_empty_rows_gives_empty_result(self):
        self.assertEqual(compute_totals_with_distinct([]), {})

    def test_separate_courses_are_not_mixed(self):
        rows = [
            _reg("yoga", "confirmed", "2026-01-01", "u1"),
            _reg("pilates", "confirmed", "2026-01-01", "u1"),
        ]
        result = compute_totals_with_distinct(rows)
        self.assertEqual(result[("yoga", "confirmed")], (1, 1))
        self.assertEqual(result[("pilates", "confirmed")], (1, 1))


class ComputeLastAndNextSlotTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 8)

    def test_last_slot_is_most_recent_date_today_or_earlier(self):
        rows = [
            _reg("yoga", "confirmed", "2026-06-01", "u1"),
            _reg("yoga", "confirmed", "2026-07-01", "u2"),
            _reg("yoga", "confirmed", "2026-07-08", "u3"),  # today itself counts as "last"
        ]
        result = compute_last_and_next_slot(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["last_slot"], ("2026-07-08", 1))

    def test_next_slot_is_soonest_date_strictly_after_today(self):
        rows = [
            _reg("yoga", "confirmed", "2026-07-08", "u1"),
            _reg("yoga", "confirmed", "2026-07-15", "u2"),
            _reg("yoga", "confirmed", "2026-07-15", "u3"),
            _reg("yoga", "confirmed", "2026-07-22", "u4"),
        ]
        result = compute_last_and_next_slot(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["next_slot"], ("2026-07-15", 2))

    def test_no_past_rows_gives_none_last_slot(self):
        rows = [_reg("yoga", "confirmed", "2026-07-22", "u1")]
        result = compute_last_and_next_slot(rows, today=self.today)
        self.assertIsNone(result[("yoga", "confirmed")]["last_slot"])

    def test_no_future_rows_gives_none_next_slot(self):
        rows = [_reg("yoga", "confirmed", "2026-01-01", "u1")]
        result = compute_last_and_next_slot(rows, today=self.today)
        self.assertIsNone(result[("yoga", "confirmed")]["next_slot"])

    def test_separate_status_gets_its_own_slots(self):
        # 2026-07-08, the operator: "you will have this info per status listed" --
        # a canceled row on one date must not bleed into confirmed's own
        # last/next slot for the same course.
        rows = [
            _reg("yoga", "confirmed", "2026-07-01", "u1"),
            _reg("yoga", "canceled_by_guest", "2026-07-05", "u2"),
        ]
        result = compute_last_and_next_slot(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["last_slot"], ("2026-07-01", 1))
        self.assertEqual(result[("yoga", "canceled_by_guest")]["last_slot"], ("2026-07-05", 1))

    def test_course_with_no_rows_absent_from_result(self):
        result = compute_last_and_next_slot([], today=self.today)
        self.assertEqual(result, {})


class ComputeYearPeriodStatsTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 8)

    def test_this_year_to_date_includes_jan_1_through_today(self):
        rows = [
            _reg("yoga", "confirmed", "2026-01-01", "u1"),
            _reg("yoga", "confirmed", "2026-07-08", "u2"),
            _reg("yoga", "confirmed", "2026-07-09", "u3"),  # tomorrow -- excluded
        ]
        result = compute_year_period_stats(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["this_year_to_date"], (2, 2))

    def test_last_year_includes_the_whole_prior_calendar_year_only(self):
        rows = [
            _reg("yoga", "confirmed", "2025-01-01", "u1"),
            _reg("yoga", "confirmed", "2025-12-31", "u2"),
            _reg("yoga", "confirmed", "2024-12-31", "u3"),  # two years ago -- excluded
            _reg("yoga", "confirmed", "2026-01-01", "u4"),  # this year -- excluded
        ]
        result = compute_year_period_stats(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["last_year"], (2, 2))

    def test_last_year_to_date_is_the_same_month_day_boundary_last_year(self):
        rows = [
            _reg("yoga", "confirmed", "2025-07-08", "u1"),  # same day last year -- included
            _reg("yoga", "confirmed", "2025-07-09", "u2"),  # one day later last year -- excluded
        ]
        result = compute_year_period_stats(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["last_year_to_date"], (1, 1))

    def test_feb_29_today_falls_back_to_feb_28_last_year(self):
        rows = [_reg("yoga", "confirmed", "2023-02-28", "u1")]
        result = compute_year_period_stats(rows, today=date(2024, 2, 29))
        self.assertEqual(result[("yoga", "confirmed")]["last_year_to_date"], (1, 1))

    def test_same_user_twice_in_a_window_counts_once_as_distinct(self):
        rows = [
            _reg("yoga", "confirmed", "2026-01-01", "u1"),
            _reg("yoga", "confirmed", "2026-02-01", "u1"),
        ]
        result = compute_year_period_stats(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["this_year_to_date"], (2, 1))

    def test_status_kept_separate(self):
        rows = [
            _reg("yoga", "confirmed", "2026-01-01", "u1"),
            _reg("yoga", "canceled_by_guest", "2026-01-01", "u2"),
        ]
        result = compute_year_period_stats(rows, today=self.today)
        self.assertEqual(result[("yoga", "confirmed")]["this_year_to_date"], (1, 1))
        self.assertEqual(result[("yoga", "canceled_by_guest")]["this_year_to_date"], (1, 1))

    def test_window_with_nothing_in_it_is_absent(self):
        result = compute_year_period_stats([], today=self.today)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
