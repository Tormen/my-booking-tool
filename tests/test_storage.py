import tempfile
import unittest
from pathlib import Path

from app.security import hash_secret, hash_token, new_token
from app.storage import STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class UserTest(StoreTestBase):
    def test_upsert_creates_then_updates(self):
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("A@B.com", "Alice B.")
        self.assertEqual(u1.user_id, u2.user_id)
        self.assertEqual(self.store.find_user_by_email("a@b.com").name, "Alice B.")

    def test_find_missing_returns_none(self):
        self.assertIsNone(self.store.find_user_by_email("nobody@x.com"))

    def test_new_user_has_no_password_set(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.assertEqual(u.password_hash, "")
        self.assertEqual(u.password_salt, "")

    def test_upsert_for_booking_never_touches_an_existing_password(self):
        # This is the actual account-hijack fix: nothing reachable from a
        # booking can change another email's password, confirmed or not.
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        h, s = hash_secret("hunter2")
        self.store.set_password(u.user_id, h, s)
        self.store.upsert_user_for_booking("a@b.com", "Someone else typed my name")
        reloaded = self.store.find_user_by_email("a@b.com")
        self.assertEqual(reloaded.password_hash, h)
        self.assertEqual(reloaded.password_salt, s)
        self.assertEqual(reloaded.name, "Someone else typed my name")  # name IS updated, deliberately

    def test_set_confirm_token_then_find_by_it(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_confirm_token(u.user_id, "deadbeef", "2026-07-05T00:00:00+00:00")
        found = self.store.find_user_by_confirm_token_hash("deadbeef")
        self.assertEqual(found.user_id, u.user_id)

    def test_find_by_confirm_token_blank_hash_never_matches(self):
        self.store.upsert_user_for_booking("a@b.com", "Alice")
        # A brand-new user's confirm_token_hash is "" -- a lookup with a
        # blank hash must never accidentally match every unconfirmed user.
        self.assertIsNone(self.store.find_user_by_confirm_token_hash(""))

    def test_set_password_clears_the_confirm_token(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_confirm_token(u.user_id, "deadbeef", "2026-07-05T00:00:00+00:00")
        h, s = hash_secret("hunter2")
        self.store.set_password(u.user_id, h, s)
        self.assertIsNone(self.store.find_user_by_confirm_token_hash("deadbeef"))
        reloaded = self.store.find_user_by_email("a@b.com")
        self.assertEqual(reloaded.password_hash, h)


class RegistrationTest(StoreTestBase):
    def test_add_and_count(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.add_registration("yoga-class-1", "2026-07-08", u.user_id, hash_token(new_token()))
        self.assertEqual(self.store.count_confirmed("yoga-class-1", "2026-07-08"), 1)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", "2026-07-15"), 0)

    def test_cancel_is_idempotent(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("yoga-class-1", "2026-07-08", u.user_id, hash_token(new_token()))
        self.assertTrue(self.store.cancel(reg.registration_id, canceled_by="guest"))
        self.assertFalse(self.store.cancel(reg.registration_id, canceled_by="guest"))
        self.assertEqual(self.store.count_confirmed("yoga-class-1", "2026-07-08"), 0)

    def test_times_registered_counts_all_statuses(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        r1 = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))
        self.store.add_registration("c", "2026-01-08", u.user_id, hash_token(new_token()))
        self.store.cancel(r1.registration_id, canceled_by="guest")
        self.assertEqual(self.store.times_registered(u.user_id), 2)

    def test_find_by_guest_token_hash_matches_confirmed_and_waitlisted(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        token = new_token()
        reg = self.store.add_registration(
            "c", "2026-01-01", u.user_id, hash_token(token), status=STATUS_WAITLISTED
        )
        found = self.store.find_by_guest_token_hash(hash_token(token))
        self.assertEqual(found.registration_id, reg.registration_id)


class AddRegistrationCheckingCapacityTest(StoreTestBase):
    """Regression coverage for the capacity race fix: count-then-insert must
    happen as one locked operation, not two separate ones."""

    def test_fills_up_to_capacity_then_waitlists(self):
        users = [self.store.upsert_user_for_booking(f"u{i}@x.com", f"U{i}") for i in range(3)]
        statuses = [
            self.store.add_registration_checking_capacity(
                "c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=2
            ).status
            for u in users
        ]
        self.assertEqual(statuses, [STATUS_CONFIRMED, STATUS_CONFIRMED, STATUS_WAITLISTED])
        self.assertEqual(self.store.count_confirmed("c", "2026-01-01"), 2)

    def test_never_exceeds_capacity_even_under_simulated_races(self):
        # Not true concurrency (the CSV lock serializes real concurrent
        # processes anyway), but confirms the decision is made from a single
        # consistent read of the row set rather than a separate earlier read.
        users = [self.store.upsert_user_for_booking(f"r{i}@x.com", f"R{i}") for i in range(10)]
        for u in users:
            self.store.add_registration_checking_capacity(
                "c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=1
            )
        self.assertEqual(self.store.count_confirmed("c", "2026-01-01"), 1)
        all_regs = [r for r in self.store.all_registrations() if r.occurrence_date == "2026-01-01"]
        self.assertEqual(len(all_regs), 10)
        waitlisted = sum(1 for r in all_regs if r.status == STATUS_WAITLISTED)
        self.assertEqual(waitlisted, 9)


class ConfirmPendingRegistrationTest(StoreTestBase):
    """STATUS_PENDING_CONFIRMATION rows are excluded from count_confirmed/
    add_registration_checking_capacity/promote_next_waitlisted simply by
    not matching STATUS_CONFIRMED anywhere -- these tests cover
    confirm_pending_registration's own promotion logic specifically."""

    def test_promotes_to_confirmed_when_room(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration(
            "c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION
        )
        updated = self.store.confirm_pending_registration(reg.registration_id, capacity=2, cancel_token_hash="newhash")
        self.assertEqual(updated.status, STATUS_CONFIRMED)
        self.assertEqual(updated.guest_cancel_token_hash, "newhash")
        self.assertEqual(self.store.count_confirmed("c", "2026-01-01"), 1)

    def test_promotes_to_waitlisted_when_full(self):
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        self.store.add_registration("c", "2026-01-01", u1.user_id, hash_token(new_token()))  # fills capacity=1
        reg = self.store.add_registration(
            "c", "2026-01-01", u2.user_id, "", status=STATUS_PENDING_CONFIRMATION
        )
        updated = self.store.confirm_pending_registration(reg.registration_id, capacity=1, cancel_token_hash="newhash")
        self.assertEqual(updated.status, STATUS_WAITLISTED)

    def test_pending_never_counts_toward_capacity_before_confirming(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.add_registration("c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.assertEqual(self.store.count_confirmed("c", "2026-01-01"), 0)

    def test_returns_none_if_no_longer_pending(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))  # already confirmed
        self.assertIsNone(
            self.store.confirm_pending_registration(reg.registration_id, capacity=5, cancel_token_hash="x")
        )


class WaitlistTest(StoreTestBase):
    def test_promotes_earliest_waitlisted_when_capacity_frees_up(self):
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        confirmed = self.store.add_registration("c", "2026-01-01", u1.user_id, hash_token(new_token()))
        waiting = self.store.add_registration(
            "c", "2026-01-01", u2.user_id, hash_token(new_token()), status=STATUS_WAITLISTED
        )
        self.store.cancel(confirmed.registration_id, canceled_by="guest")
        promoted = self.store.promote_next_waitlisted("c", "2026-01-01", capacity=1)
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.registration_id, waiting.registration_id)
        self.assertEqual(self.store.count_confirmed("c", "2026-01-01"), 1)

    def test_does_not_promote_if_no_room(self):
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        self.store.add_registration("c", "2026-01-01", u1.user_id, hash_token(new_token()))
        self.store.add_registration(
            "c", "2026-01-01", u2.user_id, hash_token(new_token()), status=STATUS_WAITLISTED
        )
        # capacity=1 and 1 confirmed already -- no room, even though someone waits
        promoted = self.store.promote_next_waitlisted("c", "2026-01-01", capacity=1)
        self.assertIsNone(promoted)

    def test_no_waitlist_returns_none(self):
        self.assertIsNone(self.store.promote_next_waitlisted("c", "2026-01-01", capacity=5))


class ErasureTest(StoreTestBase):
    def test_erase_moves_user_and_registrations_to_archive(self):
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest Name")
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))

        ok = self.store.erase_user(u.user_id, "erased:deadbeef")
        self.assertTrue(ok)

        self.assertIsNone(self.store.find_user_by_email("guest@example.com"))
        self.assertEqual(self.store.registrations_for_user(u.user_id), [])

        archived_users = self.store.read_users(scope="archived")
        self.assertEqual(len(archived_users), 1)
        self.assertEqual(archived_users[0]["email"], "erased:deadbeef")
        self.assertEqual(archived_users[0]["name"], "[erased]")

        archived_regs = self.store.read_registrations(scope="archived")
        self.assertEqual([r["registration_id"] for r in archived_regs], [reg.registration_id])

    def test_erase_unknown_user_returns_false(self):
        self.assertFalse(self.store.erase_user("no-such-id", "erased:x"))

    def test_read_registrations_scope_filters(self):
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))
        self.store.erase_user(u.user_id, "erased:x")
        self.assertEqual(len(self.store.read_registrations(scope="live")), 0)
        self.assertEqual(len(self.store.read_registrations(scope="archived")), 1)
        self.assertEqual(len(self.store.read_registrations(scope="all")), 1)


class CsvInjectionTest(StoreTestBase):
    def test_dangerous_name_is_escaped_on_disk(self):
        self.store.upsert_user_for_booking("a@b.com", "=cmd|'/c calc'!A1")
        raw = Path(self.store.users_path).read_text()
        self.assertIn("'=cmd", raw)


if __name__ == "__main__":
    unittest.main()
