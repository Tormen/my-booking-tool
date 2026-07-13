import tempfile
import unittest
from datetime import date

from app.caldav_client import CalDAVClient, Response
from app.erasure import erase_user_by_email, find_archived_user_ids_for_email
from app.security import hash_email_for_erasure, hash_token, is_erased_email, new_token
from app.storage import STATUS_CANCELED_BY_GUEST, STATUS_CONFIRMED, STATUS_WAITLISTED, Store

from .helpers import make_course, make_settings

PROPFIND_BODY = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/caldav/Bookings/</D:href>
    <D:propstat><D:prop><D:displayname>Bookings</D:displayname></D:prop></D:propstat>
  </D:response>
</D:multistatus>"""

EMPTY_REPORT = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>"""


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body="", extra_headers=None):
        self.calls.append((method, url, body))
        if method == "PROPFIND":
            return Response(207, {}, PROPFIND_BODY)
        if method == "REPORT":
            return Response(207, {}, EMPTY_REPORT)
        if method == "PUT":
            return Response(201, {"etag": '"e1"'}, "")
        if method == "DELETE":
            return Response(204, {}, "")
        raise AssertionError(f"unexpected {method} {url}")


class ErasureFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.settings = make_settings()

    def tearDown(self):
        self._tmp.cleanup()

    def test_erase_cancels_future_bookings_and_archives(self):
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest")
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
        self.store.upsert_user_for_booking("one@example.com", "One")
        self.store.upsert_user_for_booking("two@example.com", "Two")
        erase_user_by_email(self.store, self.settings, "one@example.com", today=date(2027, 1, 1))
        erase_user_by_email(self.store, self.settings, "two@example.com", today=date(2027, 1, 1))
        emails = {u["email"] for u in self.store.read_users(scope="archived")}
        self.assertEqual(len(emails), 2)

    def test_without_caldav_never_touches_the_network(self):
        # Default/backward-compatible behavior (caldav=None): cancels and
        # archives exactly as before, no CalDAV call attempted at all --
        # this is what every pre-2026-07-06 direct call (and every test
        # above) relies on.
        course = make_course(shortname="yoga-class-1", capacity=1)
        settings = make_settings(courses=(course,), booking_calendar="Bookings")
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("yoga-class-1", "2099-01-01", u.user_id, hash_token(new_token()))
        ok = erase_user_by_email(self.store, settings, "guest@example.com", today=date(2027, 1, 1))
        self.assertTrue(ok)  # no caldav given -> no network call, no crash


class ErasureCalendarSyncTest(unittest.TestCase):
    """2026-07-06: erase_user_by_email's pre-archival force-cancel now runs
    the same app.cancel_flow.cancel_and_promote as every other cancellation
    path when given a `caldav` client -- both real callers (app/webapp.py's
    `/my` self-erasure and `my-bt erase`, see scripts/my-bt::cmd_erase) pass
    one. Previously this was a documented gap: erasure canceled the rows but
    never re-synced the calendar or promoted a waitlisted person."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        course = make_course(shortname="yoga-class-1", title="Yoga", capacity=1)
        self.settings = make_settings(courses=(course,), booking_calendar="Bookings")
        self.transport = FakeTransport()
        self.caldav = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self.transport,
        )
        self.sent_emails: list[tuple[str, str, str]] = []
        from unittest.mock import patch
        patcher = patch(
            "app.cancel_flow.send_mail",
            side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=(), reply_to=None: self.sent_emails.append((to, subject, body)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_erase_with_caldav_syncs_the_calendar(self):
        u = self.store.upsert_user_for_booking("guest@example.com", "Guest")
        self.store.add_registration("yoga-class-1", "2099-01-01", u.user_id, hash_token(new_token()))

        ok = erase_user_by_email(
            self.store, self.settings, "guest@example.com", today=date(2027, 1, 1), caldav=self.caldav
        )
        self.assertTrue(ok)
        methods = [m for m, _url, _body in self.transport.calls]
        self.assertIn("PROPFIND", methods)
        self.assertIn("REPORT", methods)
        # Zero active remain after this force-cancel, and (FakeTransport's
        # EMPTY_REPORT) no event currently exists for this occurrence either
        # -- nothing to delete, so no PUT/DELETE fires. The REPORT call
        # itself is the proof that calendar_sync actually ran (a
        # pre-2026-07-06 erase never made any CalDAV call at all).
        self.assertNotIn("PUT", methods)
        self.assertNotIn("DELETE", methods)

    def test_erase_with_caldav_promotes_next_waitlisted(self):
        # capacity=1: erasing the confirmed guest frees the only spot, so
        # the waitlisted guest must be promoted, exactly like the web
        # admin's cancel does.
        confirmed_user = self.store.upsert_user_for_booking("erased@example.com", "Erased")
        self.store.add_registration(
            "yoga-class-1", "2099-01-01", confirmed_user.user_id, hash_token(new_token()), status=STATUS_CONFIRMED,
        )
        waiter = self.store.upsert_user_for_booking("waiter@example.com", "Waiter")
        waiter_reg = self.store.add_registration(
            "yoga-class-1", "2099-01-01", waiter.user_id, hash_token(new_token()), status=STATUS_WAITLISTED,
        )

        ok = erase_user_by_email(
            self.store, self.settings, "erased@example.com", today=date(2027, 1, 1), caldav=self.caldav
        )
        self.assertTrue(ok)
        promoted = self.store.find_by_id(waiter_reg.registration_id)
        self.assertEqual(promoted.status, STATUS_CONFIRMED)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertTrue(any(s.startswith("You're in!") for s in subjects))

    def test_erase_without_caldav_arg_still_skips_network_same_as_before(self):
        # Same erase_user_by_email call, no caldav= kwarg -- must behave
        # exactly like the pre-2026-07-06 code (cancel + archive only).
        u = self.store.upsert_user_for_booking("guest2@example.com", "Guest2")
        self.store.add_registration("yoga-class-1", "2099-01-01", u.user_id, hash_token(new_token()))
        ok = erase_user_by_email(self.store, self.settings, "guest2@example.com", today=date(2027, 1, 1))
        self.assertTrue(ok)
        self.assertEqual(self.transport.calls, [])


class FindArchivedUserIdsForEmailTest(unittest.TestCase):
    """find_archived_user_ids_for_email() -- how the READ-ONLY merges
    (app.cli_list.merge_archived_for_display, used by `/admin` and
    `my-bt list --all`/`--past`) find a re-booked guest's pre-erasure
    identity from their current, live email alone. 2026-07-14: moved here
    from tests/test_cli_history.py, which tested it alongside
    app.cli_history.run_merge() -- that module (and the `my-bt admin
    dearchive` command it backed) was removed entirely as a clear GDPR
    violation for permanently re-linking supposedly-erased
    history onto a live, identifiable account. This function itself
    stays -- it's read-only (finds ids, moves nothing) and the display-
    time merges that use it were explicitly kept: "the implicit
    functionality of this baked into /admin and my-bt list should
    stay." """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()

    def _erase(self, email: str, name: str = "Guest"):
        user = self.store.upsert_user_for_booking(email, name)
        self.store.add_registration("c", "2026-01-01", user.user_id, hash_token(new_token()))
        hashed = hash_email_for_erasure(user.email, self.settings.erasure_pepper)
        self.store.erase_user(user.user_id, hashed)
        return user.user_id

    def test_finds_archived_user_after_rebooking(self):
        old_id = self._erase("guest@example.com")
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
        old1 = self._erase("guest@example.com")
        old2 = self._erase("guest@example.com")
        found = find_archived_user_ids_for_email(self.store, self.settings, "guest@example.com")
        self.assertEqual(set(found), {old1, old2})


if __name__ == "__main__":
    unittest.main()
