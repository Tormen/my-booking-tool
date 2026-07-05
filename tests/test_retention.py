import tempfile
import unittest
from dataclasses import replace
from datetime import date

from app.retention import run_purge, should_purge
from app.security import hash_token, new_token
from app.storage import Store

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
        u = self.store.upsert_user(f"{occurrence_date}-{status}@x.com", "X", "h", "s")
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


if __name__ == "__main__":
    unittest.main()
