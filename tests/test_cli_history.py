import tempfile
import unittest

from app.cli_history import run_merge
from app.erasure import find_archived_user_ids_for_email
from app.security import hash_token, new_token
from app.storage import Store

from .helpers import make_settings


class CliHistoryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.settings = make_settings()

    def tearDown(self):
        self._tmp.cleanup()

    def _erase(self, email: str, name: str = "Guest"):
        """Helper: creates a user with a booking, then erases them exactly
        the way app/erasure.erase_user_by_email would (hashing with this
        test's own settings.erasure_pepper), and returns the pre-erasure
        user_id."""
        from app.security import hash_email_for_erasure

        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration("c", "2026-01-01", user.user_id, hash_token(new_token()))
        hashed = hash_email_for_erasure(user.email, self.settings.erasure_pepper)
        self.store.erase_user(user.user_id, hashed)
        return user.user_id, reg.registration_id


class FindArchivedUserIdsForEmailTest(CliHistoryTestBase):
    def test_finds_archived_user_after_rebooking(self):
        old_id, _ = self._erase("guest@example.com")
        found = find_archived_user_ids_for_email(self.store, self.settings, "guest@example.com")
        self.assertEqual(found, [old_id])

    def test_no_match_when_never_erased(self):
        self.store.upsert_user_for_booking("guest@example.com", "Guest")
        found = find_archived_user_ids_for_email(self.store, self.settings, "guest@example.com")
        self.assertEqual(found, [])

    def test_no_match_for_unrelated_email(self):
        self._erase("guest@example.com")
        found = find_archived_user_ids_for_email(self.store, self.settings, "nobody@example.com")
        self.assertEqual(found, [])

    def test_finds_multiple_archived_identities_same_email(self):
        old1, _ = self._erase("guest@example.com")
        old2, _ = self._erase("guest@example.com")
        found = find_archived_user_ids_for_email(self.store, self.settings, "guest@example.com")
        self.assertEqual(set(found), {old1, old2})


class RunMergeTest(CliHistoryTestBase):
    def test_merges_and_reports_moved_registrations(self):
        old_id, reg_id = self._erase("guest@example.com")
        new_user = self.store.upsert_user_for_booking("guest@example.com", "Guest")

        result = run_merge(self.store, [old_id], new_user.user_id)
        self.assertEqual(result.moved_count, 1)
        self.assertEqual(result.moved_registrations[0]["registration_id"], reg_id)

        live_regs = self.store.registrations_for_user(new_user.user_id)
        self.assertEqual([r.registration_id for r in live_regs], [reg_id])
        self.assertEqual(self.store.read_registrations(scope="archived"), [])

    def test_nothing_to_merge_reports_zero(self):
        new_user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        result = run_merge(self.store, [], new_user.user_id)
        self.assertEqual(result.moved_count, 0)
        self.assertEqual(result.moved_registrations, [])


if __name__ == "__main__":
    unittest.main()
