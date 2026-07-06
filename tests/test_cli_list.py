import unittest
from datetime import date

from app.cli_list import filter_by_date


def _row(occurrence_date: str) -> dict:
    return {"registration_id": occurrence_date, "occurrence_date": occurrence_date}


class FilterByDateTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 6)
        self.rows = [_row("2026-07-01"), _row("2026-07-06"), _row("2026-07-10")]

    def test_neither_flag_returns_everything_unfiltered(self):
        # Preserves `my-bt list`'s exact pre-existing default behavior when
        # neither --upcoming nor --past is passed.
        result = filter_by_date(self.rows, upcoming=False, past=False, today=self.today)
        self.assertEqual(result, self.rows)

    def test_upcoming_includes_today_and_future_only(self):
        result = filter_by_date(self.rows, upcoming=True, past=False, today=self.today)
        self.assertEqual([r["occurrence_date"] for r in result], ["2026-07-06", "2026-07-10"])

    def test_past_excludes_today(self):
        result = filter_by_date(self.rows, upcoming=False, past=True, today=self.today)
        self.assertEqual([r["occurrence_date"] for r in result], ["2026-07-01"])

    def test_upcoming_with_no_matching_rows(self):
        rows = [_row("2020-01-01")]
        result = filter_by_date(rows, upcoming=True, past=False, today=self.today)
        self.assertEqual(result, [])

    def test_past_with_no_matching_rows(self):
        rows = [_row("2030-01-01")]
        result = filter_by_date(rows, upcoming=False, past=True, today=self.today)
        self.assertEqual(result, [])

    def test_defaults_to_real_today_when_not_passed(self):
        # No `today` override: uses date.today() -- a row far in the past
        # must never show up as "upcoming".
        result = filter_by_date([_row("2000-01-01")], upcoming=True, past=False)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
