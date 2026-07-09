import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.security import hash_secret, hash_token, new_token
from app.storage import (
    REG_FIELDS,
    STATUS_CANCELED_BY_GUEST,
    STATUS_CANCELED_BY_HOST,
    STATUS_CONFIRMED,
    STATUS_PENDING_CONFIRMATION,
    STATUS_WAITLISTED,
    Store,
    _LockedCsv,
    _SERVICE_GROUP,
    _fsync_dir,
    _secure_data_path,
    format_display_timestamp,
    status_label,
)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class GitAutoCommitTest(StoreTestBase):
    """2026-07-07, the operator: "after any change to any of the CSV files: CUD
    ... please directly do a git commit ... Commit message should state
    what changed without revealing personal data ... as a safety net in
    case of ANY bugs." Every Store write goes through _LockedCsv, which is
    where this is wired in (storage.py::_git_commit_data_file) -- so any
    one mutating call is enough to exercise it.

    Deliberately does NOT auto-`git init` -- same "opt in via `my-bt setup
    -i` only" design as the existing hourly app/git_snapshot.py -- so every
    test here except test_no_repo_yet_is_a_silent_noop starts by git-init'ing
    the tmp data dir itself, the same one-time step setup -i performs on a
    real deployment."""

    def _init_repo(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True, capture_output=True)

    def _log(self) -> list[str]:
        out = subprocess.run(
            ["git", "log", "--format=%s"], cwd=self._tmp.name, capture_output=True, text=True, check=True,
        )
        return out.stdout.splitlines()

    def test_no_repo_yet_is_a_silent_noop(self):
        # No `my-bt setup -i` run yet for this data dir -- a write must
        # NOT silently turn it into a git repo (matches
        # app/git_snapshot.py's own "not_a_repo" convention, which this is
        # a per-write companion to).
        self.assertFalse((Path(self._tmp.name) / ".git").exists())
        self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.assertFalse((Path(self._tmp.name) / ".git").exists())

    def test_write_creates_a_commit_with_a_descriptive_message(self):
        self._init_repo()
        self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.assertEqual(self._log(), ["create user"])

    def test_commit_message_never_contains_the_email_or_name(self):
        # The whole point: an admin/attacker with read access to `git log`
        # must not be able to harvest guest emails/names from commit
        # messages -- only from the (equally access-controlled) CSV
        # content itself, same exposure as today.
        self._init_repo()
        self.store.upsert_user_for_booking("secret-guest@example.org", "Secret Name")
        log_text = "\n".join(self._log())
        self.assertNotIn("secret-guest@example.org", log_text)
        self.assertNotIn("Secret Name", log_text)

    def test_each_mutation_adds_its_own_commit(self):
        self._init_repo()
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.store.set_name(user.user_id, "Renamed")
        self.assertEqual(self._log(), ["update user name", "create user"])

    def test_a_no_op_write_of_identical_content_adds_no_commit(self):
        self._init_repo()
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        commits_after_create = len(self._log())
        # Re-touching the exact same name is byte-identical on disk --
        # nothing staged, so no new commit should appear.
        self.store.set_name(user.user_id, "Guest")
        self.assertEqual(len(self._log()), commits_after_create)

    def test_registrations_csv_is_committed_separately_from_users_csv(self):
        self._init_repo()
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.store.add_registration("yoga-class-1", "2026-08-01", user.user_id, "tok-hash")
        out = subprocess.run(
            ["git", "show", "--stat", "--format=", "HEAD"],
            cwd=self._tmp.name, capture_output=True, text=True, check=True,
        )
        self.assertIn("registrations.csv", out.stdout)
        self.assertNotIn("users.csv", out.stdout)

    def test_archived_csvs_reuse_the_same_top_level_repo(self):
        # archived/*.csv (see Store.erase_user) live one directory below
        # users.csv/registrations.csv -- must NOT end up as a separate,
        # nested git repo; a second .git should never appear under
        # archived/.
        self._init_repo()
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        from app.security import hash_email_for_erasure
        hashed = hash_email_for_erasure("guest@example.org", b"\x00" * 32)
        self.store.erase_user(user.user_id, hashed)
        self.assertFalse((Path(self._tmp.name) / "archived" / ".git").exists())
        # erase_user() is now several separate commits (2026-07-15:
        # archive-writes happen BEFORE the live-row removals, for
        # crash-safety -- see erase_user's own docstring), so the archived
        # write is no longer necessarily HEAD -- check the whole log, not
        # just the latest commit.
        out = subprocess.run(
            ["git", "log", "--stat", "--format="],
            cwd=self._tmp.name, capture_output=True, text=True, check=True,
        )
        self.assertIn("archived", out.stdout)


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


class PendingEmailTest(StoreTestBase):
    """2026-07-10: /my/settings' email-change flow -- see app/webapp.py's
    "-- /my/settings --" section for the request/confirm handlers that
    call these."""

    def test_set_name_updates_it(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_name(u.user_id, "Alice Renamed")
        self.assertEqual(self.store.find_user_by_email("a@b.com").name, "Alice Renamed")

    def test_set_pending_email_then_find_by_token(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(u.user_id, "new@b.com", "deadbeef", "2026-07-10T00:00:00+00:00")
        found = self.store.find_user_by_pending_email_token_hash("deadbeef")
        self.assertEqual(found.user_id, u.user_id)
        self.assertEqual(found.pending_email, "new@b.com")
        self.assertEqual(found.email, "a@b.com")  # not swapped yet

    def test_second_request_overwrites_the_first_and_supersedes_its_token(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(u.user_id, "first@b.com", "hash1", "2026-07-10T00:00:00+00:00")
        self.store.set_pending_email(u.user_id, "second@b.com", "hash2", "2026-07-10T01:00:00+00:00")
        self.assertIsNone(self.store.find_user_by_pending_email_token_hash("hash1"))
        current = self.store.find_user_by_pending_email_token_hash("hash2")
        self.assertEqual(current.pending_email, "second@b.com")
        superseded = self.store.find_user_by_prev_pending_email_token_hash("hash1")
        self.assertEqual(superseded.user_id, u.user_id)

    def test_clear_pending_email_does_not_mark_it_as_superseded(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(u.user_id, "new@b.com", "hash1", "2026-07-10T00:00:00+00:00")
        self.store.clear_pending_email(u.user_id)
        self.assertIsNone(self.store.find_user_by_pending_email_token_hash("hash1"))
        # An aborted change is not "superseded by a newer one" -- nothing
        # newer was ever sent, so the friendlier message would be wrong.
        self.assertIsNone(self.store.find_user_by_prev_pending_email_token_hash("hash1"))
        reloaded = self.store.find_user_by_email("a@b.com")
        self.assertEqual(reloaded.pending_email, "")

    def test_apply_pending_email_swaps_email_and_clears_pending_fields(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(u.user_id, "new@b.com", "hash1", "2026-07-10T00:00:00+00:00")
        updated = self.store.apply_pending_email(u.user_id)
        self.assertEqual(updated.email, "new@b.com")
        self.assertEqual(updated.pending_email, "")
        self.assertEqual(updated.pending_email_token_hash, "")
        self.assertIsNone(self.store.find_user_by_email("a@b.com"))
        self.assertIsNotNone(self.store.find_user_by_email("new@b.com"))

    def test_apply_pending_email_is_a_no_op_without_one_outstanding(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.assertIsNone(self.store.apply_pending_email(u.user_id))
        self.assertEqual(self.store.find_user_by_email("a@b.com").email, "a@b.com")

    def test_find_by_pending_email_token_blank_hash_never_matches(self):
        self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.assertIsNone(self.store.find_user_by_pending_email_token_hash(""))

    def test_cancel_token_is_separate_from_the_confirm_token(self):
        # 2026-07-11, the operator: "Please provide a link without login" -- the
        # cancel token must be its OWN secret, not a reuse of the confirm
        # token, so possessing one can never be used to perform the other
        # action (confirm vs. abort).
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(
            u.user_id, "new@b.com", "confirm-hash", "2026-07-11T00:00:00+00:00",
            cancel_token_hash="cancel-hash",
        )
        by_confirm = self.store.find_user_by_pending_email_token_hash("confirm-hash")
        by_cancel = self.store.find_user_by_pending_email_cancel_token_hash("cancel-hash")
        self.assertEqual(by_confirm.user_id, u.user_id)
        self.assertEqual(by_cancel.user_id, u.user_id)
        # Neither token satisfies the OTHER lookup.
        self.assertIsNone(self.store.find_user_by_pending_email_cancel_token_hash("confirm-hash"))
        self.assertIsNone(self.store.find_user_by_pending_email_token_hash("cancel-hash"))

    def test_omitting_cancel_token_hash_leaves_any_existing_one_untouched(self):
        # Blank cancel_token_hash (the default) is a no-op on that field --
        # existing callers/tests that don't care about the cancel link
        # shouldn't have their cancel token silently wiped.
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(
            u.user_id, "new@b.com", "confirm-hash-1", "2026-07-11T00:00:00+00:00",
            cancel_token_hash="cancel-hash-1",
        )
        self.store.set_pending_email(u.user_id, "second@b.com", "confirm-hash-2", "2026-07-11T01:00:00+00:00")
        still_there = self.store.find_user_by_pending_email_cancel_token_hash("cancel-hash-1")
        self.assertEqual(still_there.pending_email, "second@b.com")

    def test_clear_pending_email_also_clears_the_cancel_token(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(
            u.user_id, "new@b.com", "confirm-hash", "2026-07-11T00:00:00+00:00",
            cancel_token_hash="cancel-hash",
        )
        self.store.clear_pending_email(u.user_id)
        self.assertIsNone(self.store.find_user_by_pending_email_cancel_token_hash("cancel-hash"))

    def test_apply_pending_email_also_clears_the_cancel_token(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.store.set_pending_email(
            u.user_id, "new@b.com", "confirm-hash", "2026-07-11T00:00:00+00:00",
            cancel_token_hash="cancel-hash",
        )
        self.store.apply_pending_email(u.user_id)
        self.assertIsNone(self.store.find_user_by_pending_email_cancel_token_hash("cancel-hash"))

    def test_find_by_pending_email_cancel_token_blank_hash_never_matches(self):
        self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.assertIsNone(self.store.find_user_by_pending_email_cancel_token_hash(""))


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

    def test_cancel_works_on_pending_confirmation_row(self):
        # 2026-07-13, the operator: a guest who registered but hasn't yet clicked
        # their account-confirmation email link (STATUS_PENDING_CONFIRMATION)
        # previously couldn't be canceled by ANY path -- Store.cancel() only
        # accepted confirmed/waitlisted. Now cancelable too, same as any
        # other active status.
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration(
            "yoga-class-1", "2026-07-08", u.user_id, "", status=STATUS_PENDING_CONFIRMATION,
        )
        self.assertTrue(self.store.cancel(reg.registration_id, canceled_by="host"))
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

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


class CancelReinstateTokenTest(StoreTestBase):
    """2026-07-10: cancel()'s own `reinstate_token_hash` param -- the web
    app's no-login /reinstate/<token> magic-link page (see
    app/webapp.py::guest_reinstate) needs a token whose PLAINTEXT is known
    at cancellation time, which the original booking's own cancel token
    never is (only its hash was ever persisted)."""

    def test_reinstate_token_hash_overwrites_guest_cancel_token_hash(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        original_token = new_token()
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(original_token))
        new_reinstate_token = new_token()
        self.store.cancel(
            reg.registration_id, canceled_by="guest", reinstate_token_hash=hash_token(new_reinstate_token),
        )
        # The OLD (original booking) token no longer matches anything --
        # this row is canceled now, and find_by_guest_token_hash only ever
        # matches CONFIRMED/WAITLISTED anyway.
        self.assertIsNone(self.store.find_by_guest_token_hash(hash_token(original_token)))
        # The NEW token matches this now-canceled row via the reinstate lookup.
        found = self.store.find_canceled_by_guest_token_hash(hash_token(new_reinstate_token))
        self.assertEqual(found.registration_id, reg.registration_id)

    def test_omitting_reinstate_token_hash_leaves_the_existing_hash_untouched(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        token = new_token()
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(token))
        self.store.cancel(reg.registration_id, canceled_by="guest")  # no reinstate_token_hash given
        found = self.store.find_canceled_by_guest_token_hash(hash_token(token))
        self.assertEqual(found.registration_id, reg.registration_id)

    def test_reinstate_token_hash_is_not_applied_when_cancel_is_a_no_op(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        stale_token = new_token()
        # Second cancel is a no-op (already canceled) -- must not silently
        # rotate the token out from under a link already emailed once.
        changed = self.store.cancel(
            reg.registration_id, canceled_by="guest", reinstate_token_hash=hash_token(stale_token),
        )
        self.assertFalse(changed)
        self.assertIsNone(self.store.find_canceled_by_guest_token_hash(hash_token(stale_token)))

    def test_find_canceled_by_guest_token_hash_does_not_match_an_active_registration(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        token = new_token()
        self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(token))
        self.assertIsNone(self.store.find_canceled_by_guest_token_hash(hash_token(token)))

    def test_find_canceled_by_guest_token_hash_blank_hash_never_matches(self):
        self.store.upsert_user_for_booking("a@b.com", "Alice")
        self.assertIsNone(self.store.find_canceled_by_guest_token_hash(""))


class ReinstateTest(StoreTestBase):
    """2026-07-10: "undo the cancel" -- see Store.reinstate()'s own
    docstring and app/webapp.py's my_reinstate()/admin_reinstate()."""

    def test_reinstates_a_canceled_registration_back_to_confirmed(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-08-01", u.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        updated = self.store.reinstate(reg.registration_id, capacity=2)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, STATUS_CONFIRMED)
        self.assertEqual(updated.canceled_at, "")
        self.assertEqual(updated.canceled_by, "")
        self.assertEqual(self.store.count_confirmed("c", "2026-08-01"), 1)

    def test_reinstates_to_waitlisted_when_capacity_is_now_full(self):
        u1 = self.store.upsert_user_for_booking("a@b.com", "Alice")
        u2 = self.store.upsert_user_for_booking("b@b.com", "Bob")
        reg = self.store.add_registration("c", "2026-08-01", u1.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        # Someone else took the freed-up (now the only) spot in the meantime.
        self.store.add_registration_checking_capacity("c", "2026-08-01", u2.user_id, hash_token(new_token()), capacity=1)
        updated = self.store.reinstate(reg.registration_id, capacity=1)
        self.assertEqual(updated.status, STATUS_WAITLISTED)

    def test_reinstating_something_not_canceled_is_a_no_op(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-08-01", u.user_id, hash_token(new_token()))
        self.assertIsNone(self.store.reinstate(reg.registration_id, capacity=2))

    def test_reinstating_twice_is_a_no_op_the_second_time(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-08-01", u.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        self.assertIsNotNone(self.store.reinstate(reg.registration_id, capacity=2))
        self.assertIsNone(self.store.reinstate(reg.registration_id, capacity=2))

    def test_reinstate_preserves_the_original_guest_cancel_token(self):
        # So the guest's original booking-confirmation email's cancel link
        # still works after a reinstate -- no need to mint/email a new one.
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        token = new_token()
        reg = self.store.add_registration("c", "2026-08-01", u.user_id, hash_token(token))
        self.store.cancel(reg.registration_id, canceled_by="host")
        self.store.reinstate(reg.registration_id, capacity=2)
        found = self.store.find_by_guest_token_hash(hash_token(token))
        self.assertEqual(found.registration_id, reg.registration_id)

    def test_reinstates_a_host_canceled_registration_too(self):
        u = self.store.upsert_user_for_booking("a@b.com", "Alice")
        reg = self.store.add_registration("c", "2026-08-01", u.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="host")
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        updated = self.store.reinstate(reg.registration_id, capacity=2)
        self.assertEqual(updated.status, STATUS_CONFIRMED)


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


class HasActiveRegistrationTest(StoreTestBase):
    """2026-07-10, the operator (screenshot of /my): "double booking possible?" --
    Store.has_active_registration() is the pre-check app.webapp.App.book()
    now runs before add_registration_checking_capacity/
    add_party_registrations_checking_capacity, closing the gap where
    neither of those ever checked whether the REQUESTING user already held
    a spot, only aggregate capacity."""

    def test_false_when_user_has_no_registration_at_all(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.assertFalse(self.store.has_active_registration("c", "2026-01-01", u.user_id))

    def test_true_for_confirmed_registration(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration_checking_capacity("c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=5)
        self.assertTrue(self.store.has_active_registration("c", "2026-01-01", u.user_id))

    def test_true_for_waitlisted_registration(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration_checking_capacity("c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=0)
        self.assertTrue(self.store.has_active_registration("c", "2026-01-01", u.user_id))

    def test_false_for_pending_confirmation_only(self):
        # Deliberately excluded -- see the method's own docstring: a
        # brand-new guest re-submitting before clicking their confirm link
        # is handled separately (book() resends on purpose), not a
        # double-booking.
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration("c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.assertFalse(self.store.has_active_registration("c", "2026-01-01", u.user_id))

    def test_false_for_canceled_registration(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        reg = self.store.add_registration_checking_capacity("c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=5)
        self.store.cancel(reg.registration_id, canceled_by="guest")
        self.assertFalse(self.store.has_active_registration("c", "2026-01-01", u.user_id))

    def test_false_for_a_different_occurrence_date_or_course(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration_checking_capacity("c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=5)
        self.assertFalse(self.store.has_active_registration("c", "2026-01-02", u.user_id))
        self.assertFalse(self.store.has_active_registration("other-course", "2026-01-01", u.user_id))

    def test_false_for_a_different_user_at_the_same_slot(self):
        u1 = self.store.upsert_user_for_booking("a@x.com", "A")
        u2 = self.store.upsert_user_for_booking("b@x.com", "B")
        self.store.add_registration_checking_capacity("c", "2026-01-01", u1.user_id, hash_token(new_token()), capacity=5)
        self.assertFalse(self.store.has_active_registration("c", "2026-01-01", u2.user_id))


class HasPendingRegistrationTest(StoreTestBase):
    """2026-07-11, the operator: "silent re-registration for unconfirmed accounts"
    -- Store.has_pending_registration() is the STATUS_PENDING_CONFIRMATION
    twin of has_active_registration() above, used by app.webapp.App.book()
    to stop a retried booking attempt (before the guest ever clicks their
    confirm link) from inserting a second pending row for the exact same
    course+date+user -- see the method's own docstring for the
    multi-row-promoted-at-once bug this closes."""

    def test_false_when_user_has_no_registration_at_all(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.assertFalse(self.store.has_pending_registration("c", "2026-01-01", u.user_id))

    def test_true_for_a_pending_confirmation_row(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration("c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.assertTrue(self.store.has_pending_registration("c", "2026-01-01", u.user_id))

    def test_false_for_a_confirmed_or_waitlisted_row(self):
        # The active/pending checks are deliberately disjoint -- once a
        # pending row is promoted (confirm_pending_registration), it's no
        # longer "pending" and has_active_registration takes over instead.
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration_checking_capacity("c", "2026-01-01", u.user_id, hash_token(new_token()), capacity=5)
        self.assertFalse(self.store.has_pending_registration("c", "2026-01-01", u.user_id))

    def test_false_once_the_pending_row_is_promoted(self):
        # A pending row can only leave STATUS_PENDING_CONFIRMATION by being
        # promoted (Store.cancel() itself only ever acts on CONFIRMED/
        # WAITLISTED rows, so there's no direct "cancel a pending row"
        # path) -- confirm it here via confirm_pending_registration, same
        # as my_confirm() does.
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.store.confirm_pending_registration(reg.registration_id, capacity=5, cancel_token_hash=hash_token(new_token()))
        self.assertFalse(self.store.has_pending_registration("c", "2026-01-01", u.user_id))

    def test_false_for_a_different_occurrence_date_or_course(self):
        u = self.store.upsert_user_for_booking("a@x.com", "A")
        self.store.add_registration("c", "2026-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.assertFalse(self.store.has_pending_registration("c", "2026-01-02", u.user_id))
        self.assertFalse(self.store.has_pending_registration("other-course", "2026-01-01", u.user_id))

    def test_false_for_a_different_user_at_the_same_slot(self):
        u1 = self.store.upsert_user_for_booking("a@x.com", "A")
        u2 = self.store.upsert_user_for_booking("b@x.com", "B")
        self.store.add_registration("c", "2026-01-01", u1.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        self.assertFalse(self.store.has_pending_registration("c", "2026-01-01", u2.user_id))


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

    def test_crash_after_archiving_but_before_live_removal_is_recoverable(self):
        # 2026-07-15: erase_user() archives FIRST, removes the live rows
        # LAST -- the reverse of the original order -- specifically so
        # that a hard crash in the middle leaves a harmless DUPLICATE
        # (already archived, still also live) rather than losing the
        # erasure record outright (the old order's real failure mode: a
        # crash right after the live-removal write meant the row was
        # gone from users.csv and never made it into the archive, with
        # no way to re-run since a missing user_id in users.csv just
        # returns False). Simulates that crash by raising partway through
        # the live-removal step, then confirms a second call finishes the
        # job cleanly -- no duplicate archived rows, nothing left live.
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest Name")
        reg = self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))

        real_replace = os.replace
        call_count = {"n": 0}

        def flaky_replace(src, dst):
            call_count["n"] += 1
            # The 3rd os.replace() inside erase_user() is the first LIVE
            # -removal write (users.csv) -- both archive writes (1st, 2nd)
            # must already have landed by the time this "crash" hits.
            if call_count["n"] == 3:
                raise OSError("simulated hard crash mid-erasure")
            return real_replace(src, dst)

        with mock.patch("app.storage.os.replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                self.store.erase_user(u.user_id, "erased:deadbeef")

        # Archive already has the erased user -- not lost by the "crash".
        archived_users = self.store.read_users(scope="archived")
        self.assertEqual(len(archived_users), 1)
        self.assertEqual(archived_users[0]["email"], "erased:deadbeef")
        # But the live rows are still there too (the part that didn't
        # finish) -- a recoverable duplicate, not silent data loss.
        self.assertIsNotNone(self.store.find_user_by_email("guest@example.com"))

        # Re-running finishes the job: no duplicate archived rows, live
        # rows now gone.
        ok = self.store.erase_user(u.user_id, "erased:deadbeef")
        self.assertTrue(ok)
        self.assertEqual(len(self.store.read_users(scope="archived")), 1)
        self.assertEqual(
            [r["registration_id"] for r in self.store.read_registrations(scope="archived")],
            [reg.registration_id],
        )
        self.assertIsNone(self.store.find_user_by_email("guest@example.com"))
        self.assertEqual(self.store.registrations_for_user(u.user_id), [])

    def test_read_registrations_scope_filters(self):
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("c", "2026-01-01", u.user_id, hash_token(new_token()))
        self.store.erase_user(u.user_id, "erased:x")
        self.assertEqual(len(self.store.read_registrations(scope="live")), 0)
        self.assertEqual(len(self.store.read_registrations(scope="archived")), 1)
        self.assertEqual(len(self.store.read_registrations(scope="all")), 1)


# 2026-07-14: MergeArchivedRegistrationsTest (Store.merge_archived_registrations,
# the mutating method behind the now-removed `my-bt admin dearchive`) was
# removed here -- the operator: "lets remove this command from my-bt admin
# please as this is a clear GDPR violation" (permanently re-attaching
# pre-erasure history onto a live account undoes an Art. 17 erasure). The
# READ-ONLY equivalent (app.cli_list.merge_archived_for_display, used by
# /admin and `my-bt list --all`/`--past`) is untouched and still fully
# tested in tests/test_cli_list.py.


class RenameCourseShortnameTest(StoreTestBase):
    """Store.rename_course_shortname -- the CSV-row side of `my-bt admin
    rename-course` (2026-07-08, the operator: "rename lux-wed-mindfulness to
    lux-wed-mind ... provide a command to migrate the existing data").
    Does NOT touch settings.toml or the calendar -- see
    app.calendar_sync.resync_after_course_rename for the calendar side."""

    def test_renames_live_rows(self):
        user = self.store.upsert_user_for_booking("a@example.com", "A")
        self.store.add_registration("old-name", "2026-01-01", user.user_id, hash_token(new_token()))
        changed = self.store.rename_course_shortname("old-name", "new-name")
        self.assertEqual(changed, 1)
        self.assertEqual(self.store.read_registrations(scope="live")[0]["course_shortname"], "new-name")

    def test_renames_archived_rows_too(self):
        user = self.store.upsert_user_for_booking("a@example.com", "A")
        self.store.add_registration("old-name", "2026-01-01", user.user_id, hash_token(new_token()))
        self.store.erase_user(user.user_id, "erased:x")

        changed = self.store.rename_course_shortname("old-name", "new-name")

        self.assertEqual(changed, 1)
        self.assertEqual(self.store.read_registrations(scope="archived")[0]["course_shortname"], "new-name")

    def test_live_and_archived_both_counted(self):
        live_user = self.store.upsert_user_for_booking("live@example.com", "Live")
        self.store.add_registration("old-name", "2026-01-01", live_user.user_id, hash_token(new_token()))
        archived_user = self.store.upsert_user_for_booking("gone@example.com", "Gone")
        self.store.add_registration("old-name", "2026-02-01", archived_user.user_id, hash_token(new_token()))
        self.store.erase_user(archived_user.user_id, "erased:x")

        changed = self.store.rename_course_shortname("old-name", "new-name")
        self.assertEqual(changed, 2)

    def test_other_courses_are_not_touched(self):
        user = self.store.upsert_user_for_booking("a@example.com", "A")
        self.store.add_registration("other-course", "2026-01-01", user.user_id, hash_token(new_token()))
        changed = self.store.rename_course_shortname("old-name", "new-name")
        self.assertEqual(changed, 0)
        self.assertEqual(self.store.read_registrations(scope="live")[0]["course_shortname"], "other-course")

    def test_no_matching_rows_is_a_safe_no_op(self):
        self.assertEqual(self.store.rename_course_shortname("old-name", "new-name"), 0)

    def test_registration_id_and_other_fields_preserved(self):
        user = self.store.upsert_user_for_booking("a@example.com", "A")
        reg = self.store.add_registration("old-name", "2026-01-01", user.user_id, hash_token(new_token()))
        self.store.rename_course_shortname("old-name", "new-name")
        row = self.store.read_registrations(scope="live")[0]
        self.assertEqual(row["registration_id"], reg.registration_id)
        self.assertEqual(row["user_id"], user.user_id)
        self.assertEqual(row["occurrence_date"], "2026-01-01")


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


class FormatDisplayTimestampTest(unittest.TestCase):
    """2026-07-07, the operator (screenshot of the operator's CalDAV event showing
    "registered 2026-07-07T00:47:57+00:00"): "please use for TIMESTAMPS
    wherever you currently have this format ... YYYY-MM-DD_HHMM.SS"."""

    def test_formats_a_now_iso_style_timestamp(self):
        # 2026-07-14, the operator: "In the GUI please split the timestamp with a
        # space between date and time" then "(and add a 'h' between HH
        # and MM)" -- format changed from "2026-07-07_0047.57" to
        # "2026-07-07 00h47.57".
        self.assertEqual(format_display_timestamp("2026-07-07T00:47:57+00:00"), "2026-07-07 00h47.57")

    def test_blank_input_stays_blank(self):
        # canceled_at is "" for any registration that was never canceled --
        # must not raise or turn into some garbage rendering of "".
        self.assertEqual(format_display_timestamp(""), "")

    def test_unparseable_input_is_returned_unchanged(self):
        self.assertEqual(format_display_timestamp("not-a-timestamp"), "not-a-timestamp")

    def test_exact_midnight_renders_as_date_only(self):
        # 2026-07-08, the operator (screenshot of /admin?past=1's Registered
        # column showing "2025-10-18_0000.00" for SimplyMeet.me-imported
        # rows): "if we have no time, then please display just the date"
        # -- migrate_simplymeet.py stamps every imported row's
        # registered_at as "<occurrence_date>T00:00:00" (no real time-of-
        # day known), so exact midnight is treated as "no real time" and
        # rendered without the misleadingly precise "_HHMM.SS" suffix.
        self.assertEqual(format_display_timestamp("2025-10-18T00:00:00"), "2025-10-18")
        self.assertEqual(format_display_timestamp("2025-10-18T00:00:00+00:00"), "2025-10-18")

    def test_one_second_past_midnight_still_shows_full_timestamp(self):
        # Guards against an overly broad "date == midnight" check -- only
        # EXACT 00:00:00 is treated as the placeholder; a real registration
        # that happens to fall in the first minute of a day must still
        # render its real time.
        self.assertEqual(format_display_timestamp("2025-10-18T00:00:01+00:00"), "2025-10-18 00h00.01")


class StatusLabelTest(unittest.TestCase):
    """2026-07-08, the operator: "I prefer Host and Guest and then also
    'Confirmed' for the status". Moved here 2026-07-13 from
    app/webapp.py's _status_label so app/cli_list.py (and therefore
    `my-bt list`'s own clean default view) can show the identical label."""

    def test_known_statuses_get_capitalized_labels(self):
        self.assertEqual(status_label(STATUS_CONFIRMED), "Confirmed")
        self.assertEqual(status_label(STATUS_WAITLISTED), "Waitlisted")
        self.assertEqual(status_label(STATUS_PENDING_CONFIRMATION), "Pending confirmation")
        self.assertEqual(status_label(STATUS_CANCELED_BY_GUEST), "Canceled by guest")
        self.assertEqual(status_label(STATUS_CANCELED_BY_HOST), "Canceled by host")

    def test_unknown_status_falls_back_to_generic_humanization(self):
        self.assertEqual(status_label("some_new_status"), "Some new status")


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


class SecureDataPathTest(unittest.TestCase):
    """2026-07-09: real production incident on the operator's own VPS -- he ran
    `my-bt cancel` directly as root, leaving registrations.csv root:root
    mode 0600 -- completely unreadable by my-booking-watchdog.service (runs
    as the unprivileged my-booking user/group), which then crashed with
    PermissionError on its very next scheduled read. _secure_data_path is
    the self-healing fix _LockedCsv now applies on every write/creation
    (see _atomic_write and __enter__) -- these tests exercise it directly
    rather than needing an actual multi-user setup with a real "my-booking"
    system group to reproduce the original bug."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registrations.csv"
        self.path.touch()

    def tearDown(self):
        self._tmp.cleanup()

    def test_chmod_grants_group_read_not_just_owner(self):
        os.chmod(self.path, 0o600)
        with mock.patch("app.storage.grp.getgrnam", side_effect=KeyError()):
            _secure_data_path(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_directory_mode_gets_the_execute_bit_when_asked(self):
        directory = Path(self._tmp.name) / "archived"
        directory.mkdir()
        with mock.patch("app.storage.grp.getgrnam", side_effect=KeyError()):
            _secure_data_path(directory, mode=0o750)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o750)

    def test_chgrps_to_the_service_group_when_it_exists(self):
        fake_gid = 424242
        with mock.patch("app.storage.grp.getgrnam") as m_getgrnam, \
                mock.patch("app.storage.os.chown") as m_chown:
            m_getgrnam.return_value = mock.Mock(gr_gid=fake_gid)
            _secure_data_path(self.path)
        m_getgrnam.assert_called_once_with(_SERVICE_GROUP)
        # -1 as the uid arg: never touches ownership, only the group -- see
        # _secure_data_path's own docstring on why that's deliberate.
        m_chown.assert_called_once_with(self.path, -1, fake_gid)

    def test_missing_service_group_does_not_raise_and_chmod_still_applies(self):
        # e.g. a dev checkout or this very test suite, where no system
        # group named "my-booking" exists at all.
        with mock.patch("app.storage.grp.getgrnam", side_effect=KeyError("no such group")):
            try:
                _secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"_secure_data_path must swallow a missing group, raised {exc!r}")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_chown_permission_error_does_not_raise(self):
        # e.g. the calling process isn't root and isn't a member of the
        # target group -- POSIX refuses the chgrp, but the write itself
        # must still succeed.
        with mock.patch("app.storage.grp.getgrnam") as m_getgrnam, \
                mock.patch("app.storage.os.chown", side_effect=PermissionError("not allowed")):
            m_getgrnam.return_value = mock.Mock(gr_gid=1)
            try:
                _secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"_secure_data_path must swallow a chown PermissionError, raised {exc!r}")

    def test_chmod_failure_does_not_raise(self):
        with mock.patch("app.storage.os.chmod", side_effect=OSError("nope")), \
                mock.patch("app.storage.grp.getgrnam", side_effect=KeyError()):
            try:
                _secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"_secure_data_path must swallow a chmod OSError, raised {exc!r}")


class FsyncDirTest(unittest.TestCase):
    """2026-07-15, the operator, on hard-reboot data safety: fsyncing the temp
    file before os.replace() (already in place) makes the new CONTENT
    durable, but the rename() itself isn't guaranteed durable on Linux
    until the containing directory's own inode is fsynced too --
    _fsync_dir() is the fix, called right after every os.replace() in
    _atomic_write().

    A bare "was os.fsync called" mock assertion would pass even if a bug
    fsynced the wrong fd (e.g. the just-renamed file again, instead of
    its directory) -- these tests resolve the real fd back to a path via
    /proc/self/fd (Linux-only, matches this app's only deployment target)
    to prove it's actually the DIRECTORY's fd, not just "fsync was called
    some number of times"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_path = Path(self._tmp.name)

    def test_fsyncs_the_directory_fd_specifically(self):
        # Resolve the fd back to a path via /proc/self/fd WHILE it's still
        # open (inside the spy, before _fsync_dir's own finally: closes
        # it) -- a bare "os.fsync was called" mock assertion would pass
        # even if a bug fsynced the wrong fd (e.g. some other just-closed
        # file reusing the same small integer); resolving the live fd to
        # its real path is what actually proves this targeted the
        # directory.
        resolved_paths = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            resolved_paths.append(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        with mock.patch("app.storage.os.fsync", side_effect=spy_fsync):
            _fsync_dir(self.dir_path)

        self.assertEqual(resolved_paths, [os.path.realpath(str(self.dir_path))])

    def test_closes_the_directory_fd_afterwards(self):
        opened_fds = []
        real_open = os.open

        def spy_open(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            opened_fds.append(fd)
            return fd

        with mock.patch("app.storage.os.open", side_effect=spy_open):
            _fsync_dir(self.dir_path)

        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])  # closed -- no longer a valid fd

    def test_missing_directory_is_best_effort_not_a_crash(self):
        missing = self.dir_path / "does-not-exist"
        try:
            _fsync_dir(missing)
        except Exception as exc:
            self.fail(f"_fsync_dir must swallow a missing directory, raised {exc!r}")

    def test_atomic_write_fsyncs_the_target_directory(self):
        # End-to-end through the real path Store uses: writing a row via
        # _LockedCsv must fsync the data directory itself, not just the
        # temp file, and must do so AFTER os.replace() (the rename is
        # what needs the directory fsync to be durable, not the write
        # that preceded it).
        calls = []
        real_replace = os.replace
        real_fsync_dir = _fsync_dir

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        def spy_fsync_dir(path):
            calls.append("fsync_dir")
            return real_fsync_dir(path)

        path = self.dir_path / "registrations.csv"
        with mock.patch("app.storage.os.replace", side_effect=spy_replace), \
                mock.patch("app.storage._fsync_dir", side_effect=spy_fsync_dir):
            with _LockedCsv(path, REG_FIELDS) as (rows, write):
                write(rows, "test row")
        self.assertEqual(calls, ["replace", "fsync_dir"])


class LockedCsvWritePermissionsTest(unittest.TestCase):
    """End-to-end: an actual _LockedCsv write (the real path Store uses for
    every CSV mutation) leaves the file group-readable (0640), not
    owner-only (0600) -- and a data directory it has to create fresh gets
    the execute bit too (0750), so traversal by a different group member
    still works. See SecureDataPathTest above for the underlying helper's
    own unit tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registrations.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def test_written_file_is_group_readable(self):
        with _LockedCsv(self.path, REG_FIELDS) as (rows, write):
            write([])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_newly_created_data_directory_is_group_traversable(self):
        nested = Path(self._tmp.name) / "fresh_subdir" / "registrations.csv"
        with _LockedCsv(nested, REG_FIELDS) as (rows, write):
            write([])
        self.assertEqual(stat.S_IMODE(os.stat(nested.parent).st_mode), 0o750)

    def test_pre_existing_directory_is_also_self_healed_on_every_write(self):
        # 2026-07-09, the operator: "my-bt needs to ensure that touching the files
        # if my-bt is ran as root do not change permission or ownership
        # under /var/lib for instance" -- a directory that was ALREADY
        # there (e.g. created by an earlier root-run command, before this
        # fix existed) must still get repaired on the next write, not just
        # directories _LockedCsv happens to create fresh itself. Mode is
        # unconditionally reset to 0750 on every write, not left as
        # whatever it drifted to.
        os.chmod(self._tmp.name, 0o701)
        with _LockedCsv(self.path, REG_FIELDS) as (rows, write):
            write([])
        self.assertEqual(stat.S_IMODE(os.stat(self._tmp.name).st_mode), 0o750)


class TouchLoginClearsDeletionWarningTest(StoreTestBase):
    """2026-07-09, the operator: account-deletion warning email (see
    app.retention.send_account_deletion_warnings) -- a real login must
    reset the dormancy clock that warning is based on, so
    deletion_warning_sent_at is cleared here, not just last_login_at set."""

    def test_touch_login_sets_last_login_at(self):
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.assertEqual(user.last_login_at, "")
        self.store.touch_login(user.user_id)
        self.assertTrue(self.store.find_user_by_id(user.user_id).last_login_at)

    def test_touch_login_clears_a_previously_set_deletion_warning(self):
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.store.mark_deletion_warning_sent(user.user_id, "2028-01-01T00:00:00+00:00")
        self.assertTrue(self.store.find_user_by_id(user.user_id).deletion_warning_sent_at)
        self.store.touch_login(user.user_id)
        self.assertEqual(self.store.find_user_by_id(user.user_id).deletion_warning_sent_at, "")

    def test_touch_login_on_unknown_user_id_is_a_no_op(self):
        self.store.touch_login("00000000-0000-0000-0000-000000000000")  # must not raise


class MarkDeletionWarningSentTest(StoreTestBase):
    def test_sets_the_field_to_the_given_timestamp(self):
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.store.mark_deletion_warning_sent(user.user_id, "2028-01-01T00:00:00+00:00")
        self.assertEqual(
            self.store.find_user_by_id(user.user_id).deletion_warning_sent_at, "2028-01-01T00:00:00+00:00",
        )

    def test_defaults_to_now_when_no_timestamp_given(self):
        user = self.store.upsert_user_for_booking("guest@example.org", "Guest")
        self.store.mark_deletion_warning_sent(user.user_id)
        self.assertTrue(self.store.find_user_by_id(user.user_id).deletion_warning_sent_at)

    def test_unknown_user_id_is_a_no_op(self):
        self.store.mark_deletion_warning_sent("00000000-0000-0000-0000-000000000000")  # must not raise


if __name__ == "__main__":
    unittest.main()
