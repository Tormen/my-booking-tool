import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from app.retention import run_purge, should_purge
from app.security import hash_token, new_token
from app.storage import STATUS_PENDING_CONFIRMATION, Store

from .helpers import make_settings


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.settings = make_settings(retention_months=24, canceled_retention_months=6)
        self.today = date(2028, 1, 1)

    def tearDown(self):
        self._tmp.cleanup()

    def _reg(self, occurrence_date, status="confirmed"):
        u = self.store.upsert_user_for_booking(f"{occurrence_date}-{status}@x.com", "X")
        r = self.store.add_registration("c", occurrence_date, u.user_id, hash_token(new_token()))
        if status != "confirmed":
            self.store.cancel(r.registration_id, canceled_by="guest")
            r = self.store.find_by_id(r.registration_id)
        return r

    def test_confirmed_row_purged_after_retention_months(self):
        old = self._reg("2025-01-01")  # 36 months before self.today
        recent = self._reg("2027-06-01")  # 7 months before
        purged = run_purge(self.store, self.settings, today=self.today)
        self.assertEqual(purged, 1)
        remaining_dates = {r.occurrence_date for r in self.store.all_registrations()}
        self.assertEqual(remaining_dates, {"2027-06-01"})

    def test_canceled_row_purged_sooner_than_general_retention(self):
        # occurrence itself is well within the 24-month window, but it was
        # canceled 8 months ago (canceled_retention_months=6) -- should still
        # be purged by the "canceled sooner" rule.
        reg = self._reg("2027-11-01", status="canceled_by_guest")
        reg = replace(reg, canceled_at="2027-05-01T00:00:00+00:00")
        self.store.replace_all_registrations([reg])
        purged = run_purge(self.store, self.settings, today=self.today)
        self.assertEqual(purged, 1)

    def test_recently_canceled_row_survives(self):
        reg = self._reg("2027-11-01", status="canceled_by_guest")
        reg = replace(reg, canceled_at="2027-12-15T00:00:00+00:00")  # 2 weeks before self.today
        self.store.replace_all_registrations([reg])
        purged = run_purge(self.store, self.settings, today=self.today)
        self.assertEqual(purged, 0)

    def test_should_purge_boundary_is_inclusive(self):
        r = self._reg("2026-01-01")
        reg = self.store.find_by_id(r.registration_id)
        self.assertTrue(should_purge(reg, date(2028, 1, 1), self.settings))
        self.assertFalse(should_purge(reg, date(2027, 12, 31), self.settings))


class PendingConfirmationPurgeTest(unittest.TestCase):
    """STATUS_PENDING_CONFIRMATION rows follow a completely separate,
    hour-granularity rule (pending_confirmation_hours, default 48) --
    independent of retention_months/canceled_retention_months, which only
    ever apply to real (confirmed/waitlisted/canceled) bookings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(pending_confirmation_hours=48)

    def _pending_reg(self, registered_at: str):
        u = self.store.upsert_user_for_booking("newguest@example.org", "New Guest")
        r = self.store.add_registration(
            "c", "2099-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION
        )
        r = replace(r, registered_at=registered_at)
        self.store.replace_all_registrations([r])
        return self.store.find_by_id(r.registration_id)

    def test_survives_within_the_window(self):
        reg = self._pending_reg("2026-07-04T00:00:00+00:00")  # 24h before "now" below
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(should_purge(reg, now.date(), self.settings, now=now))

    def test_purged_once_past_the_window(self):
        reg = self._pending_reg("2026-07-01T00:00:00+00:00")  # way more than 48h before "now"
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(should_purge(reg, now.date(), self.settings, now=now))

    def test_far_future_occurrence_date_does_not_protect_a_stale_pending_row(self):
        # occurrence_date is 2099 (nowhere near retention_months) -- the
        # pending-specific rule must still fire; occurrence_date is
        # irrelevant to it entirely.
        reg = self._pending_reg("2026-07-01T00:00:00+00:00")
        purged = run_purge(self.store, self.settings, today=date(2026, 7, 5), now=datetime(2026, 7, 5, tzinfo=timezone.utc))
        self.assertEqual(purged, 1)
        self.assertEqual(self.store.all_registrations(), [])

    def test_a_real_confirmed_row_is_unaffected_by_the_pending_rule(self):
        u = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        self.store.add_registration("c", "2026-07-01", u.user_id, hash_token(new_token()))  # STATUS_CONFIRMED
        purged = run_purge(
            self.store, self.settings, today=date(2026, 7, 5), now=datetime(2026, 7, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(purged, 0)


if __name__ == "__main__":
    unittest.main()
