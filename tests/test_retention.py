import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import patch

from app.retention import (
    account_deletion_counts_by_month,
    account_deletion_date,
    purge_dormant_accounts,
    registration_purge_counts_by_month,
    registration_purge_date,
    run_purge,
    send_account_deletion_warnings,
    should_purge,
)
from app.security import hash_token, is_erased_email, new_token
from app.storage import STATUS_PENDING_CONFIRMATION, USER_FIELDS, Store, _LockedCsv

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


def _set_user_row(store: Store, user_id: str, **fields) -> None:
    """Test-only helper: no Store method exposes writing created_at/
    last_login_at/deletion_warning_sent_at directly (they're always
    system-set, e.g. Store.touch_login()'s own now_iso() call) -- reaches
    into the same _LockedCsv primitive Store itself uses, same pattern
    tests/test_cancel_flow.py uses for registration rows via
    dataclasses.replace + replace_all_registrations (no users.csv
    equivalent of that one exists, so this goes one level lower)."""
    with _LockedCsv(store.users_path, USER_FIELDS) as (rows, write):
        for row in rows:
            if row["user_id"] == user_id:
                row.update(fields)
        write(rows, "test setup")


class SendAccountDeletionWarningsTest(unittest.TestCase):
    """2026-07-09, the operator: "Our scheduler that then deletes accounts should
    detect imminent accounts that would need to be deleted and then send
    out such an email" -- see app.retention.send_account_deletion_
    warnings's own docstring for the full story (reuses retention_months
    as the dormancy threshold, last_login_at falling back to created_at,
    exactly ONE email per dormancy period)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(retention_months=24, account_deletion_warning_days=30)
        self.today = date(2028, 1, 1)
        self.sent_emails = []
        patcher = patch(
            "app.retention.send_mail",
            side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=(): (
                self.sent_emails.append((to, subject, body, bcc_addrs))
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_user(self, email: str, last_login_at: str = "", created_at: str = ""):
        user = self.store.upsert_user_for_booking(email, "Guest")
        _set_user_row(self.store, user.user_id, last_login_at=last_login_at, created_at=created_at)
        return user

    def test_disabled_when_warning_days_is_zero(self):
        settings = make_settings(retention_months=24, account_deletion_warning_days=0)
        self._make_user("dormant@example.org", last_login_at="2025-12-15T00:00:00+00:00")  # ~24mo before today - 30d
        warned = send_account_deletion_warnings(self.store, settings, today=self.today)
        self.assertEqual(warned, 0)
        self.assertEqual(self.sent_emails, [])

    def test_warns_when_within_the_configured_window(self):
        # retention_months=24, account_deletion_warning_days=30: a login
        # 24 months minus 10 days before "today" means the account is 10
        # days from deletion -- inside the 30-day warning window.
        self._make_user("soon@example.org", last_login_at="2026-01-11T00:00:00+00:00")
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 1)
        to, subject, body, _bcc = self.sent_emails[0]
        self.assertEqual(to, "soon@example.org")
        self.assertIn("account will be deleted soon", subject)
        self.assertIn("2026-01-11", body)

    def test_does_not_warn_when_outside_the_window(self):
        # Only 10 months of inactivity so far -- nowhere near the 24-month
        # deletion threshold, let alone inside the 30-day warning window.
        self._make_user("active@example.org", last_login_at="2027-03-01T00:00:00+00:00")
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 0)

    def test_does_not_warn_once_already_past_the_deletion_date(self):
        # This warning is a courtesy notice only (see
        # purge_dormant_accounts() for the actual enforcement, tested
        # separately below) -- an overdue account just never gets a
        # warning here either, rather than re-warning forever.
        self._make_user("overdue@example.org", last_login_at="2020-01-01T00:00:00+00:00")
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 0)

    def test_falls_back_to_created_at_when_never_logged_in(self):
        self._make_user("neverlogged@example.org", last_login_at="", created_at="2026-01-11T00:00:00+00:00")
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 1)

    def test_only_one_email_is_ever_sent_per_dormancy_period(self):
        user = self._make_user("soon@example.org", last_login_at="2026-01-11T00:00:00+00:00")
        first = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(first, 1)
        second = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.sent_emails), 1)

    def test_touch_login_clears_the_warning_flag_so_a_future_dormancy_can_warn_again(self):
        user = self._make_user("comesback@example.org", last_login_at="2026-01-11T00:00:00+00:00")
        send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(len(self.sent_emails), 1)
        self.store.touch_login(user.user_id)
        # Freshly logged in -- nowhere near dormant anymore, so no new
        # warning fires right now, but the flag itself must be cleared...
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 0)
        reloaded = self.store.find_user_by_id(user.user_id)
        self.assertEqual(reloaded.deletion_warning_sent_at, "")

    def test_bcc_attendee_emails_applies_to_the_warning_email(self):
        settings = make_settings(
            retention_months=24, account_deletion_warning_days=30, bcc_attendee_emails="watcher@example.org",
        )
        self._make_user("soon@example.org", last_login_at="2026-01-11T00:00:00+00:00")
        send_account_deletion_warnings(self.store, settings, today=self.today)
        _to, _subject, _body, bcc_addrs = self.sent_emails[0]
        self.assertEqual(bcc_addrs, ("watcher@example.org",))

    def test_no_activity_signal_at_all_is_skipped_not_erroring(self):
        # Defensive edge case -- created_at is always set in practice
        # (Store.upsert_user_for_booking always stamps it), but this
        # confirms a blank-everything row can't crash the sweep.
        self._make_user("blank@example.org", last_login_at="", created_at="")
        warned = send_account_deletion_warnings(self.store, self.settings, today=self.today)
        self.assertEqual(warned, 0)


class AccountDeletionDateTest(unittest.TestCase):
    """account_deletion_date() -- the shared date-projection helper both
    send_account_deletion_warnings() and purge_dormant_accounts() (below)
    call, so a listing/warning/purge can never disagree on the date for
    the same account."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(retention_months=24)

    def _make_user(self, email: str, last_login_at: str = "", created_at: str = ""):
        user = self.store.upsert_user_for_booking(email, "Guest")
        _set_user_row(self.store, user.user_id, last_login_at=last_login_at, created_at=created_at)
        return self.store.find_user_by_id(user.user_id)

    def test_projects_from_last_login(self):
        user = self._make_user("a@example.org", last_login_at="2026-01-11T00:00:00+00:00")
        self.assertEqual(account_deletion_date(user, self.settings), date(2028, 1, 11))

    def test_falls_back_to_created_at(self):
        user = self._make_user("b@example.org", last_login_at="", created_at="2026-01-11T00:00:00+00:00")
        self.assertEqual(account_deletion_date(user, self.settings), date(2028, 1, 11))

    def test_none_when_no_activity_signal_at_all(self):
        user = self._make_user("c@example.org", last_login_at="", created_at="")
        self.assertIsNone(account_deletion_date(user, self.settings))


class RegistrationPurgeDateTest(unittest.TestCase):
    """registration_purge_date() -- forward-projected counterpart to
    should_purge(), for `my-bt admin gdpr bookings`'s per-row listing.
    Must agree with should_purge() exactly (same rules, just expressed as
    a date instead of a yes/no)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(retention_months=24, canceled_retention_months=6)

    def _reg(self, occurrence_date, status="confirmed"):
        u = self.store.upsert_user_for_booking(f"{occurrence_date}-{status}@x.com", "X")
        r = self.store.add_registration("c", occurrence_date, u.user_id, hash_token(new_token()))
        if status != "confirmed":
            self.store.cancel(r.registration_id, canceled_by="guest")
            r = self.store.find_by_id(r.registration_id)
        return r

    def test_confirmed_row_projects_occurrence_date_plus_retention_months(self):
        reg = self._reg("2027-06-01")
        self.assertEqual(registration_purge_date(reg, self.settings), date(2029, 6, 1))

    def test_canceled_row_uses_the_earlier_of_the_two_windows(self):
        reg = self._reg("2027-11-01", status="canceled_by_guest")
        reg = replace(reg, canceled_at="2027-05-01T00:00:00+00:00")
        self.store.replace_all_registrations([reg])
        reg = self.store.find_by_id(reg.registration_id)
        # occurrence-based window: 2027-11-01 + 24mo = 2029-11-01
        # canceled-based window: 2027-05-01 + 6mo = 2027-11-01 -- earlier, wins
        self.assertEqual(registration_purge_date(reg, self.settings), date(2027, 11, 1))

    def test_pending_confirmation_row_projects_registered_at_plus_hours(self):
        settings = make_settings(retention_months=24, pending_confirmation_hours=48)
        u = self.store.upsert_user_for_booking("pending@x.com", "X")
        reg = self.store.add_registration("c", "2099-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        reg = replace(reg, registered_at="2027-01-01T00:00:00+00:00")
        self.store.replace_all_registrations([reg])
        reg = self.store.find_by_id(reg.registration_id)
        self.assertEqual(registration_purge_date(reg, settings), date(2027, 1, 3))

    def test_agrees_with_should_purge_at_the_boundary(self):
        reg = self._reg("2026-01-01")
        reg = self.store.find_by_id(reg.registration_id)
        today = date(2028, 1, 1)
        self.assertTrue(should_purge(reg, today, self.settings))
        self.assertLessEqual(registration_purge_date(reg, self.settings), today)


class PurgeDormantAccountsTest(unittest.TestCase):
    """purge_dormant_accounts() -- the actual account-erasure enforcement
    (2026-07-14, the operator: "now we also need the account purge after the same
    duration"). Runs regardless of whether the warning email is enabled
    or was ever sent (the operator: "yes regardless"), tied exactly to
    retention_months (the operator: "this is the GDPR law")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.today = date(2028, 1, 1)

    def _make_user(self, email: str, last_login_at: str = "", created_at: str = ""):
        user = self.store.upsert_user_for_booking(email, "Guest")
        _set_user_row(self.store, user.user_id, last_login_at=last_login_at, created_at=created_at)
        return user

    def test_erases_an_account_past_its_deadline(self):
        settings = make_settings(retention_months=24, account_deletion_warning_days=0)
        self._make_user("overdue@example.org", last_login_at="2020-01-01T00:00:00+00:00")
        purged = purge_dormant_accounts(self.store, settings, today=self.today)
        self.assertEqual(purged, 1)
        self.assertEqual(self.store.read_users(scope="live"), [])
        archived = self.store.read_users(scope="archived")
        self.assertEqual(len(archived), 1)
        self.assertTrue(is_erased_email(archived[0]["email"]))

    def test_runs_regardless_of_whether_the_warning_email_is_enabled(self):
        # account_deletion_warning_days=0 (the warning feature entirely
        # off) must not stop the actual purge -- these are independent.
        settings = make_settings(retention_months=24, account_deletion_warning_days=0)
        self._make_user("overdue@example.org", last_login_at="2020-01-01T00:00:00+00:00")
        purged = purge_dormant_accounts(self.store, settings, today=self.today)
        self.assertEqual(purged, 1)

    def test_runs_even_if_no_warning_was_ever_sent(self):
        settings = make_settings(retention_months=24, account_deletion_warning_days=30)
        user = self._make_user("overdue@example.org", last_login_at="2020-01-01T00:00:00+00:00")
        reloaded = self.store.find_user_by_id(user.user_id)
        self.assertEqual(reloaded.deletion_warning_sent_at, "")  # never warned
        purged = purge_dormant_accounts(self.store, settings, today=self.today)
        self.assertEqual(purged, 1)

    def test_does_not_erase_an_account_not_yet_past_its_deadline(self):
        settings = make_settings(retention_months=24)
        self._make_user("active@example.org", last_login_at="2027-03-01T00:00:00+00:00")
        purged = purge_dormant_accounts(self.store, settings, today=self.today)
        self.assertEqual(purged, 0)
        self.assertEqual(len(self.store.read_users(scope="live")), 1)

    def test_returns_the_number_erased_across_multiple_accounts(self):
        settings = make_settings(retention_months=24)
        self._make_user("overdue1@example.org", last_login_at="2020-01-01T00:00:00+00:00")
        self._make_user("overdue2@example.org", last_login_at="2020-06-01T00:00:00+00:00")
        self._make_user("active@example.org", last_login_at="2027-03-01T00:00:00+00:00")
        purged = purge_dormant_accounts(self.store, settings, today=self.today)
        self.assertEqual(purged, 2)
        self.assertEqual(len(self.store.read_users(scope="live")), 1)


class PurgeCountsByMonthTest(unittest.TestCase):
    """registration_purge_counts_by_month() / account_deletion_counts_by_month()
    -- the "expected purge counts per month" table `my-bt admin gdpr`
    prints (2026-07-14, the operator: "please also provide a table with the
    currently expected purge counts per month for accounts and for
    bookings"). Both just bucket the same per-row/per-account dates
    registration_purge_date()/account_deletion_date() already compute,
    by "YYYY-MM"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(retention_months=24, canceled_retention_months=6)

    def _reg(self, occurrence_date, status="confirmed"):
        u = self.store.upsert_user_for_booking(f"{occurrence_date}-{status}-{len(self.store.all_registrations())}@x.com", "X")
        r = self.store.add_registration("c", occurrence_date, u.user_id, hash_token(new_token()))
        if status != "confirmed":
            self.store.cancel(r.registration_id, canceled_by="guest")
            r = self.store.find_by_id(r.registration_id)
        return r

    def _make_user(self, email: str, last_login_at: str = ""):
        user = self.store.upsert_user_for_booking(email, "Guest")
        _set_user_row(self.store, user.user_id, last_login_at=last_login_at)
        return user

    def test_registration_counts_grouped_by_purge_month(self):
        self._reg("2026-01-01")  # purges 2028-01
        self._reg("2026-01-15")  # same month, purges 2028-01
        self._reg("2027-06-01")  # purges 2029-06
        counts = registration_purge_counts_by_month(self.store, self.settings)
        self.assertEqual(counts, {"2028-01": 2, "2029-06": 1})

    def test_registration_counts_empty_store_gives_empty_dict(self):
        self.assertEqual(registration_purge_counts_by_month(self.store, self.settings), {})

    def test_account_counts_grouped_by_deletion_month(self):
        self._make_user("a@example.org", last_login_at="2026-01-01T00:00:00+00:00")  # 2028-01
        self._make_user("b@example.org", last_login_at="2026-01-20T00:00:00+00:00")  # 2028-01
        self._make_user("c@example.org", last_login_at="2027-06-01T00:00:00+00:00")  # 2029-06
        counts = account_deletion_counts_by_month(self.store, self.settings)
        self.assertEqual(counts, {"2028-01": 2, "2029-06": 1})

    def test_account_counts_skips_accounts_with_no_activity_timestamp(self):
        self._make_user("no-activity@example.org", last_login_at="")
        _set_user_row(self.store, self.store.read_users(scope="live")[0]["user_id"], created_at="")
        counts = account_deletion_counts_by_month(self.store, self.settings)
        self.assertEqual(counts, {})


if __name__ == "__main__":
    unittest.main()
