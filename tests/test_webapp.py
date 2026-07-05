import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from app import webapp
from app.caldav_client import CalDAVClient, Response
from app.slots import Occurrence
from app.storage import Store
from app.webapp import App

from .helpers import make_settings

PROPFIND_BODY = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/caldav/Calendar/</D:href>
    <D:propstat><D:prop><D:displayname>Calendar</D:displayname></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/caldav/YogaBookings/</D:href>
    <D:propstat><D:prop><D:displayname>Yoga-Bookings</D:displayname></D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


def _report_with_event(uid: str) -> str:
    return f"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/caldav/x.ics</D:href>
    <D:propstat><D:prop>
      <D:getetag>"e1"</D:getetag>
      <C:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:{uid}
DTSTART:20260708T171500Z
DTEND:20260708T185500Z
SUMMARY:Test
END:VEVENT
END:VCALENDAR
</C:calendar-data>
    </D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


EMPTY_REPORT = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>"""


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.propfind_response = Response(207, {}, PROPFIND_BODY)
        # per-calendar-href REPORT response, keyed by href
        self.report_responses: dict[str, Response] = {}

    def __call__(self, method, url, body="", extra_headers=None):
        self.calls.append((method, url))
        if method == "PROPFIND":
            return self.propfind_response
        if method == "REPORT":
            for href, resp in self.report_responses.items():
                if href in url:
                    return resp
            return Response(207, {}, EMPTY_REPORT)
        raise AssertionError(f"unexpected {method} {url}")


class ConflictCheckerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(conflict_calendars=("Calendar", "Yoga-Bookings"))
        self.app = App(self.settings, self.store)
        self.transport = FakeTransport()
        self.app.caldav = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self.transport,
        )

    def test_checks_every_configured_conflict_calendar_not_just_first(self):
        # "Calendar" (the first one) has nothing; "Yoga-Bookings" (the
        # second) has a real conflicting event -- this must still be caught.
        self.transport.report_responses["YogaBookings"] = Response(207, {}, _report_with_event("other-event@x"))
        check = self.app._conflict_checker(exclude_own=True)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc)
        self.assertTrue(check(start, end))

    def test_no_conflict_when_all_calendars_empty(self):
        check = self.app._conflict_checker(exclude_own=True)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc)
        self.assertFalse(check(start, end))

    def test_own_generated_events_excluded_in_every_calendar(self):
        # UID domain is derived from settings.base_url (see
        # calendar_sync._uid_parts) -- make_settings()'s default base_url
        # is https://example.org, so "our own" events look like this.
        self.transport.report_responses["YogaBookings"] = Response(
            207, {}, _report_with_event("example-org-yoga-class-1-2026-07-08@example.org")
        )
        check = self.app._conflict_checker(exclude_own=True)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc)
        self.assertFalse(check(start, end))

    def test_calendar_href_is_cached_not_refetched_per_check(self):
        check = self.app._conflict_checker(exclude_own=True)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc)
        check(start, end)
        check(start, end)
        propfind_calls = [c for c in self.transport.calls if c[0] == "PROPFIND"]
        # Two conflict calendars, but PROPFIND (which lists ALL calendars at
        # once) should only ever run once total, cached after that -- not
        # once per calendar per check.
        self.assertEqual(len(propfind_calls), 1)


class SpotsLeftDisplayTest(unittest.TestCase):
    """`spots_left_offset` (settings.toml [defaults]) is display-only, for
    A/B-testing whether perceived scarcity changes booking behaviour --
    these lock in that it can never make a genuinely-open occurrence claim
    "FULL" (what "join waitlist" promises has to stay true), never drops
    below "1 spot(s) left" while bookable-as-confirmed, and never touches
    the real capacity/waitlist decision at all (that's a separate code
    path -- Store.add_registration_checking_capacity -- not exercised or
    influenced by this display text in any way)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _app(self, **overrides) -> App:
        return App(make_settings(**overrides), self.store)

    def _occ(self, spots_taken: int, capacity: int = 10) -> Occurrence:
        d = date(2026, 7, 8)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc)
        return Occurrence("yoga-class-1", d, start, end, spots_taken, capacity)

    def test_default_offset_shows_the_real_number(self):
        app = self._app()
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "8 spot(s) left")

    def test_default_offset_shows_full_when_actually_full(self):
        app = self._app()
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "FULL, join waitlist")

    def test_positive_offset_reduces_shown_number(self):
        app = self._app(spots_left_offset=3)
        # real spots_left = 8, offset 3 -> shown 5
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "5 spot(s) left")

    def test_offset_never_shows_below_one_when_not_actually_full(self):
        app = self._app(spots_left_offset=50)
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "1 spot(s) left")

    def test_offset_never_overrides_a_genuinely_full_occurrence(self):
        # A large *negative* offset would otherwise inflate the number --
        # must still say FULL, since a booking here really is waitlisted.
        app = self._app(spots_left_offset=-50)
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "FULL, join waitlist")

    def test_negative_offset_is_clamped_to_capacity(self):
        app = self._app(spots_left_offset=-100)
        self.assertEqual(app._spots_left_text(self._occ(9, capacity=10)), "10 spot(s) left")

    def test_show_spots_left_false_hides_the_text_entirely(self):
        app = self._app(show_spots_left=False)
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "")
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "")


class LateBookingQuorumTest(unittest.TestCase):
    """min_required_participants (settings.toml [defaults], default 1)
    only ever matters for a LATE booking (within min_notice_hours of
    start): allowed normally if quorum's already met or this booking is
    the one that reaches it, rejected only if it still wouldn't be."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _app(self, **overrides) -> App:
        return App(make_settings(**overrides), self.store)

    def _occ(self, spots_taken: int, start: datetime, capacity: int = 10) -> Occurrence:
        d = date(2026, 7, 8)
        end = start + timedelta(minutes=100)
        return Occurrence("yoga-class-1", d, start, end, spots_taken, capacity)

    def test_default_min_required_participants_never_rejects(self):
        # Default is 1 -- always a no-op, even for a booking seconds before start.
        app = self._app(min_notice_hours=2)
        now = datetime(2026, 7, 8, 17, 10, tzinfo=timezone.utc)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(0, start)
        self.assertIsNone(app._late_booking_rejection(occ, now))

    def test_not_late_is_never_rejected_even_below_quorum(self):
        app = self._app(min_notice_hours=2, min_required_participants=3)
        now = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)  # well over 2h before start
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(0, start)
        self.assertIsNone(app._late_booking_rejection(occ, now))

    def test_late_booking_allowed_when_it_reaches_quorum(self):
        app = self._app(min_notice_hours=2, min_required_participants=3)
        now = datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc)  # 15 min before start, late
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(2, start)  # +1 from this booking = 3 = quorum
        self.assertIsNone(app._late_booking_rejection(occ, now))

    def test_late_booking_allowed_when_quorum_already_met(self):
        app = self._app(min_notice_hours=2, min_required_participants=3)
        now = datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(5, start)  # already well past quorum
        self.assertIsNone(app._late_booking_rejection(occ, now))

    def test_late_booking_rejected_when_still_under_quorum(self):
        app = self._app(min_notice_hours=2, min_required_participants=3)
        now = datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(0, start)  # +1 = 1, still short of 3
        msg = app._late_booking_rejection(occ, now)
        self.assertIsNotNone(msg)
        self.assertIn("3", msg)
        self.assertIn("2h", msg)

    def test_full_occurrence_is_never_rejected_regardless_of_quorum(self):
        # Already full -- this booking only joins the waitlist, which
        # can't affect whether the course itself runs.
        app = self._app(min_notice_hours=2, min_required_participants=3)
        now = datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc)
        start = datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc)
        occ = self._occ(10, start, capacity=10)
        self.assertIsNone(app._late_booking_rejection(occ, now))


class PolicyNoteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _app(self, **overrides) -> App:
        return App(make_settings(**overrides), self.store)

    def test_default_min_required_participants_shows_no_note(self):
        app = self._app()
        self.assertEqual(app._policy_note(), "")

    def test_note_shown_when_min_required_participants_above_one(self):
        app = self._app(min_required_participants=3, min_notice_hours=2)
        note = app._policy_note()
        self.assertIn("3", note)
        self.assertIn("2h", note)


class AdminLoginRateLimitTest(unittest.TestCase):
    """Regression coverage for the 2026-07-05 fix: login_limiter used to be
    keyed by a single global "admin" string, so anyone, unauthenticated,
    could lock the real admin out of /admin/login for up to an hour with 5
    wrong guesses from any IP. It's now keyed per client IP (via
    webapp._client_ip(), which trusts X-Forwarded-For -- see
    nginx/my-booking.conf) -- see the maintainer's local notes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.app = App(make_settings(), self.store)
        # login_limiter (app/webapp.py) is a module-level singleton shared
        # across every test in the process -- reset just the keys this
        # class touches, both before and after, so this test can't leak
        # state into (or be polluted by) any other test.
        self._keys = [f"admin:203.0.113.{i}" for i in range(1, 5)]
        for k in self._keys:
            webapp.login_limiter.reset(k)
        self.addCleanup(lambda: [webapp.login_limiter.reset(k) for k in self._keys])

    def _post(self, password: str, *, forwarded_for: str | None = None, remote_addr: str | None = None):
        body = f"password={password}".encode()
        environ = {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if forwarded_for is not None:
            environ["HTTP_X_FORWARDED_FOR"] = forwarded_for
        if remote_addr is not None:
            environ["REMOTE_ADDR"] = remote_addr
        _status, _headers, resp_body = self.app.admin_login("POST", environ)
        return resp_body

    def test_five_failures_from_one_ip_then_that_ip_is_locked(self):
        ip = "203.0.113.1"
        for _ in range(5):
            self.assertIn("Wrong password", self._post("wrong", forwarded_for=ip))
        self.assertIn("Too many attempts", self._post("wrong", forwarded_for=ip))

    def test_a_different_ip_is_unaffected_by_another_ips_lockout(self):
        attacker_ip, admin_ip = "203.0.113.2", "203.0.113.3"
        for _ in range(5):
            self._post("wrong", forwarded_for=attacker_ip)
        self.assertIn("Too many attempts", self._post("wrong", forwarded_for=attacker_ip))
        # The real admin, from a different IP, must NOT be locked out just
        # because someone else exhausted their own budget.
        body = self._post("wrong", forwarded_for=admin_ip)
        self.assertIn("Wrong password", body)
        self.assertNotIn("Too many attempts", body)

    def test_falls_back_to_remote_addr_without_x_forwarded_for(self):
        # No nginx in front (e.g. local dev) -- must not crash, and must
        # still rate-limit using REMOTE_ADDR.
        ip = "203.0.113.4"
        for _ in range(5):
            self.assertIn("Wrong password", self._post("wrong", remote_addr=ip))
        self.assertIn("Too many attempts", self._post("wrong", remote_addr=ip))


if __name__ == "__main__":
    unittest.main()
