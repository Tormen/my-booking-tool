import tempfile
import unittest
from datetime import date

from app.migrate_simplymeet import plan_import, run_migration
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

    def test_other_participants_are_flagged_but_still_imported(self):
        row = _row(**{"Other participants": "extra@example.com; "})
        report = plan_import([row], self.settings, self.store, today=self.today)
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.rows_with_other_participants, 1)


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


if __name__ == "__main__":
    unittest.main()
