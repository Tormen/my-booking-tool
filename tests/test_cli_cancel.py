import tempfile
import unittest
from unittest.mock import patch

from app.caldav_client import CalDAVClient, Response
from app.cli_cancel import cancel_registration, classify_cancel_query, resolve_course_shortname_for_date
from app.cli_list import assign_short_ids
from app.security import hash_token, new_token
from app.storage import (
    STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store,
)

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
    """Same minimal fake as tests/test_webapp.py's -- records every call so
    tests can assert calendar_sync actually ran (a PUT/DELETE), not just
    that it didn't crash."""

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


class CancelRegistrationTest(unittest.TestCase):
    """`my-bt cancel`'s underlying logic (scripts/my-bt has no .py
    extension, so it's tested here -- see app/cli_cancel.py's own
    docstring). Must behave IDENTICALLY to app/webapp.py::App.admin_cancel
    -- both call the exact same app.cancellation.send_cancellation_emails
    AND (2026-07-06) app.cancel_flow.cancel_and_promote functions, so these
    tests mirror test_webapp.py's
    AdminOverviewTest.test_admin_cancel_notifies_both_sides_with_message."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        course = make_course(shortname="yoga-class-1", title="Yoga", capacity=1)
        self.settings = make_settings(courses=(course,), booking_calendar="Bookings")

        self.sent_emails: list[tuple[str, str, str]] = []
        for target in ("app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            patcher = patch(
                target,
                side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None: self.sent_emails.append((to, subject, body)),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        # cancel_registration() now also runs app.cancel_flow.cancel_and_promote,
        # which needs a real CalDAVClient -- build_caldav_client() would
        # otherwise try a genuine HTTPS connection using make_settings()'s
        # placeholder caldav_url/credentials. Patch it to return one wired
        # to FakeTransport instead, same approach as test_webapp.py's
        # ConflictCheckerTest/BookingFlowTest.
        self.transport = FakeTransport()
        fake_client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self.transport,
        )
        patcher = patch("app.cli_cancel.build_caldav_client", return_value=fake_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _book(self, email: str, name: str, status: str = STATUS_CONFIRMED, occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration(
            "yoga-class-1", occurrence_date, user.user_id, hash_token(new_token()), status=status,
        )
        return user, reg

    # -- happy path -----------------------------------------------------

    def test_cancels_confirmed_registration_and_notifies_both_sides(self):
        user, reg = self._book("guest@example.org", "Guest")
        result = cancel_registration(self.store, self.settings, reg.registration_id, message="course canceled")

        self.assertTrue(result.ok)
        self.assertEqual(result.status_before, STATUS_CONFIRMED)
        self.assertEqual(result.course_shortname, "yoga-class-1")
        self.assertEqual(result.occurrence_date, "2026-08-01")
        self.assertEqual(result.user_email, "guest@example.org")
        self.assertTrue(result.emailed)

        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        self.assertEqual(reloaded.canceled_by, "host")
        self.assertEqual(reloaded.host_message, "course canceled")

        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("guest@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)

        participant_mail = next(b for t, s, b in self.sent_emails if t == "guest@example.org")
        self.assertIn("The host canceled this booking:", participant_mail)
        # 2026-07-09, the operator (b): host-initiated cancels label the message
        # from the ATTENDEE's point of view -- it came from the host.
        self.assertIn("Message from the host: course canceled", participant_mail)
        self.assertIn("What: Yoga", participant_mail)
        # 2026-07-09, the operator (c): no reinstate link for a host-initiated
        # cancel's participant copy -- `my-bt cancel` is always host-side.
        self.assertNotIn("/reinstate/", participant_mail)

        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org")
        # 2026-07-09, the operator (a): the admin copy must name WHO was canceled,
        # not just say "You".
        self.assertIn("You canceled Guest <guest@example.org>'s booking:", admin_mail)
        self.assertIn("Message: course canceled", admin_mail)

    def test_cancels_waitlisted_registration(self):
        user, reg = self._book("guest@example.org", "Guest", status=STATUS_WAITLISTED)
        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_before, STATUS_WAITLISTED)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

    def test_cancels_pending_confirmation_registration(self):
        # 2026-07-13, the operator: a guest who registered but hasn't yet clicked
        # their account-confirmation email link (STATUS_PENDING_CONFIRMATION)
        # previously couldn't be canceled by ANY path at all -- closing that
        # gap here (see Store.cancel()'s own docstring). This guest is still
        # emailed (send_cancellation_emails doesn't check status), and no
        # promotion happens -- a pending row never held a real spot.
        user, reg = self._book("guest@example.org", "Guest", status=STATUS_PENDING_CONFIRMATION)
        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_before, STATUS_PENDING_CONFIRMATION)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("guest@example.org", to_addrs)

    def test_cancel_email_includes_a_working_host_reinstate_link(self):
        # 2026-07-10: `my-bt cancel` mints a fresh reinstate token the same
        # way every web cancel path does, and the ADMIN copy's
        # /host-reinstate/<registration_id> link (unconditional regardless
        # of who canceled) actually undoes it. 2026-07-09, the operator (c): the
        # PARTICIPANT copy gets no reinstate link at all for a host-
        # initiated cancel like this one -- see the test right above.
        user, reg = self._book("guest@example.org", "Guest")
        cancel_registration(self.store, self.settings, reg.registration_id)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org")
        self.assertIn(f"Reinstate this booking: https://example.org/host-reinstate/{reg.registration_id}", admin_mail)
        # The reinstate_token itself is still minted/stored (harmless, just
        # unused by any email now) -- confirm it's still a real, working
        # token rather than silently dropped.
        stored = self.store.find_by_id(reg.registration_id)
        self.assertTrue(stored.guest_cancel_token_hash)
        self.assertIsNotNone(self.store.find_canceled_by_guest_token_hash(stored.guest_cancel_token_hash))

    def test_without_message_omits_message_line_and_reports_empty(self):
        user, reg = self._book("guest@example.org", "Guest")
        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertEqual(result.message, "")
        participant_mail = next(b for t, s, b in self.sent_emails if t == "guest@example.org")
        self.assertNotIn("Message:", participant_mail)

    # -- 2026-07-06: unified flow (promotion + calendar sync) --------------

    def test_cancel_promotes_next_waitlisted_person(self):
        # capacity=1: the confirmed guest holds the only spot, a second
        # guest is waitlisted -- canceling the confirmed one must promote
        # the waitlisted guest to confirmed, exactly like the web admin's
        # /admin/cancel (App._cancel_and_promote) already does.
        _confirmed_user, confirmed_reg = self._book("first@example.org", "First")
        waiter_user, waiter_reg = self._book("second@example.org", "Second", status=STATUS_WAITLISTED)

        result = cancel_registration(self.store, self.settings, confirmed_reg.registration_id)
        self.assertTrue(result.ok)

        promoted = self.store.find_by_id(waiter_reg.registration_id)
        self.assertEqual(promoted.status, STATUS_CONFIRMED)

        subjects = [s for _, s, _ in self.sent_emails]
        self.assertTrue(any(s.startswith("You're in!") for s in subjects))
        self.assertTrue(any(s.startswith("Promoted from waitlist:") for s in subjects))
        promoted_mail = next(b for t, s, b in self.sent_emails if t == "second@example.org" and s.startswith("You're in!"))
        self.assertIn("you're now confirmed", promoted_mail)

    def test_cancel_syncs_the_calendar(self):
        # A PUT (re-sync, still 0 active after this solo cancel -> actually
        # a DELETE here, since capacity=1 and this was the only confirmed
        # registrant) must reach the CalDAV client -- asserts the network
        # call actually happened, not just that cancel_registration() didn't
        # crash.
        _user, reg = self._book("guest@example.org", "Guest")
        cancel_registration(self.store, self.settings, reg.registration_id)
        methods = [m for m, _url, _body in self.transport.calls]
        self.assertIn("PROPFIND", methods)
        # Zero active registrants remain after this cancel -- sync_occurrence
        # deletes the (nonexistent, in this fake) event rather than PUTting
        # one; REPORT still runs first to check for an existing event.
        self.assertIn("REPORT", methods)

    def test_cancel_calendar_sync_puts_event_with_canceled_participant_listed(self):
        # capacity=2 so the course still has an active registrant after
        # this cancel -- sync_occurrence PUTs an updated event (not a
        # DELETE), and its description must list the just-canceled guest
        # under a separate "Canceled" section (see calendar_sync's own
        # tests for the full invite-body behavior) rather than silently
        # dropping them.
        course = make_course(shortname="yoga-class-1", title="Yoga", capacity=2)
        settings = make_settings(courses=(course,), booking_calendar="Bookings")
        self.store.upsert_user_for_booking("stays@example.org", "Stays")
        self.store.add_registration(
            "yoga-class-1", "2026-08-01", self.store.find_user_by_email("stays@example.org").user_id,
            hash_token(new_token()), status=STATUS_CONFIRMED,
        )
        _user, reg = self._book("guest@example.org", "Guest")
        cancel_registration(self.store, settings, reg.registration_id)
        put_bodies = [body for m, _url, body in self.transport.calls if m == "PUT"]
        self.assertEqual(len(put_bodies), 1)
        # ICS folds long lines (see app/ics.py), so assert on the
        # unfolded description rather than exact substrings that could
        # straddle a fold boundary.
        unfolded = put_bodies[0].replace("\r\n ", "")
        self.assertIn("Canceled:", unfolded)
        self.assertIn("canceled_by_host", unfolded)
        self.assertIn("canceled 2026-", unfolded)

    # -- error paths ------------------------------------------------------

    def test_nonexistent_registration_id_reports_clearly_no_exception(self):
        result = cancel_registration(self.store, self.settings, "no-such-id")
        self.assertFalse(result.ok)
        self.assertIn("no registration", result.reason)
        self.assertEqual(self.sent_emails, [])

    def test_already_canceled_registration_is_not_recanceled(self):
        user, reg = self._book("guest@example.org", "Guest")
        cancel_registration(self.store, self.settings, reg.registration_id)
        self.sent_emails.clear()

        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertFalse(result.ok)
        self.assertIn("not cancelable", result.reason)
        self.assertIn("canceled_by_host", result.reason)
        self.assertEqual(self.sent_emails, [])
        # Still canceled (unchanged), not double-processed.
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

    def test_course_removed_from_settings_still_cancels_but_does_not_email(self):
        # Mirrors admin_cancel()'s own "if course:" guard -- a registration
        # for a course shortname no longer in settings.toml still gets
        # canceled (the status transition doesn't depend on course config),
        # it just can't compose an email, promote, or sync a calendar
        # without a course to promote/sync against.
        user, reg = self._book("guest@example.org", "Guest")
        settings_without_course = make_settings(courses=())
        result = cancel_registration(self.store, settings_without_course, reg.registration_id)
        self.assertTrue(result.ok)
        self.assertFalse(result.emailed)
        self.assertEqual(self.sent_emails, [])
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        # No CalDAV calls either -- can't sync a course that isn't configured.
        self.assertEqual(self.transport.calls, [])


class ResolveCourseShortnameForDateTest(unittest.TestCase):
    """app.cli_cancel.resolve_course_shortname_for_date -- `my-bt cancel
    --date`'s course auto-detection (2026-07-13, the operator: "--course parameter
    should be optional")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _book(self, course_shortname: str, email: str, status: str = STATUS_CONFIRMED, occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, email.split("@")[0].title())
        self.store.add_registration(course_shortname, occurrence_date, user.user_id, hash_token(new_token()), status=status)

    def test_explicit_course_is_returned_unvalidated(self):
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01", "whatever-i-say")
        self.assertEqual(resolved, "whatever-i-say")
        self.assertEqual(candidates, [])

    def test_auto_detects_the_single_course_booked_on_that_date(self):
        self._book("yoga-class-1", "guest@example.org")
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertEqual(resolved, "yoga-class-1")
        self.assertEqual(candidates, [])

    def test_auto_detection_includes_waitlisted(self):
        self._book("yoga-class-1", "guest@example.org", status=STATUS_WAITLISTED)
        resolved, _candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertEqual(resolved, "yoga-class-1")

    def test_auto_detection_includes_pending_confirmation(self):
        self._book("yoga-class-1", "guest@example.org", status=STATUS_PENDING_CONFIRMATION)
        resolved, _candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertEqual(resolved, "yoga-class-1")

    def test_no_live_registrations_on_that_date_returns_none_with_no_candidates(self):
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertIsNone(resolved)
        self.assertEqual(candidates, [])

    def test_two_courses_on_the_same_date_is_ambiguous(self):
        self._book("yoga-class-1", "a@example.org")
        self._book("pilates-1", "b@example.org")
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertIsNone(resolved)
        self.assertEqual(candidates, ["pilates-1", "yoga-class-1"])

    def test_canceled_registrations_are_not_counted(self):
        # Only LIVE, still-cancelable statuses count -- an already-canceled
        # row for a second course on the same date shouldn't make this look
        # ambiguous.
        self._book("yoga-class-1", "a@example.org")
        self._book("pilates-1", "b@example.org", status="canceled_by_guest")
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertEqual(resolved, "yoga-class-1")

    def test_different_date_does_not_count_toward_ambiguity(self):
        self._book("yoga-class-1", "a@example.org", occurrence_date="2026-08-01")
        self._book("pilates-1", "b@example.org", occurrence_date="2026-08-08")
        resolved, candidates = resolve_course_shortname_for_date(self.store, "2026-08-01")
        self.assertEqual(resolved, "yoga-class-1")
        self.assertEqual(candidates, [])


class ClassifyCancelQueryTest(unittest.TestCase):
    """app.cli_cancel.classify_cancel_query -- 2026-07-09, the operator: "please
    make cancel also SMART like show ... the ID short or long is unique to
    recognize! a DATE as well a course name as well!" Reuses the exact same
    building blocks as app.cli_show.classify_show_query (see that module's
    own tests in test_cli_show.py for the low-level id/date-shape checks);
    these tests focus on classify_cancel_query's own narrower precedence
    (id > course > date, no user/email step) and its distinct "course"
    outcome (not itself cancelable -- needs a --date too)."""

    def setUp(self):
        self.courses = [make_course(shortname="yoga-class-1"), make_course(shortname="pilates-1")]

    def test_exact_full_id_match(self):
        ids = ["4aa6b8c1-ccf0-4af4-b492-dab5bfecf650", "other-id"]
        kind, data = classify_cancel_query(
            "4aa6b8c1-ccf0-4af4-b492-dab5bfecf650", ids, self.courses, min_id_length=6,
        )
        self.assertEqual(kind, "id")
        self.assertEqual(data, ids[0])

    def test_short_id_prefix_resolves_via_hash(self):
        ids = ["4aa6b8c1-ccf0-4af4-b492-dab5bfecf650"]
        short = assign_short_ids(ids)[ids[0]]
        kind, data = classify_cancel_query(short, ids, self.courses, min_id_length=6)
        self.assertEqual(kind, "id")
        self.assertEqual(data, ids[0])

    def test_ambiguous_short_id_prefix(self):
        # Trivial digest_fn deliberately collides both ids on the exact
        # same digest -- same technique test_cli_list.py's own
        # ResolveShortIdTest uses (real sha1 output can't be hand-crafted
        # to collide on demand). The query itself must still look
        # id-shaped (valid hex, long enough) to reach resolve_short_id at
        # all -- "aaaaaa" qualifies at min_id_length=6.
        ids = ["id-a", "id-b"]
        kind, data = classify_cancel_query(
            "aaaaaa", ids, self.courses, min_id_length=6, digest_fn=lambda s: "aaaaaa",
        )
        self.assertEqual(kind, "ambiguous_id")
        self.assertCountEqual(data, ids)

    def test_bare_course_shortname_is_reported_as_course_not_cancelable(self):
        kind, data = classify_cancel_query("yoga-class-1", [], self.courses, min_id_length=6)
        self.assertEqual(kind, "course")
        self.assertEqual(data.shortname, "yoga-class-1")

    def test_course_shortname_match_is_case_insensitive(self):
        kind, data = classify_cancel_query("YOGA-CLASS-1", [], self.courses, min_id_length=6)
        self.assertEqual(kind, "course")

    def test_date_recognized_when_no_id_or_course_matches(self):
        kind, data = classify_cancel_query("2026-08-01", [], self.courses, min_id_length=6)
        self.assertEqual(kind, "date")
        self.assertEqual(data, "2026-08-01")

    def test_course_takes_precedence_over_date_shaped_lookalike(self):
        # Not a realistic collision (course shortnames aren't YYYY-MM-DD
        # shaped in practice) but confirms the documented precedence order
        # (course checked before date) rather than leaving it implicit.
        weird_course = make_course(shortname="2026-08-01")
        kind, data = classify_cancel_query(
            "2026-08-01", [], [weird_course], min_id_length=6,
        )
        self.assertEqual(kind, "course")

    def test_unrecognized_query_returns_none(self):
        kind, data = classify_cancel_query("not-a-thing-at-all", [], self.courses, min_id_length=6)
        self.assertEqual(kind, "none")
        self.assertIsNone(data)

    def test_short_ambiguous_id_like_query_without_matches_is_none(self):
        # id-shaped (hex, long enough) but resolves to nothing -- distinct
        # from "none" only in that it at least LOOKS like an id; either
        # way nothing cancelable comes out of it.
        kind, data = classify_cancel_query("deadbeef", [], self.courses, min_id_length=6)
        self.assertEqual(kind, "none")
        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
