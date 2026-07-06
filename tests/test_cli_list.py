import unittest
from datetime import date

from app.cli_list import annotate_party_info, filter_by_date


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


class AnnotatePartyInfoTest(unittest.TestCase):
    def setUp(self):
        self.users = {
            "leader-1": {"user_id": "leader-1", "email": "leader@example.com"},
            "guest-1": {"user_id": "guest-1", "email": "guest@example.com"},
        }

    def test_solo_booking_gets_blank_party(self):
        rows = [{"registration_id": "r1", "user_id": "leader-1", "party_id": "", "invited_by_user_id": ""}]
        result = annotate_party_info(rows, self.users)
        self.assertEqual(result[0]["party"], "")

    def test_leader_row_shows_guest_count(self):
        rows = [
            {"registration_id": "r1", "user_id": "leader-1", "party_id": "p1", "invited_by_user_id": ""},
            {"registration_id": "r2", "user_id": "guest-1", "party_id": "p1", "invited_by_user_id": "leader-1"},
        ]
        result = annotate_party_info(rows, self.users)
        leader_row = next(r for r in result if r["user_id"] == "leader-1")
        self.assertEqual(leader_row["party"], "+1 guest")

    def test_guest_row_shows_who_they_are_a_guest_of(self):
        rows = [
            {"registration_id": "r1", "user_id": "leader-1", "party_id": "p1", "invited_by_user_id": ""},
            {"registration_id": "r2", "user_id": "guest-1", "party_id": "p1", "invited_by_user_id": "leader-1"},
        ]
        result = annotate_party_info(rows, self.users)
        guest_row = next(r for r in result if r["user_id"] == "guest-1")
        self.assertEqual(guest_row["party"], "guest of leader@example.com")

    def test_does_not_mutate_input_rows(self):
        rows = [{"registration_id": "r1", "user_id": "leader-1", "party_id": "", "invited_by_user_id": ""}]
        annotate_party_info(rows, self.users)
        self.assertNotIn("party", rows[0])

    def test_missing_party_fields_default_to_blank(self):
        # Rows from before this feature existed have no party_id/
        # invited_by_user_id key at all (old CSV header) -- must not crash.
        rows = [{"registration_id": "r1", "user_id": "leader-1"}]
        result = annotate_party_info(rows, self.users)
        self.assertEqual(result[0]["party"], "")


if __name__ == "__main__":
    unittest.main()
