import tempfile
import unittest
from datetime import date

from app.cli_list import (
    annotate_admin_party_label, annotate_party_info, assign_short_ids, build_clean_registration_view,
    build_clean_user_view, compute_last_confirmed_course, compute_times_booked_counts, filter_by_date,
    merge_archived_for_display, resolve_short_id,
)
from app.security import hash_email_for_erasure, hash_token, new_token
from app.storage import Store

from .helpers import make_settings


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


class AnnotateAdminPartyLabelTest(unittest.TestCase):
    """Mirrors app/webapp.py's admin_overview() Guests column exactly --
    see annotate_admin_party_label's own docstring for why this is a
    SEPARATE, capitalized format from AnnotatePartyInfoTest above."""

    def setUp(self):
        self.users = {
            "leader-1": {"user_id": "leader-1", "name": "Leo Leader", "email": "leo@example.com"},
            "guest-1": {"user_id": "guest-1", "name": "Gil Guest", "email": "gil@example.com"},
            "placeholder-1": {"user_id": "placeholder-1", "name": "Guest", "email": "placeholder@example.com"},
        }

    def test_solo_booking_gets_blank_label(self):
        rows = [{"registration_id": "r1", "user_id": "leader-1", "party_id": "", "invited_by_user_id": ""}]
        result = annotate_admin_party_label(rows, self.users)
        self.assertEqual(result[0]["party_label"], "")

    def test_leader_row_says_host(self):
        rows = [
            {"registration_id": "r1", "user_id": "leader-1", "party_id": "p1", "invited_by_user_id": ""},
            {"registration_id": "r2", "user_id": "guest-1", "party_id": "p1", "invited_by_user_id": "leader-1"},
        ]
        result = annotate_admin_party_label(rows, self.users)
        leader_row = next(r for r in result if r["user_id"] == "leader-1")
        self.assertEqual(leader_row["party_label"], "Host (+1 guest)")

    def test_guest_row_says_guest_of_leader_name(self):
        rows = [
            {"registration_id": "r1", "user_id": "leader-1", "party_id": "p1", "invited_by_user_id": ""},
            {"registration_id": "r2", "user_id": "guest-1", "party_id": "p1", "invited_by_user_id": "leader-1"},
        ]
        result = annotate_admin_party_label(rows, self.users)
        guest_row = next(r for r in result if r["user_id"] == "guest-1")
        self.assertEqual(guest_row["party_label"], "Guest of Leo Leader")

    def test_guest_of_placeholder_name_falls_back_to_email(self):
        # 2026-07-08, the operator: "Is guest of Guest correct??" -- must read
        # "Guest of placeholder@example.com", never "Guest of Guest".
        rows = [{"registration_id": "r1", "user_id": "guest-1", "party_id": "", "invited_by_user_id": "placeholder-1"}]
        result = annotate_admin_party_label(rows, self.users)
        self.assertEqual(result[0]["party_label"], "Guest of placeholder@example.com")

    def test_unknown_leader_falls_back_gracefully(self):
        rows = [{"registration_id": "r1", "user_id": "guest-1", "party_id": "", "invited_by_user_id": "ghost"}]
        result = annotate_admin_party_label(rows, self.users)
        self.assertEqual(result[0]["party_label"], "Guest of (unknown)")


class ComputeTimesBookedCountsTest(unittest.TestCase):
    def test_splits_up_to_now_from_total(self):
        rows = [
            {"user_id": "u1", "occurrence_date": "2026-01-01"},
            {"user_id": "u1", "occurrence_date": "2026-12-31"},
            {"user_id": "u2", "occurrence_date": "2026-06-15"},
        ]
        total, upto_now = compute_times_booked_counts(rows, today=date(2026, 6, 15))
        self.assertEqual(total["u1"], 2)
        self.assertEqual(upto_now["u1"], 1)
        self.assertEqual(total["u2"], 1)
        self.assertEqual(upto_now["u2"], 1)  # today's own session counts

    def test_unknown_user_has_zero_counts(self):
        total, upto_now = compute_times_booked_counts([], today=date(2026, 1, 1))
        self.assertEqual(total["nobody"], 0)
        self.assertEqual(upto_now["nobody"], 0)


class BuildCleanRegistrationViewTest(unittest.TestCase):
    def setUp(self):
        self.users = {"u1": {"user_id": "u1", "name": "Ada", "email": "ada@example.com"}}
        self.row = {
            "registration_id": "reg-1", "user_id": "u1", "course_shortname": "yoga-class-1",
            "occurrence_date": "2026-07-10", "status": "confirmed", "registered_at": "2026-07-01T00:00:00",
            "party_id": "", "invited_by_user_id": "",
        }

    def test_shows_admin_style_columns(self):
        result = build_clean_registration_view([self.row], self.users, [self.row], today=date(2026, 7, 5))
        self.assertEqual(result[0]["status"], "Confirmed")
        self.assertEqual(result[0]["course"], "yoga-class-1")
        self.assertEqual(result[0]["date"], "2026-07-10")
        self.assertEqual(result[0]["name"], "Ada")
        self.assertEqual(result[0]["email"], "ada@example.com")
        self.assertEqual(result[0]["times_booked"], "0/1")
        self.assertEqual(result[0]["guests"], "")

    def test_date_is_the_first_column(self):
        # 2026-07-08, the operator: "put this as first column" (the date column).
        result = build_clean_registration_view([self.row], self.users, [self.row], today=date(2026, 7, 5))
        self.assertEqual(next(iter(result[0])), "date")

    def test_registered_column_hidden_by_default(self):
        # 2026-07-08, the operator: "don't show when they registered by default
        # but only with my-bt list -V".
        result = build_clean_registration_view([self.row], self.users, [self.row], today=date(2026, 7, 5))
        self.assertNotIn("registered", result[0])

    def test_registered_column_shown_when_verbose(self):
        result = build_clean_registration_view(
            [self.row], self.users, [self.row], today=date(2026, 7, 5), verbose=True,
        )
        self.assertEqual(result[0]["registered"], "2026-07-01")

    def test_no_raw_ids_leak_into_the_output_columns(self):
        result = build_clean_registration_view([self.row], self.users, [self.row], today=date(2026, 7, 5))
        self.assertNotIn("user_id", result[0])
        self.assertNotIn("party_id", result[0])
        self.assertNotIn("invited_by_user_id", result[0])
        self.assertNotIn("registration_id", result[0])

    def test_unknown_user_shows_placeholder(self):
        row = {**self.row, "user_id": "ghost"}
        result = build_clean_registration_view([row], self.users, [row], today=date(2026, 7, 5))
        self.assertEqual(result[0]["name"], "(unknown)")
        self.assertEqual(result[0]["email"], "(unknown)")

    def test_short_id_column_populated_when_given(self):
        result = build_clean_registration_view(
            [self.row], self.users, [self.row], today=date(2026, 7, 5),
            short_ids_by_reg_id={"reg-1": "abc12345"},
        )
        self.assertEqual(result[0]["id"], "abc12345")

    def test_short_id_column_blank_when_not_given(self):
        result = build_clean_registration_view([self.row], self.users, [self.row], today=date(2026, 7, 5))
        self.assertEqual(result[0]["id"], "")

    def test_totals_computed_over_all_rows_not_just_displayed_rows(self):
        # A row narrowed OUT of `rows` (e.g. by --course) must still count
        # toward this user's overall times-booked total.
        other_row = {**self.row, "registration_id": "reg-2", "course_shortname": "other-course"}
        result = build_clean_registration_view(
            [self.row], self.users, [self.row, other_row], today=date(2026, 7, 5),
        )
        self.assertEqual(result[0]["times_booked"], "0/2")


class BuildCleanUserViewTest(unittest.TestCase):
    def setUp(self):
        self.row = {
            "user_id": "u1", "name": "Ada", "email": "ada@example.com",
            "created_at": "2026-01-01T00:00:00", "last_login_at": "2026-07-01T09:30:00",
        }

    def test_shows_name_email_joined_last_login(self):
        result = build_clean_user_view([self.row])
        self.assertEqual(result[0]["name"], "Ada")
        self.assertEqual(result[0]["email"], "ada@example.com")
        self.assertEqual(result[0]["joined"], "2026-01-01")
        self.assertEqual(result[0]["last_login"], "2026-07-01")

    def test_column_order_is_name_joined_last_login_last_course_email(self):
        # 2026-07-08, the operator: "please have name joined last_login
        # last_course email".
        result = build_clean_user_view([self.row])
        self.assertEqual(list(result[0].keys()), ["name", "joined", "last_login", "last_course", "email"])

    def test_dates_are_date_only_by_default_even_with_a_real_time_of_day(self):
        # 2026-07-08, the operator: "please only use YYYY-MM-DD for the columns
        # and only with -V show also the timestamp" -- unlike the
        # shared format_display_timestamp(), no time-of-day leaks through
        # here even when last_login_at isn't exactly midnight.
        result = build_clean_user_view([self.row])
        self.assertEqual(result[0]["last_login"], "2026-07-01")

    def test_verbose_shows_full_timestamp(self):
        result = build_clean_user_view([self.row], verbose=True)
        self.assertEqual(result[0]["joined"], "2026-01-01")  # midnight -- date only either way
        self.assertEqual(result[0]["last_login"], "2026-07-01_0930.00")

    def test_never_logged_in_shows_placeholder(self):
        rows = [{"user_id": "u1", "name": "Ada", "email": "ada@example.com", "created_at": "", "last_login_at": ""}]
        result = build_clean_user_view(rows)
        self.assertEqual(result[0]["last_login"], "(never)")

    def test_no_user_id_in_output(self):
        rows = [{"user_id": "u1", "name": "Ada", "email": "ada@example.com"}]
        result = build_clean_user_view(rows)
        self.assertNotIn("user_id", result[0])

    def test_last_course_populated_from_lookup(self):
        result = build_clean_user_view([self.row], last_course_by_user={"u1": "yoga-class-1"})
        self.assertEqual(result[0]["last_course"], "yoga-class-1")

    def test_last_course_blank_when_absent_from_lookup(self):
        result = build_clean_user_view([self.row], last_course_by_user={})
        self.assertEqual(result[0]["last_course"], "")

    def test_erased_email_hash_shown_as_placeholder(self):
        # 2026-07-08, the operator: "please only make email as wide as needed!"
        # -- root cause was an erased user's ~70-char hashed email
        # (app.security.hash_email_for_erasure) being shown in full.
        row = {**self.row, "email": hash_email_for_erasure("ada@example.com", b"pepper"), "name": "[erased]"}
        result = build_clean_user_view([row])
        self.assertEqual(result[0]["email"], "[erased]")

    def test_ordinary_email_unaffected(self):
        result = build_clean_user_view([self.row])
        self.assertEqual(result[0]["email"], "ada@example.com")


class ComputeLastConfirmedCourseTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 8)

    def _reg(self, user_id: str, course: str, occurrence_date: str, status: str = "confirmed") -> dict:
        return {"user_id": user_id, "course_shortname": course, "occurrence_date": occurrence_date, "status": status}

    def test_picks_most_recent_confirmed_occurrence_today_or_earlier(self):
        rows = [
            self._reg("u1", "yoga-class-1", "2026-06-01"),
            self._reg("u1", "yoga-class-2", "2026-07-08"),  # today -- counts
            self._reg("u1", "yoga-class-3", "2026-07-09"),  # tomorrow -- excluded
        ]
        result = compute_last_confirmed_course(rows, today=self.today)
        self.assertEqual(result["u1"], "yoga-class-2")

    def test_ignores_non_confirmed_statuses(self):
        rows = [
            self._reg("u1", "yoga-class-1", "2026-07-01"),
            self._reg("u1", "yoga-class-2", "2026-07-05", status="canceled_by_guest"),
        ]
        result = compute_last_confirmed_course(rows, today=self.today)
        self.assertEqual(result["u1"], "yoga-class-1")

    def test_no_qualifying_row_absent_from_result(self):
        rows = [self._reg("u1", "yoga-class-1", "2026-07-20")]  # future only
        result = compute_last_confirmed_course(rows, today=self.today)
        self.assertNotIn("u1", result)

    def test_separate_users_kept_separate(self):
        rows = [
            self._reg("u1", "yoga-class-1", "2026-07-01"),
            self._reg("u2", "yoga-class-2", "2026-07-02"),
        ]
        result = compute_last_confirmed_course(rows, today=self.today)
        self.assertEqual(result["u1"], "yoga-class-1")
        self.assertEqual(result["u2"], "yoga-class-2")


class AssignShortIdsTest(unittest.TestCase):
    def test_assigns_min_length_prefix_when_no_collision(self):
        ids = ["a1b2c3d4-0000-0000-0000-000000000001", "ffffffff-0000-0000-0000-000000000002"]
        result = assign_short_ids(ids, min_length=8)
        self.assertEqual(result[ids[0]], "a1b2c3d4")
        self.assertEqual(result[ids[1]], "ffffffff")

    def test_extends_length_on_collision(self):
        # Both share the same first 8 hex chars once dashes are stripped --
        # must grow past 8 for BOTH, exactly like git's own abbreviation.
        ids = ["aaaaaaaa-1111-0000-0000-000000000001", "aaaaaaaa-2222-0000-0000-000000000002"]
        result = assign_short_ids(ids, min_length=8)
        self.assertNotEqual(result[ids[0]], result[ids[1]])
        self.assertTrue(result[ids[0]].startswith("aaaaaaaa"))
        self.assertGreater(len(result[ids[0]]), 8)

    def test_empty_input(self):
        self.assertEqual(assign_short_ids([]), {})

    def test_stable_across_repeated_calls(self):
        ids = ["a1b2c3d4-0000-0000-0000-000000000001", "ffffffff-0000-0000-0000-000000000002"]
        self.assertEqual(assign_short_ids(ids), assign_short_ids(ids))


class ResolveShortIdTest(unittest.TestCase):
    def setUp(self):
        self.ids = ["a1b2c3d4-0000-0000-0000-000000000001", "ffffffff-0000-0000-0000-000000000002"]

    def test_unique_prefix_resolves(self):
        resolved, candidates = resolve_short_id("a1b2c3d4", self.ids)
        self.assertEqual(resolved, self.ids[0])
        self.assertEqual(candidates, [])

    def test_full_id_resolves(self):
        resolved, candidates = resolve_short_id(self.ids[1], self.ids)
        self.assertEqual(resolved, self.ids[1])

    def test_no_match_returns_none_and_no_candidates(self):
        resolved, candidates = resolve_short_id("zzzzzzzz", self.ids)
        self.assertIsNone(resolved)
        self.assertEqual(candidates, [])

    def test_ambiguous_prefix_returns_all_candidates(self):
        ids = ["aaaaaaaa-1111-0000-0000-000000000001", "aaaaaaaa-2222-0000-0000-000000000002"]
        resolved, candidates = resolve_short_id("aaaaaaaa", ids)
        self.assertIsNone(resolved)
        self.assertCountEqual(candidates, ids)

    def test_case_insensitive(self):
        resolved, _candidates = resolve_short_id("A1B2C3D4", self.ids)
        self.assertEqual(resolved, self.ids[0])


class MergeArchivedForDisplayTest(unittest.TestCase):
    """Read-only equivalent of `my-bt admin dearchive` -- see
    merge_archived_for_display's own docstring. 2026-07-13, the operator: "/admin
    should [be] non-mutating" -- this is what both `my-bt list --all`/
    `--past` and admin_overview() now call instead of actually rewriting
    the CSVs on every load."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()

    def _erase(self, email: str) -> tuple[str, str]:
        """Same erasure simulation as tests/test_cli_history.py's own
        helper: creates a user with one booking, then erases them for
        real (hashing with this test's own erasure_pepper)."""
        user = self.store.upsert_user_for_booking(email, "Guest")
        reg = self.store.add_registration("c", "2026-01-01", user.user_id, hash_token(new_token()))
        hashed = hash_email_for_erasure(user.email, self.settings.erasure_pepper)
        self.store.erase_user(user.user_id, hashed)
        return user.user_id, reg.registration_id

    def test_relabels_archived_row_onto_the_live_rebooked_user(self):
        old_id, reg_id = self._erase("guest@example.com")
        new_user = self.store.upsert_user_for_booking("guest@example.com", "Guest")

        archived = self.store.read_registrations(scope="archived")
        result = merge_archived_for_display(self.store, self.settings, [new_user.__dict__], archived)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["registration_id"], reg_id)
        self.assertEqual(result[0]["user_id"], new_user.user_id)
        self.assertNotEqual(result[0]["user_id"], old_id)

    def test_does_not_write_to_disk(self):
        self._erase("guest@example.com")
        new_user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        archived_before = self.store.read_registrations(scope="archived")

        merge_archived_for_display(self.store, self.settings, [new_user.__dict__], archived_before)

        # Still there, untouched, on disk -- a real merge (`dearchive`)
        # would have removed it from the archived CSV entirely.
        self.assertEqual(self.store.read_registrations(scope="archived"), archived_before)

    def test_no_matching_live_user_leaves_row_unchanged(self):
        old_id, reg_id = self._erase("guest@example.com")
        archived = self.store.read_registrations(scope="archived")

        # No live rebook at all -- nobody to relabel onto.
        result = merge_archived_for_display(self.store, self.settings, [], archived)

        self.assertEqual(result[0]["registration_id"], reg_id)
        self.assertEqual(result[0]["user_id"], old_id)

    def test_unrelated_live_user_does_not_claim_someone_elses_history(self):
        self._erase("guest@example.com")
        other_user = self.store.upsert_user_for_booking("other@example.com", "Other")
        archived = self.store.read_registrations(scope="archived")

        result = merge_archived_for_display(self.store, self.settings, [other_user.__dict__], archived)

        self.assertNotEqual(result[0]["user_id"], other_user.user_id)


if __name__ == "__main__":
    unittest.main()
