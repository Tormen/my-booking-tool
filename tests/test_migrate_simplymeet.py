import tempfile
import unittest
from datetime import date

from app.migrate_simplymeet import _match_course, plan_import, run_migration
from app.security import hash_email_for_erasure
from app.storage import STATUS_CANCELED_BY_GUEST, STATUS_CONFIRMED, Store

from .helpers import make_course, make_settings


def _row(**overrides):
    row = {
        "id": "1000001",
        "Date and time": "2026-01-15 17:15",
        "Duration": "100",
        "Client": "Jane Doe",
        "Client phone number": "",
        "Client email": "jane@example.com",
        "Meeting type": "Dynamic Ashtanga Vinyasa Yoga",
        "Meeting name": "Dynamic Ashtanga Vinyasa Yoga",
        "Location": "Example Gym",
        "User name ": "the operator (admin@example.org)",
        "Notes": "",
        "Is canceled": "No",
        "Cancellation time": "",
        "Other participants": "",
        "Payment status": "",
        "Payment amount": "",
        "Payment currency": "",
        "Payment system": "",
        "Payment reverence id": "",
    }
    row.update(overrides)
    return row


class MigrateSimplymeetTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-wed", title="Dynamic Ashtanga Vinyasa Yoga")
        self.settings = make_settings(courses=(self.course,))
        self.today = date(2026, 7, 6)

    def tearDown(self):
        self._tmp.cleanup()


class PlanImportTest(MigrateSimplymeetTestBase):
    def test_past_confirmed_row_is_planned(self):
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)
        plan = report.planned[0]
        self.assertEqual(plan.simplymeet_id, "1000001")
        self.assertEqual(plan.email, "jane@example.com")
        self.assertEqual(plan.course_shortname, "yoga-wed")
        self.assertEqual(plan.occurrence_date, "2026-01-15")
        self.assertEqual(plan.status, STATUS_CONFIRMED)
        self.assertEqual(plan.registered_at, "2026-01-15T00:00:00")
        self.assertEqual(plan.canceled_at, "")

    def test_future_row_is_skipped(self):
        row = _row(**{"Date and time": "2026-07-10 17:15"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_future, 1)

    def test_today_itself_counts_as_future_not_history(self):
        row = _row(**{"Date and time": "2026-07-06 17:15"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_future, 1)

    def test_unmatched_meeting_type_is_skipped_and_reported(self):
        row = _row(**{"Meeting type": "Some Retired Class"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_unmatched_course, ["Some Retired Class"])

    def test_whitespace_only_title_difference_matches_and_is_flagged(self):
        row = _row(**{"Meeting type": "  Dynamic  Ashtanga Vinyasa Yoga "})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.planned[0].course_shortname, "yoga-wed")
        self.assertEqual(len(report.fuzzy_matched_courses), 1)

    def test_slightly_reworded_title_fuzzy_matches_and_is_flagged(self):
        # Real-world case (2026-07-06): a course title in settings.toml
        # drifted slightly from what the SimplyMeet.me export recorded.
        course = make_course(
            shortname="lux-wed-mindfulness",
            title="DBG-only WED@Lux - Mindfulness Session (Breathing / Pranayama)",
        )
        settings = make_settings(courses=(self.course, course))
        row = _row(**{
            "Meeting type": "DBG-only WED@Lux - Mindfulness Session (often Breathing / Pranayama)",
        })
        report = plan_import([row], settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.planned[0].course_shortname, "lux-wed-mindfulness")
        self.assertEqual(len(report.fuzzy_matched_courses), 1)
        self.assertIn("VERIFY", report.fuzzy_matched_courses[0])

    def test_wildly_different_title_is_not_fuzzy_matched(self):
        row = _row(**{"Meeting type": "Completely Unrelated Thing"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.fuzzy_matched_courses, [])
        self.assertEqual(report.skipped_unmatched_course, ["Completely Unrelated Thing"])

    def test_ambiguous_between_two_similar_courses_is_not_guessed(self):
        course_a = make_course(shortname="a", title="Yoga Session Alpha")
        course_b = make_course(shortname="b", title="Yoga Session Beta")
        settings = make_settings(courses=(course_a, course_b))
        row = _row(**{"Meeting type": "Yoga Session Alph"})  # close to BOTH
        report = plan_import([row], settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(len(report.ambiguous_course_matches), 1)

    def test_missing_email_is_skipped(self):
        row = _row(**{"Client email": ""})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_missing_email, 1)

    def test_canceled_row_maps_to_canceled_by_guest(self):
        row = _row(**{"Is canceled": "Yes", "Cancellation time": "2026-01-10 09:30"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        plan = report.planned[0]
        self.assertEqual(plan.status, STATUS_CANCELED_BY_GUEST)
        self.assertEqual(plan.canceled_by, "guest")
        self.assertEqual(plan.canceled_at, "2026-01-10T09:30:00")

    def test_canceled_row_without_cancellation_time_uses_placeholder(self):
        row = _row(**{"Is canceled": "Yes", "Cancellation time": ""})
        report = plan_import([row], self.settings, self.store, today=self.today)
        plan = report.planned[0]
        self.assertEqual(plan.canceled_at, "2026-01-15T00:00:00")

    def test_already_imported_registration_id_is_skipped(self):
        user = self.store.upsert_user_for_booking("jane@example.com", "Jane Doe")
        self.store.import_historical_registration(
            registration_id="simplymeet-1000001",
            course_shortname="yoga-wed",
            occurrence_date="2026-01-15",
            user_id=user.user_id,
            status=STATUS_CONFIRMED,
            registered_at="2026-01-15T00:00:00",
        )
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_already_imported, 1)

    def test_erased_email_is_skipped_not_recreated(self):
        user = self.store.upsert_user_for_booking("jane@example.com", "Jane Doe")
        hashed = hash_email_for_erasure(user.email, self.settings.erasure_pepper)
        self.store.erase_user(user.user_id, hashed)

        report = plan_import([_row()], self.settings, self.store, today=self.today)
        self.assertEqual(report.planned, [])
        self.assertEqual(report.skipped_erased_email, 1)

    def test_other_participants_become_linked_guest_plans(self):
        row = _row(**{"Other participants": "extra@example.com; "})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 2)
        self.assertEqual(report.guests_imported, 1)
        leader = next(p for p in report.planned if p.email == "jane@example.com")
        guest = next(p for p in report.planned if p.email == "extra@example.com")
        self.assertEqual(leader.party_id, guest.party_id)
        self.assertNotEqual(leader.party_id, "")
        self.assertEqual(leader.invited_by_email, "")
        self.assertEqual(guest.invited_by_email, "jane@example.com")
        self.assertNotEqual(leader.registration_id, guest.registration_id)

    def test_multiple_other_participants_all_become_guests(self):
        row = _row(**{"Other participants": "one@example.com; two@example.com;three@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.guests_imported, 3)
        self.assertEqual(len(report.planned), 4)
        party_ids = {p.party_id for p in report.planned}
        self.assertEqual(len(party_ids), 1)  # everyone in the same party

    def test_guest_matching_leader_email_is_skipped_as_duplicate(self):
        row = _row(**{"Other participants": "jane@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)  # leader only, no party
        self.assertEqual(report.planned[0].party_id, "")
        self.assertEqual(report.skipped_guest_duplicate, 1)

    def test_duplicate_guest_emails_on_same_row_only_counted_once(self):
        row = _row(**{"Other participants": "extra@example.com; extra@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(report.guests_imported, 1)
        self.assertEqual(report.skipped_guest_duplicate, 1)

    def test_malformed_guest_email_is_skipped(self):
        row = _row(**{"Other participants": "not-an-email"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)  # leader only
        self.assertEqual(report.skipped_guest_malformed, 1)

    def test_erased_guest_email_is_skipped_not_recreated(self):
        guest_user = self.store.upsert_user_for_booking("extra@example.com", "Extra")
        hashed = hash_email_for_erasure(guest_user.email, self.settings.erasure_pepper)
        self.store.erase_user(guest_user.user_id, hashed)

        row = _row(**{"Other participants": "extra@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)  # leader only
        self.assertEqual(report.skipped_guest_erased, 1)

    def test_blank_other_participants_is_a_plain_solo_row(self):
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.planned[0].party_id, "")
        self.assertEqual(report.guests_imported, 0)


class RunMigrationTest(MigrateSimplymeetTestBase):
    def test_creates_new_user_and_registration(self):
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        written = run_migration(report.planned, self.store)
        self.assertEqual(written, 1)

        user = self.store.find_user_by_email("jane@example.com")
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "Jane Doe")
        reg = self.store.find_by_id("simplymeet-1000001")
        self.assertIsNotNone(reg)
        self.assertEqual(reg.user_id, user.user_id)

    def test_reuses_existing_user_without_overwriting_name(self):
        self.store.upsert_user_for_booking("jane@example.com", "Preferred Name")
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        run_migration(report.planned, self.store)

        user = self.store.find_user_by_email("jane@example.com")
        self.assertEqual(user.name, "Preferred Name")

    def test_rerunning_is_idempotent(self):
        report = plan_import([_row()], self.settings, self.store, today=self.today)
        first = run_migration(report.planned, self.store)
        second = run_migration(report.planned, self.store)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        matches = [r for r in self.store.all_registrations() if r.registration_id == "simplymeet-1000001"]
        self.assertEqual(len(matches), 1)

    def test_two_rows_same_email_share_one_user(self):
        rows = [
            _row(id="1", **{"Date and time": "2026-01-08 17:15"}),
            _row(id="2", **{"Date and time": "2026-01-15 17:15"}),
        ]
        report = plan_import(rows, self.settings, self.store, today=self.today)
        written = run_migration(report.planned, self.store)
        self.assertEqual(written, 2)
        user = self.store.find_user_by_email("jane@example.com")
        regs = self.store.registrations_for_user(user.user_id)
        self.assertEqual(len(regs), 2)

    def test_guest_gets_own_account_and_linked_registration(self):
        row = _row(**{"Other participants": "extra@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        written = run_migration(report.planned, self.store)
        self.assertEqual(written, 2)

        leader = self.store.find_user_by_email("jane@example.com")
        guest = self.store.find_user_by_email("extra@example.com")
        self.assertIsNotNone(guest)
        self.assertEqual(guest.name, "Guest")  # no name known from SimplyMeet.me's export

        leader_reg = self.store.find_by_id("simplymeet-1000001")
        guest_reg = self.store.find_by_id("simplymeet-1000001-guest-0")
        self.assertIsNotNone(guest_reg)
        self.assertEqual(leader_reg.party_id, guest_reg.party_id)
        self.assertEqual(guest_reg.invited_by_user_id, leader.user_id)
        self.assertEqual(leader_reg.invited_by_user_id, "")
        self.assertEqual(guest_reg.status, leader_reg.status)

    def test_guest_reuses_existing_accounts_real_name(self):
        self.store.upsert_user_for_booking("extra@example.com", "Real Guest Name")
        row = _row(**{"Other participants": "extra@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        run_migration(report.planned, self.store)
        guest = self.store.find_user_by_email("extra@example.com")
        self.assertEqual(guest.name, "Real Guest Name")

    def test_rerunning_with_guests_is_idempotent(self):
        row = _row(**{"Other participants": "extra@example.com"})
        report = plan_import([row], self.settings, self.store, today=self.today)
        first = run_migration(report.planned, self.store)
        second_report = plan_import([row], self.settings, self.store, today=self.today)
        second = run_migration(second_report.planned, self.store)
        self.assertEqual(first, 2)
        self.assertEqual(second_report.planned, [])  # everything already imported
        self.assertEqual(second, 0)


if __name__ == "__main__":
    unittest.main()
