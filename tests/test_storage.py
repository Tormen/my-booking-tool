import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.security import hash_secret, hash_token, new_token
from app.storage import (
    REG_FIELDS,
    STATUS_CONFIRMED,
    STATUS_PENDING_CONFIRMATION,
    STATUS_WAITLISTED,
    Store,
    _LockedCsv,
)


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
        self.assertEqual([r.registration_id for r in promoted], [waiting.registration_id])
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

    def test_party_is_promoted_together_not_split(self):
        """A waitlisted party of 2 (see add_party_registrations_checking_capacity)
        is only promoted once BOTH spots are free -- never split so one
        member confirms while the other stays waitlisted."""
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        party = self.store.add_party_registrations_checking_capacity(
            "c", "2026-01-01",
            [(u1.user_id, hash_token(new_token())), (u2.user_id, hash_token(new_token()))],
            capacity=0,  # no room at all -- whole party waitlists
        )
        self.assertTrue(all(r.status == STATUS_WAITLISTED for r in party))

        # only ONE spot frees up -- not enough for the party of 2
        promoted = self.store.promote_next_waitlisted("c", "2026-01-01", capacity=1)
        self.assertIsNone(promoted)
        still_waiting = [r for r in self.store.all_registrations() if r.status == STATUS_WAITLISTED]
        self.assertEqual(len(still_waiting), 2)

        # now BOTH spots are free -- the whole party promotes together
        promoted = self.store.promote_next_waitlisted("c", "2026-01-01", capacity=2)
        self.assertIsNotNone(promoted)
        self.assertEqual({r.user_id for r in promoted}, {u1.user_id, u2.user_id})
        self.assertTrue(all(r.status == STATUS_CONFIRMED for r in promoted))

    def test_front_of_line_party_blocks_a_smaller_party_behind_it(self):
        """First-come-first-served applies per-party: a smaller party that
        would fit is never promoted ahead of a bigger one that registered
        first but doesn't fit yet."""
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        u3 = self.store.upsert_user_for_booking("c@b.com", "Carol")
        self.store.add_party_registrations_checking_capacity(
            "c", "2026-01-01",
            [(u1.user_id, hash_token(new_token())), (u2.user_id, hash_token(new_token()))],
            capacity=0,
        )
        self.store.add_registration(
            "c", "2026-01-01", u3.user_id, hash_token(new_token()), status=STATUS_WAITLISTED
        )
        # 1 spot free: not enough for the front party of 2, and the solo
        # party of 1 behind it must NOT jump the queue
        promoted = self.store.promote_next_waitlisted("c", "2026-01-01", capacity=1)
        self.assertIsNone(promoted)


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


class MergeArchivedRegistrationsTest(StoreTestBase):
    """Store.merge_archived_registrations -- the explicit, admin-invoked
    history merge (`my-bt merge`), distinct from erase_user's own archival."""

    def test_moves_registrations_from_archive_to_live(self):
        old = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        reg = self.store.add_registration("c", "2026-01-01", old.user_id, hash_token(new_token()))
        self.store.erase_user(old.user_id, "erased:x")

        new = self.store.upsert_user_for_booking("guest@example.com", "Guest")  # re-books, fresh user_id
        self.assertNotEqual(old.user_id, new.user_id)

        moved = self.store.merge_archived_registrations([old.user_id], new.user_id)
        self.assertEqual(moved, 1)

        live_regs = self.store.registrations_for_user(new.user_id)
        self.assertEqual(len(live_regs), 1)
        self.assertEqual(live_regs[0].registration_id, reg.registration_id)  # id preserved
        self.assertEqual(self.store.read_registrations(scope="archived"), [])

    def test_user_id_is_rewritten_to_the_live_user(self):
        old = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("c", "2026-01-01", old.user_id, hash_token(new_token()))
        self.store.erase_user(old.user_id, "erased:x")
        new = self.store.upsert_user_for_booking("guest@example.com", "Guest")

        self.store.merge_archived_registrations([old.user_id], new.user_id)

        moved_row = self.store.read_registrations(scope="live")[0]
        self.assertEqual(moved_row["user_id"], new.user_id)

    def test_archived_user_row_is_untouched(self):
        old = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("c", "2026-01-01", old.user_id, hash_token(new_token()))
        self.store.erase_user(old.user_id, "erased:x")
        new = self.store.upsert_user_for_booking("guest@example.com", "Guest")

        self.store.merge_archived_registrations([old.user_id], new.user_id)

        archived_users = self.store.read_users(scope="archived")
        self.assertEqual(len(archived_users), 1)
        self.assertEqual(archived_users[0]["user_id"], old.user_id)
        self.assertEqual(archived_users[0]["email"], "erased:x")
        self.assertEqual(archived_users[0]["name"], "[erased]")

    def test_multiple_archived_user_ids_all_move(self):
        old1 = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("c", "2026-01-01", old1.user_id, hash_token(new_token()))
        self.store.erase_user(old1.user_id, "erased:x")

        old2 = self.store.upsert_user_for_booking("guest@example.com", "Guest")  # re-booked, erased again
        self.store.add_registration("c", "2026-02-01", old2.user_id, hash_token(new_token()))
        self.store.erase_user(old2.user_id, "erased:x")  # same hash both times (same email)

        new = self.store.upsert_user_for_booking("guest@example.com", "Guest")

        moved = self.store.merge_archived_registrations([old1.user_id, old2.user_id], new.user_id)
        self.assertEqual(moved, 2)
        live_dates = {r.occurrence_date for r in self.store.registrations_for_user(new.user_id)}
        self.assertEqual(live_dates, {"2026-01-01", "2026-02-01"})
        self.assertEqual(self.store.read_registrations(scope="archived"), [])

    def test_no_matching_archived_rows_returns_zero_and_touches_nothing(self):
        new = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        moved = self.store.merge_archived_registrations(["no-such-user-id"], new.user_id)
        self.assertEqual(moved, 0)
        self.assertEqual(self.store.registrations_for_user(new.user_id), [])

    def test_only_matched_user_ids_move_others_stay_archived(self):
        old1 = self.store.upsert_user_for_booking("one@example.com", "One")
        self.store.add_registration("c", "2026-01-01", old1.user_id, hash_token(new_token()))
        self.store.erase_user(old1.user_id, "erased:one")

        old2 = self.store.upsert_user_for_booking("two@example.com", "Two")
        self.store.add_registration("c", "2026-02-01", old2.user_id, hash_token(new_token()))
        self.store.erase_user(old2.user_id, "erased:two")

        new = self.store.upsert_user_for_booking("one@example.com", "One")
        moved = self.store.merge_archived_registrations([old1.user_id], new.user_id)
        self.assertEqual(moved, 1)

        remaining_archived = self.store.read_registrations(scope="archived")
        self.assertEqual(len(remaining_archived), 1)
        self.assertEqual(remaining_archived[0]["user_id"], old2.user_id)


class ImportHistoricalRegistrationTest(StoreTestBase):
    """Store.import_historical_registration -- backs the one-off SimplyMeet.me
    migration (app/migrate_simplymeet.py). Unlike add_registration(), the
    caller supplies registration_id/status/registered_at directly."""

    def test_writes_row_with_caller_supplied_fields(self):
        user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        created = self.store.import_historical_registration(
            registration_id="simplymeet-123",
            course_shortname="c",
            occurrence_date="2026-01-01",
            user_id=user.user_id,
            status=STATUS_CONFIRMED,
            registered_at="2026-01-01T00:00:00",
        )
        self.assertTrue(created)
        reg = self.store.find_by_id("simplymeet-123")
        self.assertIsNotNone(reg)
        self.assertEqual(reg.course_shortname, "c")
        self.assertEqual(reg.occurrence_date, "2026-01-01")
        self.assertEqual(reg.user_id, user.user_id)
        self.assertEqual(reg.status, STATUS_CONFIRMED)
        self.assertEqual(reg.registered_at, "2026-01-01T00:00:00")
        self.assertEqual(reg.guest_cancel_token_hash, "")

    def test_canceled_fields_round_trip(self):
        user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.import_historical_registration(
            registration_id="simplymeet-456",
            course_shortname="c",
            occurrence_date="2026-01-01",
            user_id=user.user_id,
            status="canceled_by_guest",
            registered_at="2026-01-01T00:00:00",
            canceled_at="2025-12-30T09:00:00",
            canceled_by="guest",
        )
        reg = self.store.find_by_id("simplymeet-456")
        self.assertEqual(reg.status, "canceled_by_guest")
        self.assertEqual(reg.canceled_at, "2025-12-30T09:00:00")
        self.assertEqual(reg.canceled_by, "guest")

    def test_reimport_of_same_id_is_a_noop(self):
        user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        first = self.store.import_historical_registration(
            registration_id="simplymeet-789",
            course_shortname="c",
            occurrence_date="2026-01-01",
            user_id=user.user_id,
            status=STATUS_CONFIRMED,
            registered_at="2026-01-01T00:00:00",
        )
        second = self.store.import_historical_registration(
            registration_id="simplymeet-789",
            course_shortname="different-course",
            occurrence_date="2027-01-01",
            user_id=user.user_id,
            status=STATUS_CONFIRMED,
            registered_at="2027-01-01T00:00:00",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        # only the original row exists, untouched by the second call's args
        matches = [r for r in self.store.all_registrations() if r.registration_id == "simplymeet-789"]
        self.assertEqual(len(matches), 1)

    def test_party_id_and_invited_by_user_id_round_trip(self):
        """Backs the migration script's import of SimplyMeet.me's "Other
        participants" as linked guest registrations (app/migrate_simplymeet.py)."""
        leader = self.store.upsert_user_for_booking("leader@example.com", "Leader")
        guest = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.import_historical_registration(
            registration_id="simplymeet-1-leader",
            course_shortname="c", occurrence_date="2026-01-01",
            user_id=leader.user_id, status=STATUS_CONFIRMED,
            registered_at="2026-01-01T00:00:00", party_id="party-1",
        )
        self.store.import_historical_registration(
            registration_id="simplymeet-1-guest-0",
            course_shortname="c", occurrence_date="2026-01-01",
            user_id=guest.user_id, status=STATUS_CONFIRMED,
            registered_at="2026-01-01T00:00:00", party_id="party-1",
            invited_by_user_id=leader.user_id,
        )
        leader_reg = self.store.find_by_id("simplymeet-1-leader")
        guest_reg = self.store.find_by_id("simplymeet-1-guest-0")
        self.assertEqual(leader_reg.party_id, "party-1")
        self.assertEqual(leader_reg.invited_by_user_id, "")
        self.assertEqual(guest_reg.party_id, "party-1")
        self.assertEqual(guest_reg.invited_by_user_id, leader.user_id)

    def test_party_id_defaults_blank_for_a_plain_historical_row(self):
        user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.import_historical_registration(
            registration_id="simplymeet-solo",
            course_shortname="c", occurrence_date="2026-01-01",
            user_id=user.user_id, status=STATUS_CONFIRMED,
            registered_at="2026-01-01T00:00:00",
        )
        reg = self.store.find_by_id("simplymeet-solo")
        self.assertEqual(reg.party_id, "")
        self.assertEqual(reg.invited_by_user_id, "")


class CsvInjectionTest(StoreTestBase):
    def test_dangerous_name_is_escaped_on_disk(self):
        self.store.upsert_user_for_booking("a@b.com", "=cmd|'/c calc'!A1")
        raw = Path(self.store.users_path).read_text()
        self.assertIn("'=cmd", raw)


class LockedCsvReadonlyModeTest(unittest.TestCase):
    """_LockedCsv(readonly=True) must open "r", never "r+" -- "r+" fails
    outright on a genuinely read-only file/mount (e.g. systemd's
    ReadOnlyPaths=, which the my-booking-watchdog unit sets on
    /var/lib/my-booking), even though nothing was ever going to be
    written."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registrations.csv"
        with _LockedCsv(self.path, REG_FIELDS) as (rows, write):
            write([])  # create the file with just a header

    def tearDown(self):
        # restore write perms so TemporaryDirectory cleanup can remove it
        os.chmod(self.path, 0o600)
        os.chmod(self.path.parent, 0o700)
        self._tmp.cleanup()

    def test_readonly_mode_opens_file_in_read_only_mode(self):
        seen_modes = []
        real_open = open

        def spy_open(path, mode="r", *args, **kwargs):
            if str(path) == str(self.path):
                seen_modes.append(mode)
            return real_open(path, mode, *args, **kwargs)

        import builtins
        orig = builtins.open
        builtins.open = spy_open
        try:
            with _LockedCsv(self.path, REG_FIELDS, readonly=True) as (rows, _write):
                pass
        finally:
            builtins.open = orig

        self.assertEqual(seen_modes, ["r"])

    def test_readonly_mode_survives_a_chmod_read_only_file(self):
        os.chmod(self.path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o444
        try:
            with _LockedCsv(self.path, REG_FIELDS, readonly=True) as (rows, _write):
                self.assertEqual(rows, [])
        except OSError:
            self.fail("_LockedCsv(readonly=True) must not require write access")

    def test_readonly_mode_survives_a_chmod_read_only_directory(self):
        # Mirrors systemd's ReadOnlyPaths=/var/lib/my-booking: the whole
        # mount/directory is read-only, not just the file.
        os.chmod(self.path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(self.path.parent, stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
        try:
            with _LockedCsv(self.path, REG_FIELDS, readonly=True) as (rows, _write):
                self.assertEqual(rows, [])
        except OSError:
            self.fail("_LockedCsv(readonly=True) must not require write access to the directory")

    def test_readonly_mode_raises_if_write_is_called(self):
        with _LockedCsv(self.path, REG_FIELDS, readonly=True) as (rows, write):
            with self.assertRaises(RuntimeError):
                write(rows)


class StoreReadOnlyMethodsUnderReadOnlyMountTest(StoreTestBase):
    """End-to-end proof for the actual bug report: every Store method that
    never writes must keep working when its data files (and the containing
    directory, mirroring systemd ReadOnlyPaths=) are chmod'd read-only --
    this is exactly the my-booking-watchdog scenario."""

    def setUp(self):
        super().setUp()
        # Populate some data while the directory is still writable.
        self.user = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.set_confirm_token(self.user.user_id, "tokhash", "2026-07-05T00:00:00+00:00")
        self.guest_token = new_token()
        self.reg = self.store.add_registration(
            "c", "2026-07-08", self.user.user_id, hash_token(self.guest_token)
        )

    def _make_read_only(self):
        for p in (self.store.users_path, self.store.registrations_path):
            os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(self.store.data_dir, stat.S_IRUSR | stat.S_IXUSR)

    def tearDown(self):
        os.chmod(self.store.data_dir, 0o700)
        os.chmod(self.store.users_path, 0o600)
        os.chmod(self.store.registrations_path, 0o600)
        super().tearDown()

    def test_all_pure_read_methods_work_under_read_only_mount(self):
        self._make_read_only()
        # users.csv-backed reads
        self.assertEqual(self.store.find_user_by_email("guest@example.com").user_id, self.user.user_id)
        self.assertEqual(self.store.find_user_by_id(self.user.user_id).user_id, self.user.user_id)
        self.assertEqual(
            self.store.find_user_by_confirm_token_hash("tokhash").user_id, self.user.user_id
        )
        # registrations.csv-backed reads
        self.assertEqual(self.store.count_confirmed("c", "2026-07-08"), 1)
        self.assertEqual(self.store.times_registered(self.user.user_id), 1)
        self.assertEqual(
            len(self.store.registrations_for_occurrence("c", "2026-07-08")), 1
        )
        self.assertEqual(len(self.store.registrations_for_user(self.user.user_id)), 1)
        self.assertEqual(
            self.store.find_by_id(self.reg.registration_id).registration_id,
            self.reg.registration_id,
        )
        self.assertEqual(
            self.store.find_by_guest_token_hash(hash_token(self.guest_token)).registration_id,
            self.reg.registration_id,
        )
        self.assertEqual(len(self.store.all_registrations()), 1)


if __name__ == "__main__":
    unittest.main()
