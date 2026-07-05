import tempfile
import unittest
from datetime import date

from app.erasure import erase_user_by_email
from app.security import hash_token, is_erased_email, new_token
from app.storage import STATUS_CANCELED_BY_GUEST, STATUS_CONFIRMED, STATUS_WAITLISTED, Store

from .helpers import make_settings


class ErasureFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.settings = make_settings()

    def tearDown(self):
        self._tmp.cleanup()

    def test_erase_cancels_future_bookings_and_archives(self):
        u = self.store.upsert_user("guest@example.com", "Guest", "h", "s")
        future_confirmed = self.store.add_registration(
            "c", "2099-01-01", u.user_id, hash_token(new_token())
        )
        future_waitlisted = self.store.add_registration(
            "c", "2099-01-08", u.user_id, hash_token(new_token()), status=STATUS_WAITLISTED
        )

        ok = erase_user_by_email(self.store, self.settings, "guest@example.com", today=date(2027, 1, 1))
        self.assertTrue(ok)

        archived_regs = {r["registration_id"]: r for r in self.store.read_registrations(scope="archived")}
        self.assertEqual(archived_regs[future_confirmed.registration_id]["status"], STATUS_CANCELED_BY_GUEST)
        self.assertEqual(archived_regs[future_waitlisted.registration_id]["status"], STATUS_CANCELED_BY_GUEST)

        archived_user = self.store.read_users(scope="archived")[0]
        self.assertTrue(is_erased_email(archived_user["email"]))
        self.assertEqual(archived_user["name"], "[erased]")
        self.assertIsNone(self.store.find_user_by_email("guest@example.com"))

    def test_erase_unknown_email_returns_false(self):
        self.assertFalse(
            erase_user_by_email(self.store, self.settings, "nobody@example.com", today=date(2027, 1, 1))
        )

    def test_different_users_get_different_erasure_hashes(self):
        self.store.upsert_user("one@example.com", "One", "h", "s")
        self.store.upsert_user("two@example.com", "Two", "h", "s")
        erase_user_by_email(self.store, self.settings, "one@example.com", today=date(2027, 1, 1))
        erase_user_by_email(self.store, self.settings, "two@example.com", today=date(2027, 1, 1))
        emails = {u["email"] for u in self.store.read_users(scope="archived")}
        self.assertEqual(len(emails), 2)


if __name__ == "__main__":
    unittest.main()
