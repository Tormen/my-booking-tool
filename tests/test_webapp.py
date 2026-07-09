import dataclasses
import io
import json
import re
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from http import cookies
from unittest.mock import patch
from urllib.parse import urlencode

from app import maintenance, webapp
from app.caldav_client import CalDAVClient, Response
from app.erasure import erase_user_by_email
from app.security import hash_admin_password, hash_secret, hash_token, new_token
from app.slots import Occurrence, build_occurrences
from app.storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION,
    STATUS_WAITLISTED, Store,
)
from app.webapp import App

from .helpers import make_course, make_settings

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
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "8 spots left")

    def test_default_offset_shows_full_when_actually_full(self):
        app = self._app()
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "FULL, join waitlist")

    def test_positive_offset_reduces_shown_number(self):
        app = self._app(spots_left_offset=3)
        # real spots_left = 8, offset 3 -> shown 5
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "5 spots left")

    def test_offset_never_shows_below_one_when_not_actually_full(self):
        app = self._app(spots_left_offset=50)
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "1 spot left")

    def test_offset_never_overrides_a_genuinely_full_occurrence(self):
        # A large *negative* offset would otherwise inflate the number --
        # must still say FULL, since a booking here really is waitlisted.
        app = self._app(spots_left_offset=-50)
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "FULL, join waitlist")

    def test_negative_offset_is_clamped_to_capacity(self):
        app = self._app(spots_left_offset=-100)
        self.assertEqual(app._spots_left_text(self._occ(9, capacity=10)), "10 spots left")

    def test_singular_spot_when_exactly_one_left(self):
        app = self._app()
        self.assertEqual(app._spots_left_text(self._occ(9, capacity=10)), "1 spot left")

    def test_show_spots_left_false_hides_the_text_entirely(self):
        app = self._app(show_spots_left=False)
        self.assertEqual(app._spots_left_text(self._occ(2, capacity=10)), "")
        self.assertEqual(app._spots_left_text(self._occ(10, capacity=10)), "")


class BookPageTest(unittest.TestCase):
    """`_book_page`'s subtitle/description/button-label behaviour. Full
    HTML structure isn't asserted line-by-line (that's what the rendered
    mockup is for) -- these lock in the actual content-selection logic:
    what shows up, what doesn't, and what's escaped vs. left as raw HTML."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _app(self, **overrides) -> App:
        return App(make_settings(**overrides), self.store)

    def _occ(self, d=date(2026, 7, 11), spots_taken=1, capacity=10) -> Occurrence:
        start = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 11, 11, 15, tzinfo=timezone.utc)
        return Occurrence("trier-sat-yoga", d, start, end, spots_taken, capacity)

    def test_subtitle_defaults_to_weekday_time_range_and_location(self):
        app = self._app()
        course = make_course(weekday="sat", location="Trier")  # start_time=17:15, duration=100 (helpers.py defaults)
        _, _, html = app._book_page(course, [self._occ()])
        self.assertIn('<p class="subtitle">Saturdays 17h15 - 18h55 -- Trier</p>', html)

    def test_subtitle_default_pads_on_the_hour_minutes(self):
        app = self._app()
        course = make_course(weekday="mon", location="Gym", start_time="9:00", duration_minutes=60)
        _, _, html = app._book_page(course, [self._occ()])
        self.assertIn('<p class="subtitle">Mondays 9h00 - 10h00 -- Gym</p>', html)

    def test_subtitle_empty_string_suppresses_it(self):
        app = self._app()
        course = make_course(subtitle="")
        _, _, html = app._book_page(course, [self._occ()])
        self.assertNotIn('class="subtitle"', html)

    def test_subtitle_custom_override_is_escaped_plain_text(self):
        app = self._app()
        course = make_course(subtitle="Wednesdays <b>only</b>")
        _, _, html = app._book_page(course, [self._occ()])
        self.assertIn('<p class="subtitle">Wednesdays &lt;b&gt;only&lt;/b&gt;</p>', html)

    def test_description_is_rendered_as_raw_html_not_escaped(self):
        app = self._app()
        course = make_course(description="<b>Rich</b> <i>text</i>")
        _, _, html = app._book_page(course, [self._occ()])
        self.assertIn('<div class="description"><b>Rich</b> <i>text</i></div>', html)

    def test_no_description_omits_the_box_entirely(self):
        app = self._app()
        course = make_course(description="")
        _, _, html = app._book_page(course, [self._occ()])
        self.assertNotIn('class="description"', html)

    def test_book_button_label_defaults_to_book(self):
        app = self._app()
        course = make_course()
        _, _, html = app._book_page(course, [self._occ(spots_taken=1, capacity=10)])
        self.assertIn(">Book</button>", html)
        self.assertIn('data-book-label="Book"', html)

    def test_book_button_label_is_configurable(self):
        app = self._app(book_button_label="Reserve my spot")
        course = make_course()
        _, _, html = app._book_page(course, [self._occ(spots_taken=1, capacity=10)])
        self.assertIn(">Reserve my spot</button>", html)
        self.assertIn('data-book-label="Reserve my spot"', html)

    def test_full_occurrence_always_says_join_waitlist_regardless_of_label(self):
        app = self._app(book_button_label="Reserve my spot")
        course = make_course()
        _, _, html = app._book_page(course, [self._occ(spots_taken=10, capacity=10)])
        self.assertIn(">Join waitlist</button>", html)

    def test_spots_left_text_is_split_onto_its_own_span(self):
        app = self._app()
        course = make_course()
        _, _, html = app._book_page(course, [self._occ(spots_taken=1, capacity=10)])
        self.assertIn('<span class="d-date">2026-07-11</span>', html)
        self.assertIn('<span class="d-spots">9 spots left</span>', html)


class CoursesPageTest(unittest.TestCase):
    """/courses (2026-07-06): the "overview page as simplymeet.me" that
    lists every configured course, each linking to /book/<shortname> --
    the destination for /my's "New booking" button."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def test_lists_every_configured_course_linking_to_its_book_page(self):
        c1 = make_course(shortname="yoga-wed", title="Vinyasa Yoga")
        c2 = make_course(shortname="mindfulness-thu", title="Mindfulness Session")
        app = App(make_settings(courses=(c1, c2)), self.store)
        _status, _headers, body = app.courses("GET", {})
        self.assertIn("Vinyasa Yoga", body)
        self.assertIn('<a href="/book/yoga-wed">', body)
        self.assertIn("Mindfulness Session", body)
        self.assertIn('<a href="/book/mindfulness-thu">', body)

    def test_shows_both_private_and_public_audience_courses_unfiltered(self):
        # audience is documented as display-only, no access-control
        # difference (settings.toml.example) -- /courses must not filter
        # by it.
        private = make_course(shortname="private-one", audience="private")
        public = make_course(shortname="public-one", audience="public")
        app = App(make_settings(courses=(private, public)), self.store)
        _status, _headers, body = app.courses("GET", {})
        self.assertIn("/book/private-one", body)
        self.assertIn("/book/public-one", body)

    def test_no_courses_configured_shows_a_friendly_message(self):
        app = App(make_settings(courses=()), self.store)
        _status, _headers, body = app.courses("GET", {})
        self.assertIn("No courses are configured yet.", body)

    def test_description_rendered_as_raw_html_same_as_book_page(self):
        course = make_course(description="<b>Rich</b> text")
        app = App(make_settings(courses=(course,)), self.store)
        _status, _headers, body = app.courses("GET", {})
        self.assertIn('<div class="description"><b>Rich</b> text</div>', body)


class MaintenanceModeTest(unittest.TestCase):
    """`my-bt admin site-maintenance on` (see app/maintenance.py + scripts/my-bt) blocks
    every GUEST-facing route via a data-dir flag file checked fresh on
    every request (app.webapp.App._maintenance_guard). Originally scoped
    to only /courses and /book/<shortname> (2026-07-10: the operator asked to gate
    "any booking URL (like the links on index.html)"), widened the same
    day after the operator caught, via a real external-IP test, that /my's login
    page still worked completely normally: "This should not be!" -- see
    MaintenanceScopeTest below for the full route-by-route coverage of the
    corrected scope."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def test_courses_page_shows_maintenance_message_when_enabled(self):
        maintenance.enable(self.store.data_dir, message="Back Monday")
        app = App(make_settings(admin_email="host@example.org"), self.store)
        status, _headers, body = app.courses("GET", {})
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn("down for maintenance", body)
        self.assertIn("Back Monday", body)
        self.assertIn('<a href="mailto:host@example.org">', body)
        self.assertIn("Teams", body)

    def test_courses_page_is_normal_when_disabled(self):
        app = App(make_settings(courses=(make_course(),)), self.store)
        status, _headers, _body = app.courses("GET", {})
        self.assertEqual(status, "200 OK")

    def test_book_page_shows_maintenance_message_when_enabled(self):
        maintenance.enable(self.store.data_dir, message="")
        course = make_course(shortname="trier-sat-yoga")
        app = App(make_settings(courses=(course,), admin_email="host@example.org"), self.store)
        status, _headers, body = app.book("GET", "trier-sat-yoga", {})
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn("down for maintenance", body)

    def test_book_page_maintenance_check_happens_before_course_lookup(self):
        # Even an UNKNOWN shortname must show the maintenance message, not
        # the normal 404 -- the guard is deliberately the very first thing
        # book() does, before it even looks at settings.courses.
        maintenance.enable(self.store.data_dir, message="")
        app = App(make_settings(courses=()), self.store)
        status, _headers, body = app.book("GET", "no-such-course", {})
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn("down for maintenance", body)

    def test_book_page_post_is_also_blocked(self):
        maintenance.enable(self.store.data_dir, message="")
        course = make_course(shortname="trier-sat-yoga")
        app = App(make_settings(courses=(course,)), self.store)
        status, _headers, _body = app.book("POST", "trier-sat-yoga", {"CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")})
        self.assertEqual(status, "503 Service Unavailable")

    def test_my_page_is_blocked_by_maintenance_mode(self):
        # 2026-07-10, the operator, after testing this himself from an external
        # IP: "I was able to click on login and see the normal login page
        # ... This should not be!" -- /my used to be deliberately exempt;
        # now it's gated exactly like /courses and /book (see
        # _maintenance_guard's docstring for the corrected scope).
        maintenance.enable(self.store.data_dir, message="down")
        app = App(make_settings(), self.store)
        status, _headers, body = app.my("GET", {})
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn("down for maintenance", body)

    def test_maintenance_response_links_back_to_the_homepage(self):
        # 2026-07-10, the operator: "the maintenance page should have a back link
        # or button." 2026-07-14: now the same boxed _session_banner_html()
        # banner every other guest-facing page uses, not a one-off "Back
        # to {site}" text link -- see _maintenance_response()'s docstring.
        maintenance.enable(self.store.data_dir, message="down")
        app = App(make_settings(), self.store)
        _status, _headers, body = app.courses("GET", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn(f'<a href="{app.settings.base_url}">example.org</a>', body)


class MaintenanceScopeTest(unittest.TestCase):
    """2026-07-10: full route-by-route coverage of the corrected maintenance
    scope (see _maintenance_guard's own docstring). Every one of these
    calls uses deliberately-garbage input (bogus token/registration_id) --
    same trick test_book_page_maintenance_check_happens_before_course_lookup
    already established -- to prove the guard fires as the very FIRST
    thing each handler does, before any real lookup, without needing a
    fully realistic booking/session fixture per route."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        maintenance.enable(self.store.data_dir, message="down")
        self.app = App(make_settings(), self.store)

    def _blocked(self, fn, *args):
        status, _headers, _body = fn(*args)
        self.assertEqual(status, "503 Service Unavailable", f"{fn.__name__} was not blocked")

    def test_guest_cancel_is_blocked(self):
        self._blocked(self.app.guest_cancel, "GET", "bogus-token", {})

    def test_guest_reinstate_is_blocked(self):
        self._blocked(self.app.guest_reinstate, "GET", "bogus-token", {})

    def test_my_signup_is_blocked(self):
        self._blocked(self.app.my_signup, "POST", {})

    def test_my_reset_is_blocked(self):
        self._blocked(self.app.my_reset, "GET", {})

    def test_my_confirm_is_blocked(self):
        self._blocked(self.app.my_confirm, "GET", "bogus-token", {})

    def test_my_cancel_is_blocked(self):
        self._blocked(self.app.my_cancel, "POST", "bogus-reg-id", {})

    def test_my_reinstate_is_blocked(self):
        self._blocked(self.app.my_reinstate, "POST", "bogus-reg-id", {})

    def test_my_delete_account_is_blocked(self):
        self._blocked(self.app.my_delete_account, "POST", {})

    def test_my_settings_is_blocked(self):
        self._blocked(self.app.my_settings, "GET", {})

    def test_my_settings_name_is_blocked(self):
        self._blocked(self.app.my_settings_name, "POST", {})

    def test_my_settings_email_is_blocked(self):
        self._blocked(self.app.my_settings_email, "POST", {})

    def test_my_settings_email_cancel_is_blocked(self):
        self._blocked(self.app.my_settings_email_cancel, "POST", {})

    def test_my_confirm_email_is_blocked(self):
        self._blocked(self.app.my_confirm_email, "GET", "bogus-token", {})

    def test_my_cancel_email_change_is_blocked(self):
        self._blocked(self.app.my_cancel_email_change, "GET", "bogus-token", {})

    # -- deliberately NOT gated: the host's own tools, and inert endpoints --

    def test_admin_login_page_is_unaffected(self):
        status, _headers, _body = self.app.admin_login("GET", {})
        self.assertNotEqual(status, "503 Service Unavailable")

    def test_host_cancel_is_unaffected(self):
        status, _headers, _body = self.app.host_cancel("GET", "bogus-reg-id", {})
        self.assertNotEqual(status, "503 Service Unavailable")
        self.assertEqual(status, "404 Not Found")

    def test_host_reinstate_is_unaffected(self):
        status, _headers, _body = self.app.host_reinstate("GET", "bogus-reg-id", {})
        self.assertNotEqual(status, "503 Service Unavailable")
        self.assertEqual(status, "404 Not Found")

    def test_my_logout_is_unaffected(self):
        # Logging out isn't a booking/management action -- blocking it would
        # only leave a guest stuck "logged in" against their wishes.
        status, _headers, _body = self.app.my_logout("POST", {})
        self.assertNotEqual(status, "503 Service Unavailable")

    def test_my_session_status_is_unaffected(self):
        # Read-only JSON status check the static homepage's own JS polls --
        # gating it would just break that JS's JSON parsing for no benefit.
        status, _headers, body = self.app.my_session_status("GET", {})
        self.assertNotEqual(status, "503 Service Unavailable")
        json.loads(body)  # still valid JSON, not an HTML maintenance page


class MaintenanceBypassTest(unittest.TestCase):
    """2026-07-10: "can the maintenance mode still let me access the site
    from ssh.example.net please?" -- [site].maintenance_bypass_hostname,
    resolved fresh per request (app.webapp._maintenance_bypass_allowed),
    lets a request whose (nginx-forwarded) client IP matches that
    hostname's CURRENT address through /courses and /book/<shortname> as
    normal even while maintenance mode blocks everyone else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        maintenance.enable(self.store.data_dir, message="down")

    def _environ(self, ip: str) -> dict:
        return {"HTTP_X_FORWARDED_FOR": ip}

    def test_matching_ip_bypasses_courses(self):
        app = App(make_settings(maintenance_bypass_hostname="ssh.example.net"), self.store)
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            status, _headers, _body = app.courses("GET", self._environ("1.2.3.4"))
        self.assertEqual(status, "200 OK")

    def test_matching_ip_bypasses_book(self):
        # Unknown shortname (same trick test_book_page_maintenance_check_
        # happens_before_course_lookup uses above) -- proves the bypass took
        # effect via the ordinary 404 instead of 503, without needing a real
        # course/CalDAV round trip just to prove the maintenance gate itself
        # was skipped.
        app = App(make_settings(courses=(), maintenance_bypass_hostname="ssh.example.net"), self.store)
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            status, _headers, _body = app.book("GET", "no-such-course", self._environ("1.2.3.4"))
        self.assertEqual(status, "404 Not Found")

    def test_non_matching_ip_still_blocked(self):
        app = App(make_settings(maintenance_bypass_hostname="ssh.example.net"), self.store)
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            status, _headers, _body = app.courses("GET", self._environ("9.9.9.9"))
        self.assertEqual(status, "503 Service Unavailable")

    def test_no_hostname_configured_never_bypasses(self):
        app = App(make_settings(), self.store)
        status, _headers, _body = app.courses("GET", self._environ("1.2.3.4"))
        self.assertEqual(status, "503 Service Unavailable")

    def test_dns_failure_fails_closed_still_blocked(self):
        import socket as socket_module
        app = App(make_settings(maintenance_bypass_hostname="ssh.example.net"), self.store)
        with patch("app.webapp.socket.getaddrinfo", side_effect=socket_module.gaierror("no such host")):
            status, _headers, _body = app.courses("GET", self._environ("1.2.3.4"))
        self.assertEqual(status, "503 Service Unavailable")

    def test_bypass_ip_still_gets_normal_maintenance_check_when_off(self):
        # sanity: with maintenance OFF entirely, behaves as if the hostname
        # setting didn't matter at all (no accidental interaction).
        maintenance.disable(self.store.data_dir)
        app = App(make_settings(maintenance_bypass_hostname="ssh.example.net"), self.store)
        status, _headers, _body = app.courses("GET", self._environ("9.9.9.9"))
        self.assertEqual(status, "200 OK")

    def test_ip_log_alone_bypasses_even_with_dns_unresolvable(self):
        # 2026-07-10, the operator: "if you need an IP this changes and the latest
        # can be found in /home/me/my-ip.log, but else the DNS also
        # auto-updates! Please make this a config variable." -- either
        # source is independently sufficient; DNS failing shouldn't take
        # the log-file source down with it.
        import socket as socket_module
        ip_log = str(self.store.data_dir) + "/my-ip.log"
        with open(ip_log, "w", encoding="utf-8") as f:
            f.write("5.6.7.8\n")
        app = App(make_settings(
            maintenance_bypass_hostname="ssh.example.net",
            maintenance_bypass_ip_log=ip_log,
        ), self.store)
        with patch("app.webapp.socket.getaddrinfo", side_effect=socket_module.gaierror("no such host")):
            status, _headers, _body = app.courses("GET", self._environ("5.6.7.8"))
        self.assertEqual(status, "200 OK")

    def test_ip_log_only_last_line_counts(self):
        ip_log = str(self.store.data_dir) + "/my-ip.log"
        with open(ip_log, "w", encoding="utf-8") as f:
            f.write("1.1.1.1\n5.6.7.8\n")
        app = App(make_settings(maintenance_bypass_ip_log=ip_log), self.store)
        status, _headers, _body = app.courses("GET", self._environ("1.1.1.1"))
        self.assertEqual(status, "503 Service Unavailable")


class MaintenanceBypassAllowedUnitTest(unittest.TestCase):
    """Direct unit tests of app.webapp._maintenance_bypass_allowed(), the
    pure function powering MaintenanceBypassTest above."""

    def test_no_hostname_returns_false(self):
        self.assertFalse(webapp._maintenance_bypass_allowed("1.2.3.4", None))
        self.assertFalse(webapp._maintenance_bypass_allowed("1.2.3.4", ""))

    def test_matching_resolved_ip_returns_true(self):
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            self.assertTrue(webapp._maintenance_bypass_allowed("1.2.3.4", "ssh.example.net"))

    def test_non_matching_resolved_ip_returns_false(self):
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            self.assertFalse(webapp._maintenance_bypass_allowed("9.9.9.9", "ssh.example.net"))

    def test_gaierror_returns_false(self):
        import socket as socket_module
        with patch("app.webapp.socket.getaddrinfo", side_effect=socket_module.gaierror("no such host")):
            self.assertFalse(webapp._maintenance_bypass_allowed("1.2.3.4", "ssh.example.net"))

    def test_generic_os_error_returns_false(self):
        with patch("app.webapp.socket.getaddrinfo", side_effect=OSError("network unreachable")):
            self.assertFalse(webapp._maintenance_bypass_allowed("1.2.3.4", "ssh.example.net"))

    def test_multiple_resolved_addresses_any_can_match(self):
        with patch("app.webapp.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("1.2.3.4", 0)),
            (None, None, None, None, ("::1", 0)),
        ]):
            self.assertTrue(webapp._maintenance_bypass_allowed("::1", "ssh.example.net"))

    def test_ip_log_source_matches_independently_of_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = tmp + "/my-ip.log"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("5.6.7.8\n")
            self.assertTrue(webapp._maintenance_bypass_allowed("5.6.7.8", None, log_path))

    def test_ip_log_source_survives_hostname_dns_failure(self):
        import socket as socket_module
        with tempfile.TemporaryDirectory() as tmp:
            log_path = tmp + "/my-ip.log"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("5.6.7.8\n")
            with patch("app.webapp.socket.getaddrinfo", side_effect=socket_module.gaierror("no such host")):
                self.assertTrue(webapp._maintenance_bypass_allowed("5.6.7.8", "ssh.example.net", log_path))

    def test_neither_source_configured_returns_false(self):
        self.assertFalse(webapp._maintenance_bypass_allowed("5.6.7.8", None, None))

    def test_both_sources_configured_neither_matching_returns_false(self):
        with patch("app.webapp.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 0))]):
            self.assertFalse(webapp._maintenance_bypass_allowed("9.9.9.9", "ssh.example.net", "/nonexistent/my-ip.log"))


class LatestLoggedIpTest(unittest.TestCase):
    """Direct unit tests of app.webapp._latest_logged_ip()."""

    def test_no_path_returns_none(self):
        self.assertIsNone(webapp._latest_logged_ip(None))
        self.assertIsNone(webapp._latest_logged_ip(""))

    def test_missing_file_returns_none(self):
        self.assertIsNone(webapp._latest_logged_ip("/no/such/file-my-ip.log"))

    def test_returns_last_non_empty_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/my-ip.log"
            with open(path, "w", encoding="utf-8") as f:
                f.write("1.1.1.1\n2.2.2.2\n\n5.6.7.8\n")
            self.assertEqual(webapp._latest_logged_ip(path), "5.6.7.8")

    def test_empty_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/my-ip.log"
            open(path, "w", encoding="utf-8").close()
            self.assertIsNone(webapp._latest_logged_ip(path))

    def test_strips_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/my-ip.log"
            with open(path, "w", encoding="utf-8") as f:
                f.write("5.6.7.8  \n")
            self.assertEqual(webapp._latest_logged_ip(path), "5.6.7.8")


class SessionBannerTest(unittest.TestCase):
    """2026-07-06: "/my should have a 'new booking' button... but with a
    banner showing them that they are logged in and with the ability to
    logout. same for any booking done from within /my." -- /courses and
    /book (form + result pages) show a small banner when reached with an
    active guest session, and show nothing at all otherwise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-class-1", weekday="wed", capacity=10)
        self.settings = make_settings(courses=(self.course,), conflict_calendars=("Calendar", "Yoga-Bookings"))
        self.app = App(self.settings, self.store)
        self.app.caldav = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=FakeTransport(),
        )
        self.app._sync = lambda *a, **kw: None
        for target in ("app.webapp.send_mail", "app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            patcher = patch(target, side_effect=lambda *a, **kw: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _login_environ(self, email: str = "regular@example.org") -> dict:
        user = self.store.upsert_user_for_booking(email, "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        sid = webapp._new_session({"kind": "guest", "user_id": user.user_id})
        return {"HTTP_COOKIE": f"session={sid}"}

    def _occ_date(self) -> str:
        occs = build_occurrences(
            self.course, self.settings, datetime.now(timezone.utc), lambda sn, d: 0, lambda start, end: False,
        )
        return occs[0].date.isoformat()

    def test_courses_shows_banner_when_logged_in(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.courses("GET", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("regular@example.org", body)
        self.assertIn('action="/my/logout"', body)
        # Unlike /my's own banner (on_my_page=True), /courses is a
        # different page from /my, so the "My bookings" link is a genuine
        # shortcut here and must stay.
        self.assertIn(">My bookings<", body)

    def test_courses_shows_login_banner_when_anonymous(self):
        # 2026-07-09, the operator: "Make it so that the top-bar is ALWAYS visible
        # (except for index.html) either with LOGIN or with the BAR." --
        # used to render no banner at all when anonymous; now shows a
        # "Login" link in the same box instead of nothing.
        _status, _headers, body = self.app.courses("GET", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)
        # 2026-07-11, the operator: "Login link returns to originating page" --
        # the link now carries ?next= back to this same page.
        self.assertIn('<a href="/my?next=/courses">Login</a>', body)

    def test_book_form_shows_banner_when_logged_in(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("regular@example.org", body)

    def test_book_form_shows_login_banner_when_anonymous(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)
        # 2026-07-11, the operator: "Login link returns to originating page" --
        # the link now carries ?next= back to this same course's page.
        self.assertIn('<a href="/my?next=/book/yoga-class-1">Login</a>', body)

    def test_stale_session_pointing_at_a_deleted_user_shows_login_banner(self):
        # A session cookie can outlive the account it points to (deleted
        # via /my/delete-account, or erased) -- must fall back to the
        # anonymous banner, not crash or show a blank top-bar.
        sid = webapp._new_session({"kind": "guest", "user_id": "no-such-user-id"})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        _status, _headers, body = self.app.courses("GET", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)

    def test_my_page_anonymous_view_has_no_redundant_login_banner(self):
        # Deliberately NOT given the full anonymous "Not logged in /
        # Login" banner -- this page already IS the Login/Sign up form
        # (_my_login_page()), so a "Login" link above it would be
        # redundant. 2026-07-14: it DOES now get the same boxed
        # .session-banner style (via _homepage_only_banner_html()), just
        # without that redundant "Not logged in"/"Login" text -- see
        # _my_login_page()'s own docstring.
        _status, _headers, body = self.app.my("GET", {})
        banner = body[body.index('<div class="session-banner">') : body.index("</div>") + len("</div>")]
        self.assertNotIn("Not logged in", banner)
        self.assertNotIn("Login", banner)  # the tab label below says "Login" -- that's fine, this banner shouldn't
        self.assertIn('id="my-tab-login"', body)

    def test_my_page_anonymous_view_still_links_back_to_the_homepage(self):
        # 2026-07-10, the operator (screenshot of /my's login page): "we miss a
        # back to https://booking.example.org here". 2026-07-14: "Reuse same boxed
        # banner is good" -- now the boxed banner's own homepage link
        # (see the test above), not a bare "Back to {site}" <p> link.
        _status, _headers, body = self.app.my("GET", {})
        self.assertIn(f'<a href="{self.settings.base_url}">example.org</a>', body)

    def test_page_width_matches_the_homepage_photos_container(self):
        # 2026-07-11, the operator (screenshot comparing the static homepage's own
        # photo-backed content column against the much narrower app pages):
        # "Widen homepage table layout to match photo width" -- clarified
        # to mean every application page. site/index.html's own
        # div.WordSection1 (the container its background photo fills) is
        # max-width:1000px -- every dynamic page here now matches that,
        # and tables stretch to fill it (a bare <table> has no width of
        # its own otherwise, so widening the body alone wouldn't actually
        # have widened /my's bookings table or /admin's overview table).
        _status, _headers, body = self.app.courses("GET", {})
        self.assertIn("max-width:1000px", body)
        self.assertIn("table{border-collapse:collapse;width:100%}", body)

    def test_no_text_renders_smaller_than_a_button_label(self):
        # 2026-07-11, the operator: "nothing smaller than the current font-size
        # of your button labels" -- button/input/textarea are the app's
        # own 1em baseline (see below); no other rule may declare a
        # font-size below that. A handful of previously-smaller elements
        # (.session-banner/.note/.hint/.date-btn .d-date/.date-btn
        # .d-spots/.hash-cell) now read as secondary/de-emphasized via
        # font-style:italic instead of a smaller size ("making the smaller
        # fonts italic instead -- as I had suggested to you before!").
        # .sort-indicator is DELIBERATELY excluded from this list (2026-07-08,
        # the operator: "is it just me or does this arrow up look distorted?") --
        # italic synthetically shears the up/down-triangle glyph, which has
        # no real italic form in most fonts, making it look skewed/broken
        # rather than merely de-emphasized. It stays at the 1em baseline
        # (never was smaller) with font-style:normal instead.
        _status, _headers, body = self.app.courses("GET", {})
        style = body[body.index("<style>") : body.index("</style>")]
        self.assertIn("input,button,textarea{font-size:1em", style)
        for match in re.finditer(r"font-size:\s*(\.\d+)em", style):
            self.fail(f"found a font-size below 1em: {match.group(0)!r}")
        for selector in (
            ".session-banner", ".note", ".hint", ".date-btn .d-date",
            ".date-btn .d-spots", ".hash-cell", ".th-note",
        ):
            rule = style[style.index(selector + "{") :]
            rule = rule[: rule.index("}")]
            self.assertIn("font-style:italic", rule, f"{selector} should be italic, not smaller")

    def test_sort_indicator_is_not_italic(self):
        # 2026-07-08, the operator (screenshot of /admin's Date column arrow):
        # "is it just me or does this arrow up look distorted?" -- yes:
        # font-style:italic was shearing the ▲/▼ glyph. See the previous
        # test's own comment for the full story.
        _status, _headers, body = self.app.courses("GET", {})
        style = body[body.index("<style>") : body.index("</style>")]
        rule = style[style.index(".sort-indicator{") :]
        rule = rule[: rule.index("}")]
        self.assertIn("font-style:normal", rule)

    # -- 2026-07-09: booking-page name/email hidden (not just locked) when logged in --

    def test_book_page_hides_name_email_fields_when_logged_in(self):
        # 2026-07-09, the operator, on the earlier prefilled+readonly version:
        # "This is confusing: If you are logged in ad you book, please
        # hide Your name + Your email fields (instead of showing them
        # prefilled)." -- the session banner already says who they're
        # booking as, so no visible "Your name"/"Your email" label+field
        # at all now, just hidden inputs carrying the same values.
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn('<input type="hidden" name="name" value="Regular">', body)
        self.assertIn('<input type="hidden" name="email" value="regular@example.org">', body)
        self.assertNotIn("Your name", body)
        self.assertNotIn("Your email", body)
        # Irrelevant once already logged in with a password.
        self.assertNotIn("First time booking with this email?", body)

    def test_book_page_fields_stay_editable_when_anonymous(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn('<input class="big-input id-input" name="name" required>', body)
        self.assertIn('<input class="big-input id-input" name="email" type="email" required>', body)
        self.assertIn("Your name", body)
        self.assertIn("Your email", body)
        # Only the CSS selector "input[readonly]" (always present in the
        # <style> block) should mention "readonly" here -- no actual input
        # tag should have the attribute for an anonymous visitor.
        self.assertNotIn('name="name" value=', body)
        self.assertNotIn('name="email" type="email" value=', body)
        self.assertIn("First time booking with this email?", body)

    def test_book_page_error_retry_keeps_fields_hidden_when_logged_in(self):
        # the operator's fields must stay hidden (not reappear editable) even on
        # a re-render after a validation error -- not just the fresh GET.
        environ = self._login_environ("regular@example.org")
        form = {"occurrence_date": self._occ_date(), "name": "Regular", "email": "regular@example.org"}  # no agree
        body_bytes = urlencode(form).encode()
        post_environ = dict(environ, CONTENT_LENGTH=str(len(body_bytes)), **{"wsgi.input": io.BytesIO(body_bytes)})
        _status, _headers, body = self.app.book("POST", "yoga-class-1", post_environ)
        self.assertIn("acknowledge the participation terms", body)
        self.assertIn('<input type="hidden" name="name" value="Regular">', body)
        self.assertIn('<input type="hidden" name="email" value="regular@example.org">', body)

    def test_logged_in_booking_ignores_submitted_email_and_uses_session_identity(self):
        # readonly is client-side only -- the server must not trust a
        # tampered submission over the session's own identity.
        environ = self._login_environ("regular@example.org")
        form = {
            "occurrence_date": self._occ_date(), "name": "Someone Else", "email": "someone-else@example.org",
            "agree": "on",
        }
        body_bytes = urlencode(form).encode()
        post_environ = dict(environ, CONTENT_LENGTH=str(len(body_bytes)), **{"wsgi.input": io.BytesIO(body_bytes)})
        _status, _headers, _body = self.app.book("POST", "yoga-class-1", post_environ)
        self.assertIsNone(self.store.find_user_by_email("someone-else@example.org"))
        regs = self.store.all_registrations()
        self.assertEqual(len(regs), 1)
        booked_user = self.store.find_user_by_id(regs[0].user_id)
        self.assertEqual(booked_user.email, "regular@example.org")
        self.assertEqual(booked_user.name, "Regular")

    # -- 2026-07-09: already-booked future dates listed alongside pickable ones --

    def _occ_dates(self, n: int) -> list[str]:
        occs = build_occurrences(
            self.course, self.settings, datetime.now(timezone.utc), lambda sn, d: 0, lambda start, end: False,
        )
        return [o.date.isoformat() for o in occs[:n]]

    def test_confirmed_future_booking_shown_as_booked_badge(self):
        # the operator, on a screenshot of /my showing 2 future confirmed
        # bookings the date-picker never mentioned: "It could be nice
        # here, to show the user that he/she already booked the classes
        # ... Like here: I have 2 future bookings already booked, they
        # should be listed." Answers to follow-up questions: FUTURE only,
        # waitlisted labeled "On waitinglist", not clickable.
        first_date, second_date = self._occ_dates(2)
        environ = self._login_environ("regular@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        self.store.add_registration("yoga-class-1", first_date, user.user_id, hash_token(new_token()))
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        # Merged into the same "Dates available" row as an inert badge --
        # not a radio input at all.
        self.assertIn(f'<span class="date-btn date-badge"><span><span class="d-date">{first_date}</span>'
                      '<span class="ribbon">Booked</span></span></span>', body)
        self.assertNotIn(f'value="{first_date}"', body)
        # The still-bookable second date stays a real, selectable option.
        self.assertIn(f'value="{second_date}"', body)

    def test_waitlisted_future_booking_labeled_on_waitinglist(self):
        first_date = self._occ_dates(1)[0]
        environ = self._login_environ("regular@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        self.store.add_registration(
            "yoga-class-1", first_date, user.user_id, hash_token(new_token()), status=STATUS_WAITLISTED,
        )
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn('<span class="ribbon">On waitinglist</span>', body)
        self.assertNotIn(">Booked<", body)

    def test_canceled_booking_is_not_shown_at_all(self):
        first_date = self._occ_dates(1)[0]
        environ = self._login_environ("regular@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        reg = self.store.add_registration("yoga-class-1", first_date, user.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        # NOT a bare "date-badge" substring check -- that class name is
        # always present in the page's own <style> block regardless of
        # whether any badge actually rendered; check for the real element.
        self.assertNotIn('<span class="date-btn date-badge">', body)
        # Canceled -- back to a normal, pickable date.
        self.assertIn(f'value="{first_date}"', body)

    def test_past_booking_for_this_course_is_not_shown(self):
        # the operator: "Only FUTURE bookings!" -- old history has no place here.
        environ = self._login_environ("regular@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        self.store.add_registration("yoga-class-1", "2020-01-01", user.user_id, hash_token(new_token()))
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        # NOT a bare "date-badge" substring check -- that class name is
        # always present in the page's own <style> block regardless of
        # whether any badge actually rendered; check for the real element.
        self.assertNotIn('<span class="date-btn date-badge">', body)

    def test_anonymous_guest_never_sees_already_booked_badges(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        # NOT a bare "date-badge" substring check -- that class name is
        # always present in the page's own <style> block regardless of
        # whether any badge actually rendered; check for the real element.
        self.assertNotIn('<span class="date-btn date-badge">', body)

    def test_already_booked_badge_has_no_radio_input(self):
        first_date, second_date = self._occ_dates(2)
        environ = self._login_environ("regular@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        self.store.add_registration("yoga-class-1", first_date, user.user_id, hash_token(new_token()))
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        badge_html = body.split('<span class="date-btn date-badge">', 1)[1].split("</span></span></span>", 1)[0]
        self.assertNotIn("<input", badge_html)
        self.assertNotIn("<label", badge_html)

    def test_booking_result_page_also_shows_banner_when_logged_in(self):
        environ = self._login_environ("regular@example.org")
        occs = build_occurrences(
            self.course, self.settings, datetime.now(timezone.utc), lambda sn, d: 0, lambda start, end: False,
        )
        occ_date = occs[0].date.isoformat()
        form = {"occurrence_date": occ_date, "name": "Regular", "email": "regular@example.org", "agree": "on"}
        body_bytes = urlencode(form).encode()
        post_environ = dict(environ)
        post_environ.update({"CONTENT_LENGTH": str(len(body_bytes)), "wsgi.input": io.BytesIO(body_bytes)})
        _status, _headers, body = self.app.book("POST", "yoga-class-1", post_environ)
        self.assertIn('class="session-banner"', body)

    def test_banner_links_back_to_the_homepage(self):
        # 2026-07-09, the operator: "allow in the banner to also go back to
        # https://booking.example.org".
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.courses("GET", environ)
        self.assertIn(f'<a href="{self.settings.base_url}">', body)


class AlwaysVisibleBannerRolloutTest(unittest.TestCase):
    """2026-07-14, the operator, expanding the always-visible-banner request:
    "also /admin should get the same boxed banner, basically ALL pages
    except for the index.html!" -- spot-checks a representative page from
    each remaining category that didn't already have one (guest magic
    links, host magic links, admin login/overview, /my/reset,
    /my/confirm(-email), /my/cancel-email-change). Every _room_-specific
    banner rename/wording test lives closer to its own feature (e.g.
    SessionBannerTest, MaintenanceModeTest); this class only confirms the
    box itself now shows up everywhere it didn't before."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()
        self.app = App(self.settings, self.store)

    def test_guest_cancel_bogus_token_shows_banner(self):
        _status, _headers, body = self.app.guest_cancel("GET", "bogus-token", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)

    def test_guest_reinstate_bogus_token_shows_banner(self):
        _status, _headers, body = self.app.guest_reinstate("GET", "bogus-token", {})
        self.assertIn('class="session-banner"', body)

    def test_host_cancel_bogus_id_shows_banner(self):
        _status, _headers, body = self.app.host_cancel("GET", "bogus-reg-id", {})
        self.assertIn('class="session-banner"', body)

    def test_host_reinstate_bogus_id_shows_banner(self):
        _status, _headers, body = self.app.host_reinstate("GET", "bogus-reg-id", {})
        self.assertIn('class="session-banner"', body)

    def test_host_cancel_occurrence_unknown_course_shows_banner(self):
        _status, _headers, body = self.app.host_cancel_occurrence("GET", "no-such-course", "2026-07-01", {})
        self.assertIn('class="session-banner"', body)

    def test_my_reset_form_shows_banner(self):
        _status, _headers, body = self.app.my_reset("GET", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)

    def test_my_confirm_invalid_token_shows_banner(self):
        _status, _headers, body = self.app.my_confirm("GET", "bogus-token", {})
        self.assertIn('class="session-banner"', body)

    def test_my_confirm_email_invalid_token_shows_banner(self):
        _status, _headers, body = self.app.my_confirm_email("GET", "bogus-token", {})
        self.assertIn('class="session-banner"', body)

    def test_my_cancel_email_change_invalid_token_shows_banner(self):
        _status, _headers, body = self.app.my_cancel_email_change("GET", "bogus-token", {})
        self.assertIn('class="session-banner"', body)

    def test_admin_login_page_shows_boxed_homepage_link_only(self):
        # This page IS the admin login form -- same reasoning as /my's own
        # login page: no redundant "Not logged in" text, just the box +
        # homepage link (see _homepage_only_banner_html()'s docstring).
        _status, _headers, body = self.app.admin_login("GET", {})
        self.assertIn('class="session-banner"', body)
        self.assertNotIn("Not logged in", body)
        self.assertIn(f'<a href="{self.settings.base_url}">', body)

    def test_admin_overview_shows_admin_banner_when_logged_in(self):
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("<span>Admin</span>", body)


class MySettingsTest(unittest.TestCase):
    """2026-07-10, the operator: a self-service /my/settings page to change name
    (immediate) and login email (two-step, dual-address-notified --
    see app/webapp.py's "-- /my/settings --" section for the full
    rationale). Reuses SessionBannerTest's own App/Store construction and
    _login_environ helper pattern."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()
        self.app = App(self.settings, self.store)
        self.sent_emails = []

        def recorder(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            self.sent_emails.append((to, subject, body))

        for target in ("app.webapp.send_mail",):
            patcher = patch(target, side_effect=recorder)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _login_environ(self, email: str = "regular@example.org", name: str = "Regular") -> dict:
        user = self.store.upsert_user_for_booking(email, name)
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        sid = webapp._new_session({"kind": "guest", "user_id": user.user_id})
        return {"HTTP_COOKIE": f"session={sid}"}

    @staticmethod
    def _post_environ(environ: dict, form: dict) -> dict:
        body_bytes = urlencode(form).encode()
        return dict(environ, CONTENT_LENGTH=str(len(body_bytes)), **{"wsgi.input": io.BytesIO(body_bytes)})

    # -- auth gating ----------------------------------------------------------

    def test_settings_page_requires_login(self):
        # 2026-07-14, the operator: "Can the page please redirect to login when
        # the session times out?" -- a missing/expired session now
        # redirects to /my (the login page) instead of a bare 403.
        status, headers, _body = self.app.my_settings("GET", {})
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_settings_name_post_requires_login(self):
        status, headers, _body = self.app.my_settings_name("POST", self._post_environ({}, {"name": "X"}))
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_settings_email_post_requires_login(self):
        status, headers, _body = self.app.my_settings_email(
            "POST", self._post_environ({}, {"email": "x@example.org"})
        )
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_settings_email_cancel_post_requires_login(self):
        status, headers, _body = self.app.my_settings_email_cancel("POST", self._post_environ({}, {}))
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    # -- name change ------------------------------------------------------------

    def test_settings_page_shows_current_name_and_email(self):
        environ = self._login_environ("regular@example.org", "Regular")
        _status, _headers, body = self.app.my_settings("GET", environ)
        self.assertIn('value="Regular"', body)
        self.assertIn("regular@example.org", body)

    def test_name_change_takes_effect_immediately(self):
        environ = self._login_environ("regular@example.org", "Regular")
        status, headers, _body = self.app.my_settings_name(
            "POST", self._post_environ(environ, {"name": "New Name"})
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my/settings")
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.name, "New Name")

    def test_blank_name_is_rejected(self):
        environ = self._login_environ("regular@example.org", "Regular")
        status, _headers, body = self.app.my_settings_name("POST", self._post_environ(environ, {"name": "  "}))
        self.assertEqual(status, "200 OK")
        self.assertIn("can&#x27;t be empty", body)
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.name, "Regular")

    # -- email change: requesting -----------------------------------------------

    def test_invalid_new_email_is_rejected(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.my_settings_email(
            "POST", self._post_environ(environ, {"email": "not-an-email"})
        )
        self.assertIn("valid email", body)
        self.assertEqual(self.sent_emails, [])

    def test_same_as_current_email_is_rejected(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.my_settings_email(
            "POST", self._post_environ(environ, {"email": "regular@example.org"})
        )
        self.assertIn("already your current email", body)
        self.assertEqual(self.sent_emails, [])

    def test_email_already_used_by_another_account_is_rejected(self):
        self.store.upsert_user_for_booking("taken@example.org", "Someone Else")
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.my_settings_email(
            "POST", self._post_environ(environ, {"email": "taken@example.org"})
        )
        self.assertIn("already in use", body)
        self.assertEqual(self.sent_emails, [])

    def test_valid_email_change_request_sets_pending_and_emails_both_addresses(self):
        environ = self._login_environ("regular@example.org")
        status, headers, _body = self.app.my_settings_email(
            "POST", self._post_environ(environ, {"email": "new@example.org"})
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my/settings")
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.pending_email, "new@example.org")
        self.assertEqual(user.email, "regular@example.org")  # not swapped yet
        recipients = {to for to, _subj, _body in self.sent_emails}
        self.assertEqual(recipients, {"new@example.org", "regular@example.org"})
        new_body = next(b for to, _s, b in self.sent_emails if to == "new@example.org")
        old_body = next(b for to, _s, b in self.sent_emails if to == "regular@example.org")
        self.assertIn("/my/confirm-email/", new_body)  # only the new address gets the link
        self.assertNotIn("/my/confirm-email/", old_body)
        self.assertIn("new@example.org", old_body)  # old address is told what it's changing to
        # 2026-07-11, the operator: "Please provide a link without login" -- the
        # old address's own cancel link must not require a session.
        self.assertIn("/my/cancel-email-change/", old_body)
        self.assertNotIn("/my/settings", old_body)
        self.assertNotIn("/my/cancel-email-change/", new_body)  # only the OLD address gets this one

    def test_settings_page_shows_pending_change_and_hides_request_form(self):
        environ = self._login_environ("regular@example.org")
        self.app.my_settings_email("POST", self._post_environ(environ, {"email": "new@example.org"}))
        _status, _headers, body = self.app.my_settings("GET", environ)
        self.assertIn("Email change pending", body)
        self.assertIn("new@example.org", body)
        self.assertNotIn('name="email" type="email" required', body)  # request form is gone

    def test_email_change_rate_limited(self):
        environ = self._login_environ("regular@example.org")
        for i in range(5):
            self.app.my_settings_email("POST", self._post_environ(environ, {"email": f"try{i}@example.org"}))
        _status, _headers, body = self.app.my_settings_email(
            "POST", self._post_environ(environ, {"email": "onemore@example.org"})
        )
        self.assertIn("Too many attempts", body)

    def test_cancel_pending_change_clears_it(self):
        environ = self._login_environ("regular@example.org")
        self.app.my_settings_email("POST", self._post_environ(environ, {"email": "new@example.org"}))
        status, headers, _body = self.app.my_settings_email_cancel("POST", environ)
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my/settings")
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.pending_email, "")

    def _request_email_change(self, environ, new_email: str) -> str:
        """Requests a change and returns the plaintext confirm token
        embedded in the email sent to the new address."""
        self.app.my_settings_email("POST", self._post_environ(environ, {"email": new_email}))
        new_body = next(b for to, _s, b in self.sent_emails if to == new_email)
        return new_body.split("/my/confirm-email/", 1)[1].split("\n", 1)[0].strip()

    # -- email change: confirming -----------------------------------------------

    def test_confirm_email_get_previews_without_applying(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_email_change(environ, "new@example.org")
        _status, _headers, body = self.app.my_confirm_email("GET", token, {})
        self.assertIn("regular@example.org", body)
        self.assertIn("new@example.org", body)
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.email, "regular@example.org")  # unchanged by GET
        # 2026-07-11, the operator: audit of every single-submit-button direct-link page.
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_confirm_email_post_applies_change_and_emails_both_addresses(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_email_change(environ, "new@example.org")
        self.sent_emails.clear()
        status, _headers, body = self.app.my_confirm_email("POST", token, {})
        self.assertEqual(status, "200 OK")
        self.assertIn("new@example.org", body)
        self.assertIsNone(self.store.find_user_by_email("regular@example.org"))
        updated = self.store.find_user_by_email("new@example.org")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.pending_email, "")
        recipients = {to for to, _s, _b in self.sent_emails}
        self.assertEqual(recipients, {"new@example.org", "regular@example.org"})

    def test_confirm_email_works_without_a_login_session(self):
        # Deliberately confirmable from a different browser/device than
        # the one that requested it -- no session cookie required.
        environ = self._login_environ("regular@example.org")
        token = self._request_email_change(environ, "new@example.org")
        status, _headers, _body = self.app.my_confirm_email("POST", token, {})
        self.assertEqual(status, "200 OK")

    def test_confirm_email_token_cannot_be_reused(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_email_change(environ, "new@example.org")
        self.app.my_confirm_email("POST", token, {})
        _status, _headers, body = self.app.my_confirm_email("POST", token, {})
        self.assertIn("invalid or has already been used", body)

    def test_confirm_email_invalid_token_shows_generic_message(self):
        _status, _headers, body = self.app.my_confirm_email("GET", "bogus-token", {})
        self.assertIn("invalid or has already been used", body)

    def test_confirm_email_post_invalidates_every_session_for_the_account(self):
        # 2026-07-07, the operator: "Logout user before email is changed (so with
        # its old email). Then redirect the user back to login page /my
        # with the link please."
        environ = self._login_environ("regular@example.org")
        session_id = environ["HTTP_COOKIE"].split("session=", 1)[1]
        self.assertIn(session_id, webapp.SESSIONS)
        token = self._request_email_change(environ, "new@example.org")
        _status, _headers, body = self.app.my_confirm_email("POST", token, {})
        self.assertNotIn(session_id, webapp.SESSIONS)
        self.assertIn('<a href="/my">log in</a>', body)
        self.assertNotIn("/my/settings", body)

    def test_confirm_email_superseded_by_newer_request_shows_friendlier_message(self):
        environ = self._login_environ("regular@example.org")
        old_token = self._request_email_change(environ, "first@example.org")
        self.sent_emails.clear()
        self._request_email_change(environ, "second@example.org")
        _status, _headers, body = self.app.my_confirm_email("GET", old_token, {})
        self.assertIn("newer email change request", body)

    def test_confirm_email_expired_token_shows_expiry_message(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_email_change(environ, "new@example.org")
        user = self.store.find_user_by_email("regular@example.org")
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        self.store.set_pending_email(user.user_id, user.pending_email, user.pending_email_token_hash, stale)
        _status, _headers, body = self.app.my_confirm_email("GET", token, {})
        self.assertIn("expired", body)

    # -- email change: canceling from the CURRENT (old) address's own
    # no-login link (2026-07-11, the operator: "Please provide a link without
    # login" -- the old address's notification email used to point at
    # /my/settings, which needs a session) --------------------------------

    def _request_cancel_token(self, environ, new_email: str) -> str:
        """Requests a change and returns the plaintext CANCEL token
        embedded in the notification email sent to the CURRENT (old)
        address -- separate from _request_email_change's confirm token,
        which goes to the new address instead."""
        self.app.my_settings_email("POST", self._post_environ(environ, {"email": new_email}))
        old_body = next(b for to, _s, b in self.sent_emails if to == "regular@example.org")
        return old_body.split("/my/cancel-email-change/", 1)[1].split("\n", 1)[0].strip()

    def test_cancel_email_change_get_previews_without_applying(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_cancel_token(environ, "new@example.org")
        _status, _headers, body = self.app.my_cancel_email_change("GET", token, {})
        self.assertIn("regular@example.org", body)
        self.assertIn("new@example.org", body)
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.pending_email, "new@example.org")  # unchanged by GET
        # 2026-07-11, the operator: audit of every single-submit-button direct-link page.
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_cancel_email_change_works_without_a_login_session(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_cancel_token(environ, "new@example.org")
        status, _headers, body = self.app.my_cancel_email_change("POST", token, {})
        self.assertEqual(status, "200 OK")
        self.assertIn("canceled", body)
        user = self.store.find_user_by_email("regular@example.org")
        self.assertEqual(user.pending_email, "")
        self.assertEqual(user.email, "regular@example.org")

    def test_cancel_email_change_token_cannot_be_reused(self):
        environ = self._login_environ("regular@example.org")
        token = self._request_cancel_token(environ, "new@example.org")
        self.app.my_cancel_email_change("POST", token, {})
        _status, _headers, body = self.app.my_cancel_email_change("POST", token, {})
        self.assertIn("invalid or has already been used", body)

    def test_cancel_email_change_invalid_token_shows_generic_message(self):
        _status, _headers, body = self.app.my_cancel_email_change("GET", "bogus-token", {})
        self.assertIn("invalid or has already been used", body)

    def test_cancel_email_change_does_not_confirm_the_change(self):
        # Sanity check that this link can only ABORT, never complete, the
        # pending change -- see User.pending_email_cancel_token_hash's own
        # docstring on why it's a separate token from the confirm one.
        environ = self._login_environ("regular@example.org")
        token = self._request_cancel_token(environ, "new@example.org")
        self.app.my_cancel_email_change("POST", token, {})
        self.assertIsNone(self.store.find_user_by_email("new@example.org"))


class MySessionStatusTest(unittest.TestCase):
    """GET /my/session (2026-07-09) -- the STATIC homepage's own JS calls
    this to ask "is this browser already logged in as a guest?" so it can
    swap its plain Login button for the same banner-style affordance the
    dynamic pages show. See my_session_status()'s own docstring for the
    iframe/third-party-cookie caveat this doesn't (and can't) solve."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()
        self.app = App(self.settings, self.store)

    def test_anonymous_reports_logged_out(self):
        _status, _headers, body = self.app.my_session_status("GET", {})
        self.assertEqual(json.loads(body), {"logged_in": False, "email": None})

    def test_guest_session_reports_logged_in_with_email(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        sid = webapp._new_session({"kind": "guest", "user_id": user.user_id})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        _status, _headers, body = self.app.my_session_status("GET", environ)
        self.assertEqual(json.loads(body), {"logged_in": True, "email": "regular@example.org"})

    def test_admin_session_never_reports_logged_in(self):
        # This is a GUEST-identity check for the marketing homepage -- an
        # admin session must never make it show "logged in as {admin}".
        sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        _status, _headers, body = self.app.my_session_status("GET", environ)
        self.assertEqual(json.loads(body), {"logged_in": False, "email": None})

    def test_post_not_allowed(self):
        status, _headers, _body = self.app.my_session_status("POST", {})
        self.assertEqual(status, "405 Method Not Allowed")

    def test_content_type_is_json(self):
        _status, headers, _body = self.app.my_session_status("GET", {})
        self.assertIn(("Content-Type", "application/json"), headers)


class RecordPageViewTest(unittest.TestCase):
    """_record_page_view (2026-07-13) -- called from App.__call__ on every
    request, before routing. See its own docstring for why "since when
    connected" needs no new field (expires - SESSION_TTL_SECONDS)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def _new_tracked_session(self, data):
        sid = webapp._new_session(data)
        self.addCleanup(webapp.SESSIONS.pop, sid, None)
        return sid

    def test_anonymous_request_is_a_no_op(self):
        webapp._record_page_view({}, "/courses")  # must not raise

    def test_updates_last_page_and_last_seen_for_a_real_session(self):
        sid = self._new_tracked_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        before = time.time()
        webapp._record_page_view(environ, "/admin")
        self.assertEqual(webapp.SESSIONS[sid]["last_page"], "/admin")
        self.assertGreaterEqual(webapp.SESSIONS[sid]["last_seen"], before)

    def test_expired_session_cookie_is_a_no_op(self):
        sid = self._new_tracked_session({"kind": "admin"})
        webapp.SESSIONS[sid]["expires"] = time.time() - 1
        environ = {"HTTP_COOKIE": f"session={sid}"}
        webapp._record_page_view(environ, "/admin")
        # _get_session() already purges an expired entry outright -- so
        # there's nothing left to have set last_page on.
        self.assertNotIn(sid, webapp.SESSIONS)

    def test_second_call_overwrites_the_first(self):
        sid = self._new_tracked_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        webapp._record_page_view(environ, "/admin")
        webapp._record_page_view(environ, "/admin/cancel/xyz")
        self.assertEqual(webapp.SESSIONS[sid]["last_page"], "/admin/cancel/xyz")


class InternalStatusEndpointTest(unittest.TestCase):
    """GET /internal/status (2026-07-13) -- the same-process, localhost-only
    JSON diagnostic endpoint `my-bt status` queries directly. See
    internal_status()'s own docstring for the full trust model."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings()
        self.app = App(self.settings, self.store)

    def _new_tracked_session(self, data):
        sid = webapp._new_session(data)
        self.addCleanup(webapp.SESSIONS.pop, sid, None)
        return sid

    def test_get_only(self):
        status, _headers, _body = self.app.internal_status("POST", {})
        self.assertEqual(status, "405 Method Not Allowed")

    def test_rejects_requests_that_arrived_via_nginx(self):
        # Anything carrying X-Forwarded-For arrived through the reverse
        # proxy, not a direct localhost connection from my-bt -- see
        # _client_ip's own docstring for why nginx always sets this on
        # every request it forwards.
        status, _headers, _body = self.app.internal_status("GET", {"HTTP_X_FORWARDED_FOR": "1.2.3.4"})
        self.assertEqual(status, "403 Forbidden")

    def test_content_type_is_json(self):
        _status, headers, _body = self.app.internal_status("GET", {})
        self.assertIn(("Content-Type", "application/json"), headers)

    def test_reports_maintenance_off_by_default(self):
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        self.assertFalse(payload["maintenance"]["enabled"])

    def test_reports_maintenance_on_with_message(self):
        maintenance.enable(self.store.data_dir, message="back soon")
        self.addCleanup(maintenance.disable, self.store.data_dir)
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        self.assertTrue(payload["maintenance"]["enabled"])
        self.assertEqual(payload["maintenance"]["message"], "back soon")

    def test_lists_guest_session_by_email(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        self._new_tracked_session({"kind": "guest", "user_id": user.user_id})
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        matches = [s for s in payload["sessions"] if s["who"] == "regular@example.org"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["kind"], "guest")

    def test_admin_session_shows_as_admin_not_a_user_id(self):
        self._new_tracked_session({"kind": "admin"})
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        self.assertTrue(any(s["who"] == "admin" and s["kind"] == "admin" for s in payload["sessions"]))

    def test_expired_session_excluded(self):
        # Other tests in this same process leave their own (uncleaned-up)
        # sessions in the module-level SESSIONS dict, admin ones included
        # -- so this identifies its OWN session by a unique guest email
        # rather than asserting the whole list is empty.
        user = self.store.upsert_user_for_booking("expired-session@example.org", "Expired")
        sid = self._new_tracked_session({"kind": "guest", "user_id": user.user_id})
        webapp.SESSIONS[sid]["expires"] = time.time() - 1
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        self.assertFalse(any(s["who"] == "expired-session@example.org" for s in payload["sessions"]))

    def test_last_page_and_last_seen_reflect_record_page_view(self):
        # Uses a guest session (unique email) rather than "admin" -- an
        # admin session has no unique identifier in the payload, and other
        # tests' own (uncleaned-up) admin sessions can otherwise be picked
        # up by mistake here.
        user = self.store.upsert_user_for_booking("last-page@example.org", "LastPage")
        sid = self._new_tracked_session({"kind": "guest", "user_id": user.user_id})
        environ = {"HTTP_COOKIE": f"session={sid}"}
        webapp._record_page_view(environ, "/my")
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        row = next(s for s in payload["sessions"] if s["who"] == "last-page@example.org")
        self.assertEqual(row["last_page"], "/my")
        self.assertIsNotNone(row["last_seen"])

    def test_never_viewed_a_page_yet_shows_none(self):
        user = self.store.upsert_user_for_booking("never-viewed@example.org", "NeverViewed")
        self._new_tracked_session({"kind": "guest", "user_id": user.user_id})
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        row = next(s for s in payload["sessions"] if s["who"] == "never-viewed@example.org")
        self.assertIsNone(row["last_page"])
        self.assertIsNone(row["last_seen"])

    def test_connected_since_is_session_creation_time(self):
        before = time.time()
        user = self.store.upsert_user_for_booking("connected-since@example.org", "ConnectedSince")
        self._new_tracked_session({"kind": "guest", "user_id": user.user_id})
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        row = next(s for s in payload["sessions"] if s["who"] == "connected-since@example.org")
        connected_since = datetime.fromisoformat(row["connected_since"]).timestamp()
        # Should be "now" (creation time), NOT "now + SESSION_TTL_SECONDS"
        # (the expiry) -- comfortably within a few seconds either way.
        self.assertAlmostEqual(connected_since, before, delta=5)

    def test_guest_with_unresolvable_user_id_shows_placeholder_not_a_crash(self):
        self._new_tracked_session({"kind": "guest", "user_id": "totally-unresolvable-user-id"})
        _status, _headers, body = self.app.internal_status("GET", {})
        payload = json.loads(body)
        row = next(s for s in payload["sessions"] if "totally-unresolvable-user-id" in s["who"])
        self.assertIn("totally-unresolvable-user-id", row["who"])


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


class HtmlToTextTest(unittest.TestCase):
    """_html_to_text() converts course.description's operator-authored rich
    HTML into plain text for the booking-confirmed/waitlisted emails (see
    App._send_booking_result_email) -- send_mail has no HTML alternative."""

    def test_strips_tags_and_converts_list_items_to_dashes(self):
        markup = (
            "<p><b>Please:</b></p>"
            "<ul><li>Use a working email address</li>"
            "<li>Book week by week</li></ul>"
        )
        text = webapp._html_to_text(markup)
        self.assertNotIn("<", text)
        self.assertIn("Please:", text)
        self.assertIn("- Use a working email address", text)
        self.assertIn("- Book week by week", text)

    def test_converts_link_to_text_plus_url(self):
        markup = '<p>Details: <a href="https://example.org" target="_blank">example.org</a></p>'
        text = webapp._html_to_text(markup)
        self.assertIn("example.org (https://example.org)", text)

    def test_unescapes_html_entities(self):
        text = webapp._html_to_text("<p>Bek&auml;mpft &amp; getestet</p>")
        self.assertIn("Bekämpft & getestet", text)

    def test_plain_text_passes_through_unchanged(self):
        self.assertEqual(webapp._html_to_text("test"), "test")


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


class BookingFlowTest(unittest.TestCase):
    """End-to-end coverage of book()'s two branches (already-confirmed
    account vs. not-yet-confirmed email) plus my_confirm/my_reset/my()'s
    password login -- this is the core of the 2026-07-05 account-
    confirmation rework: closes the old hijack hole (booking form could
    silently overwrite ANY existing account's login credential just by
    resubmitting that email) and defers capacity/calendar-sync until the
    guest actually confirms. CalDAV sync itself is mocked to a no-op --
    already covered by ConflictCheckerTest/calendar_sync's own tests; what's
    new here is the STATUS_PENDING_CONFIRMATION gating and the token flow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        course = make_course(shortname="yoga-class-1", weekday="wed", capacity=1)
        # conflict_calendars must match what FakeTransport's PROPFIND lists
        # below ("Calendar", "Yoga-Bookings") -- same setup as ConflictCheckerTest.
        self.settings = make_settings(courses=(course,), conflict_calendars=("Calendar", "Yoga-Bookings"))
        self.app = App(self.settings, self.store)
        self.transport = FakeTransport()
        self.app.caldav = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self.transport,
        )
        self.app._sync = lambda *a, **kw: None  # calendar mechanics covered elsewhere

        self.sent_emails: list[tuple[str, str, str]] = []
        # 2026-07-09, the operator: bcc_attendee_emails -- a separate, parallel
        # list (keyed the same way as self.sent_emails, same order) rather
        # than widening every tuple in self.sent_emails itself, since
        # dozens of existing tests already unpack that one as a plain
        # 3-tuple (to, subject, body); only tests that actually care about
        # the bcc_addrs a given send_mail() call was given read this one.
        self.sent_email_bcc: list[tuple[str, str, tuple]] = []
        occs = build_occurrences(
            course, self.settings, datetime.now(timezone.utc),
            lambda sn, d: 0, lambda start, end: False,
        )
        self.occs = occs
        self.occ_date = occs[0].date.isoformat()

        def recorder(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            self.sent_emails.append((to, subject, body))
            self.sent_email_bcc.append((to, subject, bcc_addrs))

        # Cancellation emails are composed in app.cancellation (factored
        # out of App on 2026-07-06 so `my-bt cancel` can reuse them), and the
        # promotion emails in app.cancel_flow (factored out the same day so
        # `my-bt cancel`/`my-bt erase` can reuse the full cancel+promote+sync
        # flow) -- each calls its own imported send_mail reference, so
        # patching app.webapp.send_mail alone wouldn't touch those call
        # sites, same as app.watchdog.send_mail needs its own patch in
        # test_watchdog.py.
        for target in ("app.webapp.send_mail", "app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            patcher = patch(target, side_effect=recorder)
            patcher.start()
            self.addCleanup(patcher.stop)

        # login_limiter/reset_ip_limiter (app/webapp.py) are module-level
        # singletons shared across every test in the process. Tests here
        # don't set an X-Forwarded-For/REMOTE_ADDR, so every my_reset()
        # POST in this whole file shares one _client_ip() fallback key --
        # reset it after each test so no test's rate-limit tests (or a
        # future one added to this class) can spuriously trip depending on
        # how many other tests already hit this endpoint earlier in the
        # same run.
        self.addCleanup(webapp.reset_ip_limiter.reset, "reset-ip:unknown")

    def _post(self, fn, args, form: dict):
        body = urlencode(form).encode()
        environ = {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        return fn("POST", *args, environ)

    def _book(self, email: str, name: str = "Alice", occ_date: str | None = None, agree: str = "on"):
        form = {"occurrence_date": occ_date or self.occ_date, "name": name, "email": email, "agree": agree}
        return self._post(self.app.book, ("yoga-class-1",), form)

    def _confirm_token_from_last_email(self) -> str:
        _, _, body = self.sent_emails[-1]
        return body.split("/my/confirm/")[1].split("\n")[0].strip()

    # -- new/unconfirmed email: pending, no capacity/calendar impact ------

    def test_new_email_books_pending_and_holds_no_capacity(self):
        _status, _headers, body = self._book("newguest@example.org")
        self.assertIn("Almost there", body)
        user = self.store.find_user_by_email("newguest@example.org")
        regs = self.store.registrations_for_user(user.user_id)
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].status, STATUS_PENDING_CONFIRMATION)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 0)

    def test_new_email_gets_only_a_confirm_email_not_a_booking_email(self):
        self._book("newguest@example.org")
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your example.org account"])

    def test_almost_there_page_uses_the_same_recap_box_as_every_other_confirmation(self):
        # 2026-07-07, the operator (comparing this page's old one-off "Your spot
        # for X on Y" sentence against host-cancel/Booked!/cancel-
        # confirmation's shared What/When/Where box): "The way you present
        # 'one course instance' ... should be CONSISTENT EVERYWHERE."
        _status, _headers, body = self._book("newguest@example.org")
        self.assertIn("\U0001F9D8 What:", body)  # same emoji as _course_recap_html elsewhere
        self.assertIn("\U0001F550 When:", body)
        self.assertIn("\U0001F4CD Where:", body)
        self.assertNotIn("Your spot for", body)

    def test_almost_there_resend_does_not_ask_for_email_again(self):
        # The guest just typed their email into the booking form -- the
        # resend control must carry it along as a hidden field, not send
        # them to a bare "type your email" form again (confusing, and
        # exactly what "should not ask you for your email address" means).
        _status, _headers, body = self._book("newguest@example.org")
        self.assertIn('name="email" value="newguest@example.org"', body)
        self.assertNotIn('<input class="big-input" name="email"', body)
        # And it must NOT be a plain link to /my/reset's own page (that
        # page is branded "Forgot your password?", confusing right after
        # a fresh booking) -- it's a same-page form/button instead.
        self.assertNotIn('<a href="/my/reset">', body)
        self.assertIn('action="/my/reset"', body)

    def test_returning_unconfirmed_email_resends_without_adding_a_second_pending_row(self):
        # 2026-07-11, the operator: "silent re-registration for unconfirmed
        # accounts" -- re-submitting the SAME course+date before
        # confirming used to insert a brand-new pending row every time
        # (see Store.has_pending_registration's own docstring for the
        # multi-row-promoted-at-once consequence this closes). The resend
        # itself is still deliberate/unconditional -- only the extra ROW
        # is gone.
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", name="Alice Again", occ_date=self.occ_date)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(len(self.store.registrations_for_user(user.user_id)), 1)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your example.org account", "Confirm your example.org account"])

    def test_returning_unconfirmed_email_for_a_different_date_still_adds_its_own_pending_row(self):
        # The dedup key is (course, date, user) -- a genuinely different
        # occurrence is a separate booking attempt, not a retry, and must
        # still get its own pending row.
        other_date = self.occs[1].date.isoformat()
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", occ_date=other_date)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(len(self.store.registrations_for_user(user.user_id)), 2)

    def test_confirming_after_multiple_resends_only_confirms_once(self):
        # End-to-end guard: even before this fix, my_confirm() promoted
        # EVERY pending row for the user with no per-course+date dedup of
        # its own -- so without the storage-layer fix, 3 retries here
        # would have landed as 3 separate CONFIRMED registrations for the
        # exact same class.
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", occ_date=self.occ_date)
        token = self._confirm_token_from_last_email()
        self._post(self.app.my_confirm, (token,), {"password": "hunter22"})
        user = self.store.find_user_by_email("newguest@example.org")
        regs = self.store.registrations_for_user(user.user_id)
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].status, STATUS_CONFIRMED)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 1)

    # -- the account-hijack fix --------------------------------------------

    def test_booking_never_changes_an_existing_accounts_password(self):
        user = self.store.upsert_user_for_booking("victim@example.org", "Victim")
        h, s = hash_secret("realpassword")
        self.store.set_password(user.user_id, h, s)
        self._book("victim@example.org", name="Attacker-supplied name")
        reloaded = self.store.find_user_by_email("victim@example.org")
        self.assertEqual(reloaded.password_hash, h)
        self.assertEqual(reloaded.password_salt, s)
        # name IS updated (that's fine/intended -- only the credential is protected)
        self.assertEqual(reloaded.name, "Attacker-supplied name")

    # -- already-confirmed account: instant booking, as before -------------

    def test_confirmed_account_books_instantly(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        _status, _headers, body = self._book("regular@example.org", name="Regular")
        self.assertIn("Booked!", body)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 1)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertIn("Booking confirmed: Dynamic Ashtanga Vinyasa Yoga on " + self.occ_date, subjects)
        email_body = next(b for _, s, b in self.sent_emails if s.startswith("Booking confirmed:"))
        self.assertIn("Your spot is confirmed:", email_body)
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", email_body)
        self.assertIn(f"When: {self.occ_date} 17h15 - 18h55", email_body)
        self.assertIn("Where: Example Community Gym, Room 1", email_body)
        self.assertIn("test", email_body)  # course.description, repeated in full
        self.assertIn("Manage your bookings: https://example.org/my", email_body)
        self.assertIn("Cancel this booking directly: https://example.org/cancel/", email_body)

    def test_confirmed_booking_email_greets_by_name(self):
        # 2026-07-08, the operator: "they should now all start with 'Dear <NAME>',
        # correct?" -- they didn't yet; added here (and to the waitlisted,
        # cancellation, and reinstatement participant emails).
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        email_body = next(b for _, s, b in self.sent_emails if s.startswith("Booking confirmed:"))
        self.assertTrue(email_body.startswith("Dear Regular,\n\n"))

    def test_confirmed_booking_email_attaches_a_publish_ics(self):
        # 2026-07-09, the operator: "Can you please attach a calendar invite also
        # in the email that is sent to the participant?"
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            if subject.startswith("Booking confirmed:"):
                captured["ics_attachment"] = ics_attachment
            self.sent_emails.append((to, subject, body))

        with patch("app.webapp.send_mail", side_effect=spy):
            self._book("regular@example.org", name="Regular")
        self.assertIsNotNone(captured.get("ics_attachment"))
        filename, ics_text, method = captured["ics_attachment"]
        self.assertTrue(filename.endswith(".ics"))
        self.assertEqual(method, "PUBLISH")
        self.assertIn("METHOD:PUBLISH", ics_text)
        self.assertIn("Dynamic Ashtanga Vinyasa Yoga", ics_text)

    # -- double-booking gap fix (2026-07-10, the operator: "double booking possible?") --

    def test_confirmed_account_cannot_book_the_same_slot_twice(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        self.sent_emails.clear()
        _status, _headers, body = self._book("regular@example.org", name="Regular")
        self.assertIn("already booked", body)
        regs = self.store.registrations_for_user(user.user_id)
        self.assertEqual(len(regs), 1)  # no second row inserted
        self.assertEqual(self.sent_emails, [])  # no re-notification either

    def test_waitlisted_account_cannot_rebook_the_same_slot(self):
        # the operator explicitly confirmed a second WAITLISTED attempt should be
        # blocked too, not treated as a way to grab an extra spot.
        for i in range(2):
            user = self.store.upsert_user_for_booking(f"guest{i}@example.org", f"Guest{i}")
            h, s = hash_secret("hunter22")
            self.store.set_password(user.user_id, h, s)
        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        self._book("guest1@example.org", name="Guest1")  # waitlisted
        guest1 = self.store.find_user_by_email("guest1@example.org")
        self.sent_emails.clear()
        _status, _headers, body = self._book("guest1@example.org", name="Guest1")
        self.assertIn("already booked", body)
        self.assertEqual(len(self.store.registrations_for_user(guest1.user_id)), 1)

    def test_different_occurrence_date_is_still_allowed(self):
        # Sanity check: the guard is per course+date, not per course.
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        course = self.settings.course("yoga-class-1")
        occs = build_occurrences(
            course, self.settings, datetime.now(timezone.utc),
            lambda sn, d: 0, lambda start, end: False,
        )
        other_date = occs[1].date.isoformat()
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self._book("regular@example.org", name="Regular", occ_date=other_date)
        self.assertIn("Booked!", body)
        self.assertEqual(len(self.store.registrations_for_user(user.user_id)), 2)

    def test_brand_new_pending_confirmation_still_allows_repeat_attempts(self):
        # Regression guard: STATUS_PENDING_CONFIRMATION is deliberately
        # excluded from the new check -- test_returning_unconfirmed_email_
        # adds_another_pending_and_resends above must keep passing unchanged.
        self._book("newguest@example.org")
        _status, _headers, body = self._book("newguest@example.org", name="Alice Again")
        self.assertNotIn("already booked", body)
        self.assertIn("Almost there", body)

    def test_confirmed_account_waitlisted_when_full(self):
        for i in range(2):
            user = self.store.upsert_user_for_booking(f"guest{i}@example.org", f"Guest{i}")
            h, s = hash_secret("hunter22")
            self.store.set_password(user.user_id, h, s)
        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        _status, _headers, body = self._book("guest1@example.org", name="Guest1")
        self.assertIn("waitlist", body)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertTrue(any(s.startswith("Waitlisted:") for s in subjects))
        email_body = next(b for _, s, b in self.sent_emails if s.startswith("Waitlisted:"))
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", email_body)
        self.assertIn(f"When: {self.occ_date} 17h15 - 18h55", email_body)
        self.assertIn("Where: Example Community Gym, Room 1", email_body)
        self.assertIn("Manage your bookings: https://example.org/my", email_body)
        self.assertIn("Leave the waitlist directly: https://example.org/cancel/", email_body)
        self.assertTrue(email_body.startswith("Dear Guest1,\n\n"))

    def test_waitlisted_booking_email_has_no_ics_attachment(self):
        # No confirmed slot yet -- nothing real to add to a calendar.
        for i in range(2):
            user = self.store.upsert_user_for_booking(f"guest{i}@example.org", f"Guest{i}")
            h, s = hash_secret("hunter22")
            self.store.set_password(user.user_id, h, s)
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            if subject.startswith("Waitlisted:"):
                captured["ics_attachment"] = ics_attachment
            self.sent_emails.append((to, subject, body))

        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        with patch("app.webapp.send_mail", side_effect=spy):
            self._book("guest1@example.org", name="Guest1")
        self.assertIn("ics_attachment", captured)  # subject matched
        self.assertIsNone(captured["ics_attachment"])

    def test_promoted_from_waitlist_email_matches_the_same_layout(self):
        # Regression coverage for the 2026-07-05 consistency fix: this
        # email used to stay on the old one-line "at {start_time}" format
        # after _send_booking_result_email() got the richer What/When/Where
        # layout -- see _booking_details_text().
        guest0, environ0 = self._login_as_guest("guest0@example.org", "Guest0")
        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        guest1, _environ1 = self._login_as_guest("guest1@example.org", "Guest1")
        self._book("guest1@example.org", name="Guest1")  # waitlisted
        reg0 = self.store.registrations_for_user(guest0.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg0.registration_id,), {"message": ""}, environ0)
        email_body = next(b for _, s, b in self.sent_emails if s.startswith("You're in!"))
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", email_body)
        self.assertIn(f"When: {self.occ_date} 17h15 - 18h55", email_body)
        self.assertIn("Where: Example Community Gym, Room 1", email_body)
        self.assertIn("Manage or cancel this booking: https://example.org/my", email_body)

        # 2026-07-06 fix: admin_email must ALSO get a copy, same standing
        # default as every other booking/cancellation email in this app --
        # this was the one path that silently left admin_email out.
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("admin@example.org", to_addrs)
        admin_mail = next(
            b for t, s, b in self.sent_emails
            if t == "admin@example.org" and s.startswith("Promoted from waitlist:")
        )
        self.assertEqual(
            next(s for t, s, _ in self.sent_emails if t == "admin@example.org" and s.startswith("Promoted")),
            f"Promoted from waitlist: Dynamic Ashtanga Vinyasa Yoga on {self.occ_date}",
        )
        self.assertIn("Guest1 <guest1@example.org>", admin_mail)
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", admin_mail)
        self.assertIn(f"When: {self.occ_date} 17h15 - 18h55", admin_mail)
        self.assertIn("Where: Example Community Gym, Room 1", admin_mail)

    def test_promoted_from_waitlist_email_attaches_a_publish_ics(self):
        guest0, environ0 = self._login_as_guest("guest0@example.org", "Guest0")
        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        guest1, _environ1 = self._login_as_guest("guest1@example.org", "Guest1")
        self._book("guest1@example.org", name="Guest1")  # waitlisted
        reg0 = self.store.registrations_for_user(guest0.user_id)[0]
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            if subject.startswith("You're in!"):
                captured["ics_attachment"] = ics_attachment
            self.sent_emails.append((to, subject, body))

        with patch("app.cancel_flow.send_mail", side_effect=spy):
            self._post_with_session(self.app.my_cancel, (reg0.registration_id,), {"message": ""}, environ0)
        self.assertIsNotNone(captured.get("ics_attachment"))
        _filename, ics_text, method = captured["ics_attachment"]
        self.assertEqual(method, "PUBLISH")
        self.assertIn("METHOD:PUBLISH", ics_text)

    # -- my_confirm: sets password, promotes pending ------------------------

    def test_my_confirm_invalid_token_shows_error(self):
        _status, _headers, body = self._post(self.app.my_confirm, ("bogus-token",), {"password": "hunter22"})
        self.assertIn("invalid", body.lower())

    def test_my_confirm_shows_which_account_the_link_belongs_to(self):
        # The token in the URL gives no visual confirmation of whose
        # account it is -- show the email so the guest can double-check
        # this is actually theirs before typing a password.
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, _headers, body = self.app.my_confirm("GET", token, {})
        self.assertIn("newguest@example.org", body)

    def test_my_confirm_sets_password_and_promotes_pending_booking(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, headers, body = self._post(self.app.my_confirm, (token,), {"password": "hunter22"})
        self.assertIn("Account &amp; booking confirmed", body)
        self.assertIn("did succeed for", body)
        self.assertIn("at 17h15 - 18h55 (Example Community Gym, Room 1)", body)
        self.assertTrue(any(h[0] == "Set-Cookie" for h in headers))
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertNotEqual(user.password_hash, "")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.assertEqual(reg.status, STATUS_CONFIRMED)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 1)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertIn("Booking confirmed: Dynamic Ashtanga Vinyasa Yoga on " + self.occ_date, subjects)
        # 2026-07-06 regression guard: the "Booking confirmed" email fired
        # right after setting a password must NOT also dangle a redundant
        # "set up your account" link -- this exercises the exact path
        # where the in-memory `user` object my_confirm() already had
        # (fetched before set_password()) could go stale and still show
        # an empty password_hash to _send_booking_result_guest_email()'s
        # new account-setup-link check.
        confirmed_booking_email = next(
            b for _, s, b in self.sent_emails
            if s == "Booking confirmed: Dynamic Ashtanga Vinyasa Yoga on " + self.occ_date
        )
        self.assertNotIn("/my/confirm/", confirmed_booking_email)

    def test_my_confirm_recheck_capacity_lands_on_waitlist_if_filled_meanwhile(self):
        # capacity=1: someone else confirms and fills the only spot WHILE
        # the first guest's account is still unconfirmed.
        self._book("newguest@example.org")
        other = self.store.upsert_user_for_booking("other@example.org", "Other")
        h, s = hash_secret("hunter22")
        self.store.set_password(other.user_id, h, s)
        self._book("other@example.org", name="Other")  # instantly confirmed, fills capacity=1

        # other's booking was instant (no confirm email) -- the newguest's
        # confirm link is still the FIRST email ever sent in this test.
        token = self.sent_emails[0][2].split("/my/confirm/")[1].split("\n")[0].strip()
        self._post(self.app.my_confirm, (token,), {"password": "hunter22"})
        user = self.store.find_user_by_email("newguest@example.org")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.assertEqual(reg.status, STATUS_WAITLISTED)

    def test_my_confirm_rejects_a_too_short_password(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, _headers, body = self._post(self.app.my_confirm, (token,), {"password": "ab"})
        self.assertIn("at least 8 characters", body)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(user.password_hash, "")

    # -- my_confirm: link expiry + "a newer link was sent" (2026-07-07) -----
    # the operator: "when will the confirmation links be invalid?" (they never
    # expired before -- see CONFIRM_TOKEN_TTL_HOURS) and "a new email
    # should invalidate the pending link ... [and] clicking the
    # invalidated link should inform the user that there should be a NEW
    # link coming to him".

    def test_my_confirm_link_older_than_ttl_shows_expired_not_invalid(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        user = self.store.find_user_by_email("newguest@example.org")
        # Backdate the SAME token's created_at past the TTL -- set_confirm_token
        # takes a hash + timestamp, so this re-stores the identical hash
        # (the raw token in the emailed link is unchanged) with an old clock.
        stale = (datetime.now(timezone.utc) - timedelta(hours=webapp.CONFIRM_TOKEN_TTL_HOURS, minutes=1)).isoformat(
            timespec="seconds"
        )
        self.store.set_confirm_token(user.user_id, hash_token(token), stale)
        _status, _headers, body = self.app.my_confirm("GET", token, {})
        self.assertIn("expired", body.lower())
        self.assertIn(f"{webapp.CONFIRM_TOKEN_TTL_HOURS} hours", body)
        self.assertNotIn("invalid or has already been used", body)

    def test_my_confirm_link_within_ttl_still_works(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, _headers, body = self.app.my_confirm("GET", token, {})
        self.assertIn("Setting a password for", body)

    def test_my_confirm_superseded_link_says_a_newer_one_was_sent(self):
        self._book("newguest@example.org")
        old_token = self._confirm_token_from_last_email()
        # A second request for the same email (my_reset's unified
        # resend/forgot-password flow -- see _send_confirm_email) generates
        # a fresh token and, per Store.set_confirm_token, shifts the old
        # hash into prev_confirm_token_hash before overwriting it.
        self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        new_token = self._confirm_token_from_last_email()
        self.assertNotEqual(old_token, new_token)

        _status, _headers, old_body = self.app.my_confirm("GET", old_token, {})
        self.assertIn("newer link", old_body.lower())
        self.assertIn("check your inbox", old_body.lower())
        self.assertNotIn("invalid or has already been used", old_body)

        _status, _headers, new_body = self.app.my_confirm("GET", new_token, {})
        self.assertIn("Setting a password for", new_body)

    def test_my_confirm_unknown_garbage_token_still_shows_generic_message(self):
        # No confirm/reset flow was ever started for this exact string --
        # neither the expiry nor the "superseded" path should fire.
        _status, _headers, body = self.app.my_confirm("GET", "totally-made-up-token", {})
        self.assertIn("invalid or has already been used", body)

    def test_my_confirm_already_used_link_is_not_mistaken_for_superseded(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        self._post(self.app.my_confirm, (token,), {"password": "hunter22"})
        # Clicking the same (now-consumed) link again: set_password() clears
        # both confirm_token_hash AND prev_confirm_token_hash, so this must
        # fall through to the generic message, not "a newer link was sent".
        _status, _headers, body = self.app.my_confirm("GET", token, {})
        self.assertIn("invalid or has already been used", body)

    # -- _send_confirm_email: full https:// URL + expiry note (2026-07-07) --
    # the operator: "also please write rather https://booking.example.org in the text".

    def test_confirm_email_body_spells_out_the_full_base_url(self):
        self._book("newguest@example.org")
        _, _, body = self.sent_emails[-1]
        self.assertIn(f"confirm your booking account on {self.settings.base_url}", body)

    def test_confirm_email_states_the_expiry_and_that_older_links_are_void(self):
        self._book("newguest@example.org")
        _, _, body = self.sent_emails[-1]
        self.assertIn(f"expires in {webapp.CONFIRM_TOKEN_TTL_HOURS} hours", body)
        self.assertIn("only the link in this latest email will work", body)

    def test_confirm_email_opens_with_a_greeting_by_name(self):
        # 2026-07-07, the operator (screenshot of this email): "please formulate
        # the email a bit more nicer".
        self._book("newguest@example.org", name="Alice")
        _, _, body = self.sent_emails[-1]
        self.assertTrue(body.startswith("Dear Alice,"))

    # -- my_reset: unified resend/forgot-password, never leaks existence ---

    def test_my_reset_same_response_whether_or_not_email_exists(self):
        _s1, _h1, body_known = self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        _s2, _h2, body_unknown = self._post(self.app.my_reset, (), {"email": "nobody@example.org"})
        self.assertEqual(body_known, body_unknown)

    def test_my_reset_confirmation_page_links_back_to_login(self):
        _status, _headers, body = self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        self.assertIn('<a href="/my">Back to login</a>', body)

    def test_my_reset_emails_unconfirmed_account_a_confirm_link(self):
        self._book("newguest@example.org")
        self.sent_emails.clear()
        self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your example.org account"])

    def test_my_reset_emails_confirmed_account_a_reset_link(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._post(self.app.my_reset, (), {"email": "regular@example.org"})
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Reset your example.org password"])

    # -- my_reset: 2026-07-05, visible cooldown -- safe because neither
    # limiter is ever consulted based on whether the email exists --------

    def test_my_reset_repeated_same_email_shows_cooldown_on_the_form(self):
        email = "cooldown-target@example.org"
        self.addCleanup(webapp.login_limiter.reset, f"reset:{email}")
        for _ in range(5):
            _status, _headers, body = self._post(self.app.my_reset, (), {"email": email})
            self.assertIn("Check your email", body)
        _status, _headers, body = self._post(self.app.my_reset, (), {"email": email})
        # NOT the (now misleading) "we sent it" page -- the form again,
        # with the button disabled and counting down, same pattern as the
        # login lockout (the operator, 2026-07-05: "where there is a cooldown
        # active, it should be visible on the form with the button").
        self.assertNotIn("Check your email", body)
        self.assertIn("Too many attempts", body)
        self.assertIn('id="reset-btn"', body)
        self.assertIn("btn.disabled = true;", body)

    def test_my_reset_cooldown_response_identical_for_real_vs_fake_email(self):
        # The core anti-enumeration property must survive adding a visible
        # cooldown: both login_limiter and reset_ip_limiter are checked
        # BEFORE find_user_by_email is ever consulted, so which one blocks
        # a request never depends on whether the submitted email actually
        # has an account -- verified here by checking the two cooldown
        # pages render identically (modulo the countdown number itself).
        real_user = self.store.upsert_user_for_booking("realaccount@example.org", "Real")
        h, s = hash_secret("hunter22")
        self.store.set_password(real_user.user_id, h, s)
        for email in ("realaccount@example.org", "fakeaccount@example.org"):
            self.addCleanup(webapp.login_limiter.reset, f"reset:{email}")
            for _ in range(5):
                self._post(self.app.my_reset, (), {"email": email})
        _s1, _h1, body_real = self._post(self.app.my_reset, (), {"email": "realaccount@example.org"})
        _s2, _h2, body_fake = self._post(self.app.my_reset, (), {"email": "fakeaccount@example.org"})
        strip_seconds = lambda b: re.sub(r"\(\d+s\)", "(Ns)", b)
        self.assertEqual(strip_seconds(body_real), strip_seconds(body_fake))
        self.assertIn("Too many attempts", body_real)

    def test_my_reset_ip_wide_limiter_trips_after_many_different_emails(self):
        # Defense-in-depth against enumeration: the per-email limiter alone
        # never slows down someone trying many DIFFERENT (mostly fake)
        # addresses, since each fresh string gets its own untouched
        # counter -- this is the gap the operator asked about ("know which client
        # IP triggered this and have a cooldown by IP").
        for k in [f"reset:probe{i}@example.org" for i in range(21)]:
            self.addCleanup(webapp.login_limiter.reset, k)
        for i in range(20):
            _status, _headers, body = self._post(self.app.my_reset, (), {"email": f"probe{i}@example.org"})
            self.assertIn("Check your email", body)
        _status, _headers, body = self._post(self.app.my_reset, (), {"email": "probe20@example.org"})
        self.assertIn("Too many attempts", body)
        self.assertIn('id="reset-btn"', body)

    # -- my_signup: 2026-07-06, "Sign up" tab on /my -------------------------

    def test_signup_creates_a_brand_new_account_and_emails_a_confirm_link(self):
        _status, _headers, body = self._post(
            self.app.my_signup, (), {"name": "New Person", "email": "brandnew@example.org"}
        )
        self.assertIn("Check your email", body)
        user = self.store.find_user_by_email("brandnew@example.org")
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "New Person")
        self.assertEqual(user.password_hash, "")
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your example.org account"])

    def test_signup_success_message_is_boxed_not_a_bare_paragraph(self):
        # 2026-07-06 fix: the operator flagged the success message (and the
        # signup form's own "we'll email you a link" hint) as looking like
        # stray unstyled sentences on an otherwise-empty page ("This is a
        # bit ugly" / "same here with the sentence") -- both now render
        # inside a .card, same visual weight as the form it replaces.
        _status, _headers, body = self._post(
            self.app.my_signup, (), {"name": "New Person", "email": "boxed@example.org"}
        )
        self.assertIn('<div class="card"><p>Check your email', body)

    def test_signup_form_hint_lives_inside_the_card(self):
        _status, _headers, body = self.app.my("GET", {})
        card_start = body.index('<form method="post" action="/my/signup"')
        card_end = body.index("</form>", card_start)
        self.assertIn("We'll email you a link", body[card_start:card_end])

    def test_signup_existing_account_does_not_overwrite_name(self):
        user = self.store.upsert_user_for_booking("existing@example.org", "Real Name")
        self._post(self.app.my_signup, (), {"name": "Some Other Name", "email": "existing@example.org"})
        reloaded = self.store.find_user_by_email("existing@example.org")
        self.assertEqual(reloaded.name, "Real Name")

    def test_signup_existing_confirmed_account_gets_a_reset_link_not_a_confirm_one(self):
        user = self.store.upsert_user_for_booking("confirmed@example.org", "Confirmed")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._post(self.app.my_signup, (), {"name": "Whatever", "email": "confirmed@example.org"})
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Reset your example.org password"])

    def test_signup_missing_name_shows_error_and_reopens_signup_tab(self):
        _status, _headers, body = self._post(self.app.my_signup, (), {"name": "", "email": "x@example.org"})
        self.assertIn("Please fill in your name and a valid email.", body)
        self.assertIn('id="my-tab-signup" name="my-tab" class="tab-radio" checked', body)
        self.assertIsNone(self.store.find_user_by_email("x@example.org"))

    def test_signup_invalid_email_shows_error(self):
        _status, _headers, body = self._post(self.app.my_signup, (), {"name": "Someone", "email": "not-an-email"})
        self.assertIn("Please fill in your name and a valid email.", body)

    def test_signup_get_redirects_to_my(self):
        status, headers, _body = self.app.my_signup("GET", {})
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")

    def test_signup_shares_rate_limit_bucket_with_my_reset(self):
        # Both endpoints end up doing the same thing (create/confirm an
        # account + email a token) -- they must share one lockout budget,
        # keyed the same as my_reset's own (reset:<email>).
        email = "shared-bucket@example.org"
        self.addCleanup(webapp.login_limiter.reset, f"reset:{email}")
        for _ in range(5):
            self._post(self.app.my_signup, (), {"name": "X", "email": email})
        _status, _headers, body = self._post(self.app.my_reset, (), {"email": email})
        self.assertIn("Too many attempts", body)

    def test_login_tab_open_by_default_on_get(self):
        _status, _headers, body = self.app.my("GET", {})
        self.assertIn('id="my-tab-login" name="my-tab" class="tab-radio" checked', body)
        self.assertIn('id="my-tab-signup" name="my-tab" class="tab-radio" ', body)
        self.assertNotIn('id="my-tab-signup" name="my-tab" class="tab-radio" checked', body)

    # -- /my password login --------------------------------------------------

    def test_my_login_succeeds_with_correct_password(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        _status, headers, _body = self._post(self.app.my, (), {"email": "regular@example.org", "password": "hunter22"})
        self.assertTrue(any(h[0] == "Set-Cookie" for h in headers))

    def test_my_login_fails_with_wrong_password(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        _status, headers, body = self._post(self.app.my, (), {"email": "regular@example.org", "password": "wrong"})
        self.assertFalse(any(h[0] == "Set-Cookie" for h in headers))
        self.assertIn("Email and/or password did not match.", body)

    def test_my_login_fails_for_a_still_unconfirmed_account(self):
        self._book("newguest@example.org")
        _status, headers, body = self._post(
            self.app.my, (), {"email": "newguest@example.org", "password": "anything"}
        )
        self.assertFalse(any(h[0] == "Set-Cookie" for h in headers))
        self.assertIn("Email and/or password did not match.", body)

    # -- "Login link returns to originating page" (2026-07-11, the operator) ------

    def test_login_page_get_carries_a_safe_next_into_a_hidden_field(self):
        _status, _headers, body = self.app.my("GET", {"QUERY_STRING": "next=/book/yoga-class-1"})
        card_start = body.index('<form method="post" action="/my" class="card">')
        card_end = body.index("</form>", card_start)
        self.assertIn(
            '<input type="hidden" name="next" value="/book/yoga-class-1">', body[card_start:card_end]
        )

    def test_login_page_get_drops_an_unsafe_next(self):
        _status, _headers, body = self.app.my("GET", {"QUERY_STRING": "next=https://evil.example/"})
        self.assertNotIn('name="next"', body)

    def test_login_page_get_with_no_next_omits_the_hidden_field(self):
        _status, _headers, body = self.app.my("GET", {})
        self.assertNotIn('name="next"', body)

    def test_successful_login_redirects_to_the_safe_next_path(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        status, headers, _body = self._post(
            self.app.my, (),
            {"email": "regular@example.org", "password": "hunter22", "next": "/book/yoga-class-1"},
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/book/yoga-class-1")

    def test_successful_login_with_no_next_still_lands_on_my(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        status, headers, _body = self._post(
            self.app.my, (), {"email": "regular@example.org", "password": "hunter22"},
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")

    def test_successful_login_ignores_an_unsafe_next_and_lands_on_my(self):
        # Same allowlist as the GET-side hidden-field check, re-applied
        # here since a POST body is just as hand-editable as a URL -- see
        # _safe_next_path()'s own docstring.
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        status, headers, _body = self._post(
            self.app.my, (),
            {"email": "regular@example.org", "password": "hunter22", "next": "https://evil.example/"},
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")

    def test_my_login_lockout_shows_a_disabled_button_with_a_live_countdown(self):
        # Same visible-countdown treatment as admin/login (2026-07-05) --
        # see AdminLoginRateLimitTest's equivalent test.
        email = "lockout-target@example.org"
        self.addCleanup(webapp.login_limiter.reset, f"guest:{email}")
        user = self.store.upsert_user_for_booking(email, "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        for _ in range(5):
            self._post(self.app.my, (), {"email": email, "password": "wrong"})
        _status, _headers, body = self._post(self.app.my, (), {"email": email, "password": "wrong"})
        self.assertIn("Too many attempts", body)
        self.assertIn('id="my-login-btn"', body)
        self.assertIn("btn.disabled = true;", body)
        self.assertIn("Login", body)

    # -- /my logged-in bookings table + cancel/delete dialogs (task #43) ----

    def _login_as_guest(self, email: str, name: str = "Alice") -> tuple:
        """Returns (user, environ) for a confirmed guest -- bypasses the
        actual login form (already covered above) since these tests are
        about what the logged-in page renders/does, not the login itself."""
        user = self.store.upsert_user_for_booking(email, name)
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        sid = webapp._new_session({"kind": "guest", "user_id": user.user_id})
        return user, {"HTTP_COOKIE": f"session={sid}"}

    # -- /book: hide dates the logged-in guest already holds a spot for
    # (2026-07-11, the operator, pasted `my-bt list` output showing he was already
    # `confirmed` for a date /book still offered): "If I am already booked
    # + confirmed for a date this date should simply be hidden here for
    # me." -----------------------------------------------------------------

    def test_already_confirmed_date_is_hidden_from_the_picker_when_logged_in(self):
        user, environ = self._login_as_guest("regular@example.org")
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, user.user_id, "tok-hash", capacity=1
        )
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertNotIn(f'value="{self.occ_date}"', body)

    def test_already_waitlisted_date_is_also_hidden(self):
        # has_active_registration() treats CONFIRMED and WAITLISTED alike --
        # the picker must too.
        user, environ = self._login_as_guest("regular@example.org")
        # capacity=1 course: book someone else first so `user` lands on the
        # waitlist, not confirmed.
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, "other-user-id", "tok-hash-other", capacity=1
        )
        reg = self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, user.user_id, "tok-hash", capacity=1
        )
        self.assertEqual(reg.status, STATUS_WAITLISTED)
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertNotIn(f'value="{self.occ_date}"', body)

    def test_a_canceled_bookings_date_is_not_hidden(self):
        # Only ACTIVE (confirmed/waitlisted) rows hide a date -- a
        # canceled one must not block rebooking the same date.
        user, environ = self._login_as_guest("regular@example.org")
        reg = self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, user.user_id, "tok-hash", capacity=1
        )
        self.store.cancel(reg.registration_id, canceled_by="guest")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn(f'value="{self.occ_date}"', body)

    def test_anonymous_guest_still_sees_every_date(self):
        # The filter only applies once we know WHO is asking -- an
        # anonymous visitor has no "already booked" state to hide anything
        # against.
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, user.user_id, "tok-hash", capacity=1
        )
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn(f'value="{self.occ_date}"', body)

    # -- /book: selected-date/radio mismatch on back/forward-cache restore
    # (2026-07-11, the operator, "BUG: selected date!", screenshot showing a
    # different date's radio highlighted than the "Selected date:" text
    # read) -------------------------------------------------------------

    def test_book_form_disables_autocomplete_and_reasserts_selection_on_pageshow(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn('id="book-form" autocomplete="off"', body)
        self.assertIn('window.addEventListener("pageshow", refresh);', body)

    def test_my_bookings_table_shows_title_time_location_not_shortname(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("Dynamic Ashtanga Vinyasa Yoga", body)
        # 2026-07-10, the operator: "add the weekday to the TIME column (e.g. SAT
        # 10h45-12h45)" -- weekday_time_range_label(), not time_range_label()
        # (no spaces around the dash, and a leading 3-letter weekday code).
        self.assertIn("WED 17h15-18h55", body)
        self.assertIn("Example Community Gym, Room 1", body)
        # The shortname legitimately appears once now, as the /book/<shortname>
        # link target (2026-07-07, the operator: "make the 'Course' string a link to
        # the course booking page") -- but never as the VISIBLE cell text,
        # which must stay the human title.
        self.assertNotIn(">yoga-class-1<", body)

    def test_course_cell_links_to_its_own_booking_page(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<a href="/book/yoga-class-1">Dynamic Ashtanga Vinyasa Yoga</a>', body)

    def test_location_cell_stays_plain_text_without_a_location_url(self):
        # This class's own "yoga-class-1" course (see setUp) never sets
        # location_url -- the default, and the whole point of the field
        # being optional: nothing changes for anyone who doesn't set it.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("<td>Example Community Gym, Room 1</td>", body)
        self.assertNotIn('<a href="" target="_blank"', body)

    def test_location_cell_links_out_when_location_url_is_set(self):
        # 2026-07-09, the operator: "add a location_url and then use it on /my in
        # the column location to make those clickable."
        store = Store(tempfile.mkdtemp())
        course = make_course(
            shortname="yoga-class-2", weekday="wed", capacity=10,
            location="Trier Studio", location_url="https://maps.example.org/?q=Trier+Studio",
        )
        settings = make_settings(courses=(course,), conflict_calendars=("Calendar", "Yoga-Bookings"))
        app = App(settings, store)
        app.caldav = CalDAVClient(
            settings.caldav_url, settings.caldav_username, settings.caldav_password, transport=FakeTransport(),
        )
        app._sync = lambda *a, **kw: None
        with patch("app.webapp.send_mail", side_effect=lambda *a, **kw: None), \
                patch("app.cancellation.send_mail", side_effect=lambda *a, **kw: None):
            user = store.upsert_user_for_booking("regular@example.org", "Regular")
            h, s = hash_secret("hunter22")
            store.set_password(user.user_id, h, s)
            sid = webapp._new_session({"kind": "guest", "user_id": user.user_id})
            environ = {"HTTP_COOKIE": f"session={sid}"}
            occ_date = build_occurrences(
                course, settings, datetime.now(timezone.utc), lambda sn, d: 0, lambda start, end: False,
            )[0].date.isoformat()
            self._post(
                app.book, ("yoga-class-2",),
                {"occurrence_date": occ_date, "name": "Regular", "email": "regular@example.org", "agree": "on"},
            )
            _status, _headers, body = app.my("GET", environ)
        self.assertIn(
            '<a href="https://maps.example.org/?q=Trier+Studio" target="_blank" rel="noopener">'
            "Trier Studio</a>",
            body,
        )

    def test_my_bookings_cancel_button_opens_dialog_with_reason_field(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        reg = self.store.registrations_for_user(user.user_id)[0]
        cancel_id = f"cancel-{reg.registration_id}"
        self.assertIn(f'<dialog id="{cancel_id}-dialog" class="card">', body)
        self.assertIn("Are you sure?", body)
        self.assertIn(f'<textarea name="message" rows="2" class="big-input" form="{cancel_id}-form">', body)
        self.assertIn("Confirm cancellation", body)
        self.assertIn("Never mind", body)

    def test_my_bookings_table_has_filter_and_sort_wired_up(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<table id="my-upcoming-table"', body)
        self.assertIn("<thead><tr>", body)
        self.assertIn('<input type="search" id="my-upcoming-table-filter"', body)
        # 2026-07-10: the sort/filter script no longer looks up the table by
        # id (that made its own text, and therefore its CSP hash, differ per
        # table_id) -- it locates the table via document.currentScript's own
        # DOM position instead, so the same script text is emitted verbatim
        # everywhere. Assert on that stable marker instead of the old
        # getElementById() call.
        self.assertIn("document.currentScript.previousElementSibling", body)

    def test_my_upcoming_table_date_column_defaults_to_ascending_sort(self):
        # 2026-07-08, the operator (screenshot of /admin?past=1): "Please by
        # default sort ... by Date ... Like this people see also the sort
        # arrow and can understand that this page is sortable." Data was
        # already server-side sorted; only the visual indicator was
        # missing until an actual click. Upcoming is rendered ascending.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<th data-default-sort="asc">Date<span class="sort-indicator"></span></th>', body)

    def test_my_past_table_date_column_defaults_to_descending_sort(self):
        # Same 2026-07-08 request as the ascending/upcoming test above --
        # Past is rendered most-recent-first (descending), so its default
        # sort indicator must match, not just copy Upcoming's "asc".
        user, environ = self._login_as_guest("regular@example.org")
        self._import_past(user.user_id, "2026-01-01", "past-1")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<th data-default-sort="desc">Date<span class="sort-indicator"></span></th>', body)

    def test_my_bookings_cancel_button_enabled_for_waitlisted_row_too(self):
        # 2026-07-05 fix: the Cancel button used to be disabled for
        # anything but STATUS_CONFIRMED, which made it impossible to leave
        # the waitlist from this page (the emailed cancel link and /admin
        # could always do both) -- see the comment on `disabled` in my().
        for i in range(2):
            u = self.store.upsert_user_for_booking(f"waiter{i}@example.org", f"Waiter{i}")
            h, s = hash_secret("hunter22")
            self.store.set_password(u.user_id, h, s)
        self._book("waiter0@example.org", name="Waiter0")  # capacity=1, fills it
        self._book("waiter1@example.org", name="Waiter1")  # waitlisted
        user1, environ1 = self._login_as_guest("waiter1@example.org")
        _status, _headers, body = self.app.my("GET", environ1)
        reg = self.store.registrations_for_user(user1.user_id)[0]
        self.assertEqual(reg.status, STATUS_WAITLISTED)
        cancel_id = f"cancel-{reg.registration_id}"
        self.assertIn(f'data-dialog="{cancel_id}-dialog" >Cancel', body)  # not "disabled>Cancel"

    def test_account_settings_delete_account_dialog_has_exact_requested_wording(self):
        # 2026-07-14, the operator: "please move the delete button under 'Account
        # settings': and rename to 'DELETE this account'" -- this used to
        # live at the bottom of /my (My bookings); it's on /my/settings
        # (Account settings) now instead.
        _user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my_settings("GET", environ)
        self.assertIn('<dialog id="delete-account-dialog" class="card">', body)
        self.assertIn("DELETE this account", body)
        self.assertIn(
            "Delete your account and all related data? This will cancel any booking you still have!",
            body,
        )
        self.assertIn("Yes, delete everything", body)
        # No-JS/no-<dialog>-support fallback must still be a real confirmation,
        # not silently removed (see the maintainer's local notes -- this was a regression
        # caught and fixed while wiring up the dialog).
        self.assertIn(
            "onsubmit=\"return confirm('Delete your account and all related data? "
            "This will cancel any booking you still have!');\"",
            body,
        )

    def test_my_logout_clears_session_and_redirects_to_the_homepage(self):
        # 2026-07-11, the operator: "pressing logout should bring you back to
        # https://booking.example.org" -- used to redirect to "/my" instead, which
        # was most jarring when the SAME logout form (shared with the
        # static homepage's own JS-rendered banner) was triggered from
        # there: it used to bounce you into the app's own /my login page
        # rather than staying on the site you were just on.
        _user, environ = self._login_as_guest("regular@example.org")
        sid = cookies.SimpleCookie()
        sid.load(environ["HTTP_COOKIE"])
        session_id = sid["session"].value
        status, headers, _body = self.app.my_logout("POST", environ)
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], self.settings.base_url)
        self.assertNotIn(session_id, webapp.SESSIONS)
        # session cookie is actually cleared, not just left alone
        set_cookie = dict(headers)["Set-Cookie"]
        self.assertIn("Max-Age=0", set_cookie)

    def test_my_cancel_captures_optional_reason_and_notifies_both_sides(self):
        # 2026-07-05: both the participant AND the admin get notified of
        # every cancellation now, regardless of who triggered it -- see
        # _send_cancellation_emails(). This is what would surface someone
        # canceling a booking from inside a /my session that isn't theirs.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": "can't make it"}, environ)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_GUEST)
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        self.assertIn("You canceled this booking:", participant_mail)
        # 2026-07-09, the operator (b): guest-initiated cancels label the message
        # from the ATTENDEE's own point of view -- they sent it to the host.
        self.assertIn("Message you sent to the host: can't make it", participant_mail)
        # 2026-07-08, the operator: guest-facing emails greet by name -- the admin
        # copy right below deliberately does NOT (it's a receipt to the
        # operator's own inbox, not a letter -- see greeting_html()'s
        # docstring in app/cancellation.py).
        self.assertTrue(participant_mail.startswith("Dear Regular,\n\n"))
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Canceled:"))
        self.assertIn("Regular <regular@example.org> canceled this booking:", admin_mail)
        self.assertIn("Message: can't make it", admin_mail)
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", admin_mail)
        self.assertFalse(admin_mail.startswith("Dear"))

    # -- 2026-07-09, the operator: bcc_attendee_emails -- "add as BCC the given
    # email address to all mails that go out to the attendees ... so that
    # for some time I can watch this to ensure that all is OK". Confirms
    # attendee-facing emails carry the configured BCC and admin-facing
    # copies of the same event never do. See app.config.Settings.
    # bcc_attendee_email_list and app.emailer.send_mail's own `bcc_addrs`.

    def test_no_bcc_configured_means_no_bcc_on_any_email(self):
        # self.settings (BookingFlowTest's own default) has no
        # bcc_attendee_emails set at all -- every send_mail() call in this
        # class must keep sending with an empty bcc_addrs, same as before
        # this feature existed.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        self.assertTrue(self.sent_email_bcc)
        for _to, _subject, bcc_addrs in self.sent_email_bcc:
            self.assertEqual(bcc_addrs, ())

    def test_bcc_applies_to_the_booking_confirmation_email(self):
        self.app.settings = dataclasses.replace(self.settings, bcc_attendee_emails="watcher@example.org")
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _to, _subject, bcc_addrs = next(
            (t, s, b) for t, s, b in self.sent_email_bcc if t == "regular@example.org" and s.startswith("Booking confirmed:")
        )
        self.assertEqual(bcc_addrs, ("watcher@example.org",))

    def test_bcc_does_not_apply_to_the_admin_new_booking_notification(self):
        self.app.settings = dataclasses.replace(self.settings, bcc_attendee_emails="watcher@example.org")
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _to, _subject, bcc_addrs = next(
            (t, s, b) for t, s, b in self.sent_email_bcc if t == "admin@example.org" and s.startswith("New booking:")
        )
        self.assertEqual(bcc_addrs, ())

    def test_bcc_applies_to_the_cancellation_participant_copy_not_the_admin_copy(self):
        self.app.settings = dataclasses.replace(self.settings, bcc_attendee_emails="watcher@example.org")
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_email_bcc.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        _to, _subject, participant_bcc = next(
            (t, s, b) for t, s, b in self.sent_email_bcc if t == "regular@example.org" and s.startswith("Canceled:")
        )
        self.assertEqual(participant_bcc, ("watcher@example.org",))
        _to, _subject, admin_bcc = next(
            (t, s, b) for t, s, b in self.sent_email_bcc if t == "admin@example.org" and s.startswith("Canceled:")
        )
        self.assertEqual(admin_bcc, ())

    def test_multiple_bcc_addresses_all_apply(self):
        self.app.settings = dataclasses.replace(
            self.settings, bcc_attendee_emails="watcher1@example.org, watcher2@example.org",
        )
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _to, _subject, bcc_addrs = next(
            (t, s, b) for t, s, b in self.sent_email_bcc if t == "regular@example.org" and s.startswith("Booking confirmed:")
        )
        self.assertEqual(bcc_addrs, ("watcher1@example.org", "watcher2@example.org"))

    def test_custom_email_templates_folder_overrides_the_cancel_email_wording(self):
        # 2026-07-09, the operator: "place all email templates into settings.toml
        # [directory] to easily change something there if needed" --
        # end-to-end confirmation that pointing email_templates_folder at
        # a real directory containing just a customized cancel_email.txt
        # actually changes what a real /my/cancel email says, without
        # touching app/cancellation.py at all.
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        (Path(tmpdir.name) / "cancel_email.txt").write_text("CUSTOM WORDING -- {{intro}} -- {{details}}")
        self.app.settings = dataclasses.replace(self.settings, email_templates_folder=tmpdir.name)
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        self.assertTrue(participant_mail.startswith("CUSTOM WORDING -- You canceled this booking:"))

    def test_my_cancel_email_offers_a_reinstate_link_to_the_participant(self):
        # 2026-07-10: originally a plain "book again" link ("With the
        # reschedule button the email could also contain it: If this was
        # a mistake... The what can be a link to the booking page for this
        # course"), superseded same day once a real no-login reinstate
        # page existed for it ("Only from the email there will be a single
        # page for this ... WHAT, WHEN, WHERE like in the confirmation
        # email") -- reinstating the SAME registration is strictly better
        # than a fresh booking, so this replaced the old rebook link.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        # 2026-07-09, the operator (c): a prominent standalone sentence, not a
        # plain "If this was a mistake, you can reinstate it here:" line.
        self.assertIn(
            "In case this was a mistake, you can easily resubscribe: https://example.org/reinstate/",
            participant_mail,
        )
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Canceled:"))
        self.assertNotIn("/reinstate/", admin_mail)
        self.assertIn("Rebook this booking: https://example.org/host-reinstate/", admin_mail)

    def test_my_cancel_reinstate_link_actually_reinstates_the_booking(self):
        # End-to-end: the token embedded in the participant's cancellation
        # email must be a real, working /reinstate/<token> link.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        token = participant_mail.split("/reinstate/")[1].split("\n")[0].strip()
        self._post(self.app.guest_reinstate, (token,), {"message": ""})
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)

    def test_host_cancel_email_reinstate_link_actually_reinstates_the_booking(self):
        # And the admin's own copy's /host-reinstate/<reg_id> link too.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self._post(self.app.host_reinstate, (reg.registration_id,), {"message": ""})
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)

    def test_my_cancel_without_reason_omits_message_line(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        admin_mail = next(b for _, s, b in self.sent_emails if s.startswith("Canceled:"))
        self.assertNotIn("Message:", admin_mail)

    def test_my_cancel_resubmission_does_not_send_duplicate_emails(self):
        # 2026-07-10 fix: a stale cached /my page (browser back-button) or a
        # double-click could resubmit this exact POST for an
        # already-canceled registration_id -- Store.cancel() itself is
        # idempotent about the ROW, but the promote/email side effects
        # weren't gated on that before this fix, so a resubmit used to
        # silently send a second round of "canceled" emails to both sides.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.assertEqual(self.sent_emails, [])

    def _post_with_session(self, fn, args, form: dict, environ: dict):
        body = urlencode(form).encode()
        full_environ = {**environ, "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        return fn("POST", *args, full_environ)

    # -- /cancel/<token>: the guest's one-click link from their own email --

    def test_guest_cancel_via_email_link_notifies_both_sides_with_reason(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        self.sent_emails.clear()
        self._post(self.app.guest_cancel, (token,), {"message": "car trouble"})
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)
        participant_mail = next(b for t, s2, b in self.sent_emails if t == "regular@example.org" and s2.startswith("Canceled:"))
        self.assertIn("You canceled this booking:", participant_mail)
        # 2026-07-09, the operator (b): guest-initiated cancels label the message
        # from the ATTENDEE's own point of view -- they sent it to the host.
        self.assertIn("Message you sent to the host: car trouble", participant_mail)
        admin_mail = next(b for t, s2, b in self.sent_emails if t == "admin@example.org" and s2.startswith("Canceled:"))
        self.assertIn("Regular <regular@example.org> canceled this booking:", admin_mail)

    def test_guest_cancel_confirm_page_has_optional_reason_field(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        _status, _headers, body = self.app.guest_cancel("GET", token, {})
        self.assertIn('<textarea name="message"', body)

    def test_guest_cancel_confirm_page_shows_the_course_recap(self):
        # 2026-07-09, the operator: "This page should look like as described for
        # the admin and like the email ... WHAT WHEN WHERE with emojis and
        # bold font for the keyword followed by the description."
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        _status, _headers, body = self.app.guest_cancel("GET", token, {})
        self.assertIn("What:</b>", body)
        self.assertIn("When:</b>", body)
        self.assertIn("Where:</b>", body)

    def test_guest_cancel_confirm_page_has_a_never_mind_escape(self):
        # 2026-07-11, the operator: "Check all other pages that you can reach
        # with a direct link to have not just one submit button as well!"
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        _status, _headers, body = self.app.guest_cancel("GET", token, {})
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_guest_cancel_token_reuse_shows_invalid_and_sends_no_duplicate_emails(self):
        # 2026-07-10: guest_cancel() is naturally protected against replay
        # (find_by_guest_token_hash only matches a still-CONFIRMED/
        # WAITLISTED row, and Store.cancel() flips that status away), but
        # confirm the end-to-end effect directly: a second POST with the
        # same token must neither re-cancel anything nor send a second
        # round of emails.
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        self._post(self.app.guest_cancel, (token,), {"message": ""})
        self.sent_emails.clear()
        _status, _headers, body = self._post(self.app.guest_cancel, (token,), {"message": ""})
        self.assertIn("invalid or already used", body)
        self.assertEqual(self.sent_emails, [])

    def test_cancellation_email_includes_an_html_alternative_with_the_recap(self):
        # Wiring check: cancellation.send_cancellation_emails (called from
        # guest_cancel) must actually pass html_body through to send_mail,
        # not just be capable of building one -- test_cancellation.py
        # covers course_recap_html()/html_email_body() in isolation, this
        # confirms the real flow uses them.
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("regular@example.org", name="Regular")
        confirmed_body = next(b for _, s2, b in self.sent_emails if s2.startswith("Booking confirmed:"))
        token = confirmed_body.split("/cancel/")[1].split("\n")[0].strip()
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=()):
            if subject.startswith("Canceled:") and to == "regular@example.org":
                captured["html_body"] = html_body
                captured["ics_attachment"] = ics_attachment
            self.sent_emails.append((to, subject, body))

        with patch("app.cancellation.send_mail", side_effect=spy):
            self._post(self.app.guest_cancel, (token,), {"message": ""})
        self.assertIsNotNone(captured.get("html_body"))
        self.assertIn("\U0001F9D8 What:</b>", captured["html_body"])
        # 2026-07-09, the operator: "AND CANCEL-ics as well please. Let's be nice :)"
        ics_filename, ics_text, ics_method = captured["ics_attachment"]
        self.assertEqual(ics_method, "CANCEL")
        self.assertIn("METHOD:CANCEL", ics_text)
        self.assertIn("STATUS:CANCELLED", ics_text)

    # -- /admin overview: same shortname-leak audit as /my's table ----------

    def test_admin_overview_status_column_is_capitalized(self):
        # 2026-07-08, the operator (screenshot of raw "confirmed"/"canceled_by_guest"
        # in the Status column): "I prefer Host and Guest and then also
        # 'Confirmed' for the status" -- same round as the Guests column's
        # own Host/Guest capitalization. See storage.status_label() (moved
        # there 2026-07-13 from webapp._status_label so app/cli_list.py
        # can share it too).
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn("<td>Confirmed</td>", body)
        self.assertNotIn("<td>confirmed</td>", body)

    def test_admin_overview_date_column_is_nowrap(self):
        # 2026-07-08, the operator (screenshot of a narrow Date column wrapping
        # "2025-10-18" onto two lines): "please force the date to be
        # non-breakable".
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn(f'<td class="nowrap">{self.occ_date}</td>', body)

    def test_admin_overview_past_view_sorts_newest_first_by_default(self):
        # 2026-07-08, the operator: "sorting: include past should by default show
        # the newest first please" -- today-or-future (the default view)
        # stays ascending; "include past" flips to descending.
        user, environ = self._login_as_guest("regular@example.org")
        for d in ["2026-01-01", "2026-02-01", "2026-03-01"]:
            self._import_past(user.user_id, d, f"past-{d}")
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        self.assertIn('<th data-default-sort="desc">Date<span class="sort-indicator"></span></th>', body)
        first = body.index("2026-03-01")
        second = body.index("2026-02-01")
        third = body.index("2026-01-01")
        self.assertTrue(first < second < third, "expected newest-first row order")

    def test_admin_overview_default_view_still_sorts_ascending(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn('<th data-default-sort="asc">Date<span class="sort-indicator"></span></th>', body)

    def test_admin_overview_times_booked_header_has_explanatory_subtitle(self):
        # the operator, screenshot of /admin: "please add a small subtitle
        # explaining this: Times booked <in-small-below: for now / total>"
        # -- the column shows "up-to-now/total" (see
        # test_admin_overview_times_booked_excludes_future_bookings above),
        # which isn't self-explanatory at a glance.
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn(
            '<th>Times booked<span class="sort-indicator"></span>'
            '<span class="th-note">for now / total</span></th>',
            body,
        )

    def test_admin_overview_times_booked_excludes_future_bookings(self):
        # 2026-07-08, the operator (screenshot of a guest already showing "9" with
        # sessions still weeks out): "please have the times booked UP TO
        # THIS MOMENT / date (always including of course the current
        # course)", then "actually even better: make it 2/9". Book one
        # past (today-or-earlier, via _import_past) and one future session
        # for the same user and confirm the cell reads "1/2".
        #
        # 2026-07-08 fix: use the real calendar date.today() here, NOT
        # self.occ_date -- self.occ_date is yoga-class-1's next bookable
        # Wednesday slot, which only equals today if the suite happens to
        # run on an actual Wednesday before 17:15 local time. Any other
        # day (or a Wednesday afternoon) it silently rolls to next week,
        # so this "today-session" row wasn't actually dated today and got
        # excluded from the up-to-now count -- a real RPM %check failure
        # on the VPS (run on a Wednesday evening) is what surfaced this.
        #
        # 2026-07-09 fix: that first fix used local date.today(), but
        # admin_overview()'s own cutoff is datetime.now(timezone.utc).date()
        # (see app/webapp.py) -- the two disagree for a couple of hours
        # around local midnight in any timezone ahead of UTC (e.g. CEST,
        # UTC+2), where the local calendar date has already rolled to
        # "tomorrow" while UTC hasn't. A real RPM %check run on the VPS
        # during that window is what surfaced THIS bug: "today-session"
        # got imported dated one day ahead of the UTC cutoff the app
        # actually uses, so it fell on the wrong side of "up to now" and
        # the count read 0/2 instead of 1/2. Match production's own UTC
        # cutoff here instead of the local one.
        user, environ = self._login_as_guest("regular@example.org")
        self._import_past(user.user_id, datetime.now(timezone.utc).date().isoformat(), "today-session")
        self._import_past(user.user_id, "2027-01-01", "future-session")
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        self.assertIn("<td>1/2</td>", body)

    def test_admin_overview_cancel_disabled_for_past_confirmed_booking(self):
        # 2026-07-08, the operator (screenshot of /admin?past=1 showing an enabled
        # Cancel button on a long-past confirmed row): "PAST bookings
        # should NOT have a CANCEL button as well :D"
        user, environ = self._login_as_guest("regular@example.org")
        self._import_past(user.user_id, "2026-01-01", "past-session")
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        cancel_start = body.index("admin-cancel-")
        button_html = body[body.index("<button", cancel_start):body.index("</button>", cancel_start) + 1]
        self.assertIn("disabled", button_html)

    def test_admin_overview_cancel_still_enabled_for_future_confirmed_booking(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        cancel_start = body.index("admin-cancel-")
        button_html = body[body.index("<button", cancel_start):body.index("</button>", cancel_start) + 1]
        self.assertNotIn("disabled", button_html)

    def test_admin_overview_cancel_enabled_for_pending_confirmation_booking(self):
        # 2026-07-13, the operator: a guest who registered but hasn't yet clicked
        # their account-confirmation email link (STATUS_PENDING_CONFIRMATION)
        # previously had NO way to be canceled -- the Cancel button here was
        # unconditionally disabled for that status. Closing that gap: same
        # future-only gating as confirmed/waitlisted, just one more
        # cancelable status.
        user = self.store.upsert_user_for_booking("pending@example.org", "Pending")
        self.store.add_registration(
            "yoga-class-1", self._other_occ_date(), user.user_id, "", status=STATUS_PENDING_CONFIRMATION,
        )
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        cancel_start = body.index("admin-cancel-")
        button_html = body[body.index("<button", cancel_start):body.index("</button>", cancel_start) + 1]
        self.assertNotIn("disabled", button_html)

    def test_admin_overview_shows_course_shortname_not_title(self):
        # 2026-07-06: the Course column shows the internal shortname (compact,
        # matches /book/<shortname>) -- not the human title. The cancel-dialog
        # text still uses the full title (see the cancel-dialog test below).
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn("yoga-class-1", body)

    def test_admin_overview_has_filter_and_sort_wired_to_the_table(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn('<table id="admin-overview-table"', body)
        self.assertIn('<thead><tr>', body)
        self.assertIn('<input type="search" id="admin-overview-table-filter"', body)
        # See the same 2026-07-10 comment in the /my equivalent test above.
        self.assertIn("document.currentScript.previousElementSibling", body)

    def test_admin_overview_date_column_defaults_to_ascending_sort(self):
        # 2026-07-08, the operator: same default-sort-indicator request as /my's.
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn('<th data-default-sort="asc">Date<span class="sort-indicator"></span></th>', body)

    def test_sort_filter_script_is_byte_identical_on_my_and_admin_pages(self):
        # 2026-07-10: the whole point of no longer interpolating table_id
        # into this script (see webapp._SORTABLE_FILTERABLE_TABLE_SCRIPT's
        # own docstring -- a real incident where a hash-based CSP allow-list
        # could only ever cover ONE table_id's worth of script text, leaving
        # every other table silently broken) is that ONE sha256 hash covers
        # every table on every page. Directly assert that property: extract
        # the script block from both /my (which used table_id
        # "my-upcoming-table") and /admin (which used "admin-overview-table")
        # and confirm they're the exact same string, not just "both contain
        # a similar-looking script."
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, my_body = self.app.my(
            "GET", {"HTTP_COOKIE": f"session={webapp._new_session({'kind': 'guest', 'user_id': self.store.find_user_by_email('regular@example.org').user_id})}"},
        )
        admin_sid = webapp._new_session({"kind": "admin"})
        _status, _headers, admin_body = self.app.admin_overview("GET", {"HTTP_COOKIE": f"session={admin_sid}"})

        def extract_sort_script(body: str) -> str:
            start = body.index("<script>\n(function() {\n  var table = document.currentScript")
            end = body.index("</script>", start) + len("</script>")
            return body[start:end]

        self.assertEqual(extract_sort_script(my_body), extract_sort_script(admin_body))

    def test_admin_overview_shows_erased_users_past_registration_with_hash(self):
        # 2026-07-05: erasure moves the user row AND every one of their
        # registration rows to the archive (Store.erase_user) -- the old
        # all_registrations()/find_user_by_id() (live-only) queries used
        # here meant an erased guest's history just vanished from this
        # page. Now uses scope="all" so it still shows up, with the
        # archived hash instead of the real name/email.
        user, environ = self._login_as_guest("erased-guest@example.org")
        self._book("erased-guest@example.org", name="ErasedGuest")
        reg = self.store.registrations_for_user(user.user_id)[0]
        erase_user_by_email(self.store, self.settings, "erased-guest@example.org", today=date.fromisoformat(self.occ_date))
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        self.assertIn("[erased]", body)
        self.assertIn('class="hash-cell"', body)
        self.assertIn("erased:", body)
        self.assertNotIn("erased-guest@example.org", body)
        # Erased/archived rows aren't actionable (admin_cancel can't find
        # an archived registration_id via find_by_id) -- no Cancel button.
        row_start = body.index("[erased]")
        row_html = body[row_start:row_start + 400]
        self.assertNotIn("confirm-dialog-btn", row_html)

    def test_admin_overview_hides_archived_row_without_past_toggle_even_if_future_dated(self):
        # 2026-07-06: archived (erased) registrations are always excluded
        # from the default "today + future only" view regardless of their
        # occurrence_date -- an erased user's booking was already
        # force-canceled before archiving, so a still-future-dated archived
        # row must not leak into the default view just because its date
        # hasn't passed yet. It should only surface once "include past" is
        # toggled on.
        user, environ = self._login_as_guest("erased-guest3@example.org")
        self._book("erased-guest3@example.org", name="ErasedGuest3")
        erase_user_by_email(self.store, self.settings, "erased-guest3@example.org", today=date.fromisoformat(self.occ_date))
        admin_sid = webapp._new_session({"kind": "admin"})
        no_past_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", no_past_environ)
        self.assertNotIn("[erased]", body)
        past_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", past_environ)
        self.assertIn("[erased]", body)

    def test_admin_overview_times_booked_counts_archived_registrations_too(self):
        # 2026-07-08 fix: import the pre-erasure row directly, dated real
        # date.today(), instead of booking via self._book() (which defaults
        # to self.occ_date -- yoga-class-1's next Wednesday slot, not
        # reliably "today", see test_admin_overview_times_booked_excludes_
        # future_bookings above for the full explanation). This test is
        # about the times-booked count on an erased/archived row, not
        # book()'s own flow, so a direct import is equivalent and
        # deterministic. `today=` omitted from erase_user_by_email so it
        # defaults to the real date.today() too.
        #
        # 2026-07-09 fix: switched to datetime.now(timezone.utc).date() --
        # same UTC-vs-local-midnight mismatch as
        # test_admin_overview_times_booked_excludes_future_bookings above.
        # erase_user_by_email's own `today=` default is already UTC-based
        # (app/erasure.py), so only this import needed to change to match.
        user, environ = self._login_as_guest("erased-guest2@example.org")
        self._import_past(user.user_id, datetime.now(timezone.utc).date().isoformat(), "erased-guest2-reg")
        erase_user_by_email(self.store, self.settings, "erased-guest2@example.org")
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        # times-booked column should show 1/1 (one up-to-now, one total),
        # not 0/0, for the erased row -- see the "N/M" format's own comment
        # on times_upto_now_by_user/times_total_by_user (2026-07-08).
        row_start = body.index("[erased]")
        row_html = body[row_start:row_start + 400]
        self.assertIn("<td>1/1</td>", row_html)

    def _other_occ_date(self) -> str:
        """A second, distinct occurrence date for yoga-class-1 -- lets a
        test book two genuinely different sessions instead of
        (post 2026-07-10) colliding on the same course+date, which now
        triggers the duplicate-row guard (see HasActiveRegistrationTest /
        MergeArchivedRegistrationsTest)."""
        course = self.settings.course("yoga-class-1")
        occs = build_occurrences(
            course, self.settings, datetime.now(timezone.utc),
            lambda sn, d: 0, lambda start, end: False,
        )
        return occs[1].date.isoformat()

    def test_admin_overview_auto_merges_pre_erasure_registrations_on_load(self):
        # 2026-07-10, the operator: "the merge should be automatically done if you
        # also display the history in the /admin page" -- if an erased
        # guest books again with the SAME email, book() creates a brand-new
        # live user_id (the old email no longer exists in the live table --
        # it's now a hash on the archived row). /admin shows that old
        # registration alongside the live account's own rows.
        #
        # 2026-07-13, the operator: "/admin should [be] non-mutating" -- this used
        # to physically rewrite the archived row's user_id on disk; now
        # it's purely a display-time merge (see
        # cli_list.merge_archived_for_display) -- nothing on disk changes
        # just from loading this page. (2026-07-14: the one command that
        # used to persist a real merge, `my-bt admin dearchive`, was
        # removed entirely as a GDPR violation -- this display-time merge
        # is unaffected and is now the ONLY merge behavior left.)
        #
        # Pre- and post-erasure bookings are for DIFFERENT occurrence dates
        # here (see test_admin_overview_merge_drops_a_row_that_would_
        # duplicate_the_live_account below for the same-date case, which
        # this display-time merge also has to guard against).
        # 2026-07-08 fix: the pre-erasure row is imported directly, dated
        # real date.today(), instead of booked via self._book() (defaults
        # to self.occ_date, which is only reliably "today" on an actual
        # Wednesday before 17:15 -- see test_admin_overview_times_booked_
        # excludes_future_bookings above). The post-erasure "comeback"
        # booking below stays on the real self._book()/self._other_occ_date()
        # flow -- that part genuinely exercises book()'s own account-
        # recreation behavior, and _other_occ_date() is always >= today+7,
        # so it's safely future regardless of wall-clock day/time.
        #
        # 2026-07-09 fix: switched to datetime.now(timezone.utc).date() --
        # same UTC-vs-local-midnight mismatch as the two tests above; local
        # date.today() can run a day ahead of admin_overview()'s own UTC
        # cutoff in any timezone ahead of UTC, right around local midnight.
        email = "comeback-guest@example.org"
        user, environ = self._login_as_guest(email)
        self._import_past(user.user_id, datetime.now(timezone.utc).date().isoformat(), "comeback-guest-original-reg")
        erase_user_by_email(self.store, self.settings, email)

        # Same email books again post-erasure, for a DIFFERENT date --
        # brand-new live user_id.
        self._book(email, name="ComebackGuest", occ_date=self._other_occ_date())
        live_user = self.store.find_user_by_email(email)

        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)

        # Both registrations SHOW as the live account's own -- the archived
        # row's user_id is relabeled for display only, so there's no more
        # separate "[erased]"/hashed row shown for this email.
        self.assertNotIn("[erased]", body)
        self.assertEqual(body.count(f"<td>{email}</td>"), 2)
        # true combined "Times booked": 2 total (pre- + post-erasure), but
        # only 1 up to today -- the post-erasure rebooking is for a FUTURE
        # occurrence (_other_occ_date()), so it doesn't count yet.
        self.assertIn("<td>1/2</td>", body)
        # Nothing was actually written: the live account still only has ITS
        # OWN one row on disk, and the pre-erasure row is still archived.
        self.assertEqual(len(self.store.registrations_for_user(live_user.user_id)), 1)
        self.assertEqual(len(self.store.read_registrations(scope="archived")), 1)

    def test_admin_overview_merge_is_idempotent_on_repeated_loads(self):
        # A second GET right after the first must show the exact same
        # merged view -- it's recomputed fresh every time (nothing is ever
        # persisted), so "idempotent" here just means stable across reloads.
        email = "comeback-guest2@example.org"
        self._login_as_guest(email)
        self._book(email, name="ComebackGuest2")
        erase_user_by_email(self.store, self.settings, email, today=date.fromisoformat(self.occ_date))
        self._book(email, name="ComebackGuest2", occ_date=self._other_occ_date())
        live_user = self.store.find_user_by_email(email)

        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, first_body = self.app.admin_overview("GET", admin_environ)
        _status, _headers, second_body = self.app.admin_overview("GET", admin_environ)

        self.assertEqual(first_body, second_body)
        self.assertEqual(len(self.store.registrations_for_user(live_user.user_id)), 1)
        self.assertEqual(len(self.store.read_registrations(scope="archived")), 1)
        self.assertNotIn("[erased]", second_body)

    def test_admin_overview_merge_drops_a_row_that_would_duplicate_the_live_account(self):
        # 2026-07-10, the operator's own real bug report: erasing an account with
        # a canceled booking for some date, then rebooking (and again
        # canceling) that SAME date under a fresh account with the same
        # email, then merging the old archived history back in used to
        # leave TWO rows for the same course+date -- "it should not be
        # possible to get 2 rows for the same course, same email and same
        # slot/date... here the problem might be the ARCHIVE as the 2nd row
        # was archived!" The archived row is now dropped on merge instead
        # of duplicated whenever the live account already has its own row
        # for that exact course+date.
        email = "comeback-guest3@example.org"
        self._login_as_guest(email)
        self._book(email, name="ComebackGuest3")  # same self.occ_date both times
        erase_user_by_email(self.store, self.settings, email, today=date.fromisoformat(self.occ_date))
        self._book(email, name="ComebackGuest3")
        live_user = self.store.find_user_by_email(email)

        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)

        # Only ONE row survives for this course+date -- not two.
        regs = self.store.registrations_for_user(live_user.user_id)
        self.assertEqual(len(regs), 1)
        self.assertEqual(body.count(f"<td>{email}</td>"), 1)

    def test_admin_overview_cancel_button_opens_dialog_with_reason_field(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        cancel_id = f"admin-cancel-{reg.registration_id}"
        self.assertIn(f'<dialog id="{cancel_id}-dialog" class="card">', body)
        self.assertIn("Are you sure?", body)
        self.assertIn(f'<textarea name="message" rows="2" class="big-input" form="{cancel_id}-form">', body)
        self.assertIn("Confirm cancellation", body)
        # 2026-07-10, the operator: "Please add the email address in parenthesis
        # behind the name here (and for reinstate)" -- lets the admin
        # confirm WHICH account with that name they're about to act on.
        self.assertIn("Regular</b> (regular@example.org)", body)

    # -- /admin/cancel: host-initiated, must also notify both sides --------

    def test_admin_cancel_notifies_both_sides_with_message(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self.sent_emails.clear()
        self._post_with_session(
            self.app.admin_cancel, (reg.registration_id,), {"message": "course canceled this week"}, admin_environ
        )
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, "canceled_by_host")
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)
        participant_mail = next(b for t, s2, b in self.sent_emails if t == "regular@example.org" and s2.startswith("Canceled:"))
        self.assertIn("The host canceled this booking:", participant_mail)
        # 2026-07-09, the operator (b): host-initiated cancels label the message
        # from the ATTENDEE's point of view -- it came from the host.
        self.assertIn("Message from the host: course canceled this week", participant_mail)
        # 2026-07-09, the operator (c): no reinstate link at all for a host-
        # initiated cancel's participant copy.
        self.assertNotIn("/reinstate/", participant_mail)
        admin_mail = next(b for t, s2, b in self.sent_emails if t == "admin@example.org" and s2.startswith("Canceled:"))
        # 2026-07-09, the operator (a): "I am the host, so the email should not say
        # YOU canceled the meeting!!" -- the admin copy must name WHO was
        # canceled, not just say "You".
        self.assertIn("You canceled Regular <regular@example.org>'s booking:", admin_mail)

    def test_admin_cancel_redirects_to_admin_after_post(self):
        # 2026-07-10 fix, real incident: the admin overview's own Cancel
        # button POSTs straight to /admin/cancel/<reg_id>, and this used to
        # respond 200 directly on that same URL instead of redirecting --
        # so a browser back-button + resubmit (or a reload) could replay
        # the cancellation. Now redirects (Post/Redirect/Get), same as
        # my_cancel/my_delete_account/my_logout already do.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        status, headers, _body = self._post_with_session(
            self.app.admin_cancel, (reg.registration_id,), {"message": ""}, admin_environ
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin")

    def test_admin_cancel_redirect_preserves_past_query_param(self):
        # The admin table's own Cancel form carries a hidden past=1 field
        # when reached from the "include past" view, so canceling from
        # there doesn't silently drop the admin back to today+future only.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        status, headers, _body = self._post_with_session(
            self.app.admin_cancel, (reg.registration_id,), {"message": "", "past": "1"}, admin_environ
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin?past=1")

    def test_admin_cancel_resubmission_does_not_send_duplicate_emails(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self._post_with_session(self.app.admin_cancel, (reg.registration_id,), {"message": ""}, admin_environ)
        self.sent_emails.clear()
        self._post_with_session(self.app.admin_cancel, (reg.registration_id,), {"message": ""}, admin_environ)
        self.assertEqual(self.sent_emails, [])

    # -- 2026-07-13: /admin "cancel entire session" checkbox ----------------

    def test_admin_overview_row_shows_cancel_entire_session_checkbox_with_participants(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        self._login_as_guest("second@example.org")
        self._book("second@example.org", name="Second", occ_date=self.occ_date)
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        self.assertIn('class="cancel-entire-checkbox"', body)
        self.assertIn(f'data-occurrence="yoga-class-1|{self.occ_date}"', body)
        self.assertIn("Regular (regular@example.org)", body)
        self.assertIn("Second (second@example.org)", body)
        self.assertIn("2 participant(s) will be notified", body)
        # The wiring script (shared, module-level -- one CSP hash for every
        # page/row) must actually be loaded on this page.
        self.assertIn("ownButton", body)

    def test_admin_overview_disabled_row_has_no_cancel_entire_session_checkbox(self):
        # A past/already-canceled row's own Cancel button is disabled --
        # offering "cancel the entire session" from it wouldn't make sense
        # either (see admin_overview()'s own comment).
        self._login_as_guest("regular@example.org")
        self._import_past(self.store.find_user_by_email("regular@example.org").user_id, "2026-01-01", "past-reg")
        admin_sid = webapp._new_session({"kind": "admin"})
        environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", environ)
        # "cancel-entire-checkbox" alone would also match the always-loaded
        # wiring script (_CANCEL_ENTIRE_SESSION_SCRIPT) -- check for the
        # actual per-row <input> instead, which is only rendered when the
        # row's own Cancel button is enabled.
        self.assertNotIn('name="cancel_entire_session"', body)

    def test_admin_cancel_with_checkbox_cancels_every_registration_on_the_occurrence(self):
        user1, _ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        user2, _ = self._login_as_guest("second@example.org")
        self._book("second@example.org", name="Second", occ_date=self.occ_date)
        reg1 = self.store.registrations_for_user(user1.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self.sent_emails.clear()

        self._post_with_session(
            self.app.admin_cancel, (reg1.registration_id,),
            {"message": "venue flooded", "cancel_entire_session": "1"}, admin_environ,
        )

        for user in (user1, user2):
            reg = self.store.registrations_for_user(user.user_id)[0]
            self.assertEqual(reg.status, "canceled_by_host")
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("second@example.org", to_addrs)
        participant_mail = next(b for t, s2, b in self.sent_emails if t == "regular@example.org" and s2.startswith("Canceled:"))
        self.assertIn("Message from the host: venue flooded", participant_mail)

    def test_admin_cancel_without_checkbox_still_only_cancels_the_one_row(self):
        # Regression guard for the new branch in admin_cancel(): absent (or
        # any value other than "1") must behave exactly as before -- only
        # the single targeted registration_id is touched.
        user1, _ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        user2, _ = self._login_as_guest("second@example.org")
        self._book("second@example.org", name="Second", occ_date=self.occ_date)
        reg1 = self.store.registrations_for_user(user1.user_id)[0]
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}

        self._post_with_session(self.app.admin_cancel, (reg1.registration_id,), {"message": ""}, admin_environ)

        self.assertEqual(self.store.registrations_for_user(user1.user_id)[0].status, "canceled_by_host")
        reg2 = self.store.registrations_for_user(user2.user_id)[0]
        self.assertIn(reg2.status, ("confirmed", "waitlisted"))

    # -- /my/reinstate + /admin/reinstate: undo a cancellation --------------
    # 2026-07-10, the operator: "there should be then a reschedule button for
    # canceled meetings which time (WHEN) is in the future" -- clarified in
    # discussion that this means undoing the cancel for the SAME occurrence
    # (not moving to a different one), offered both on the guest's own /my
    # page and, per the operator's follow-up ("ah yes true! (accidental error for
    # the admin could be use case!)"), on /admin too.

    def test_my_bookings_table_shows_reinstate_button_for_a_future_canceled_booking(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn(f'<form method="post" action="/my/reinstate/{reg.registration_id}"', body)
        self.assertIn("Rebook", body)

    def test_my_bookings_reinstate_button_opens_dialog_with_message_field(self):
        # 2026-07-10, the operator: "Reinstate should, LIKE CANCEL, also ask for a
        # COMMENT to be sent with the email to the other."
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        _status, _headers, body = self.app.my("GET", environ)
        reinstate_id = f"reinstate-{reg.registration_id}"
        self.assertIn(f'<dialog id="{reinstate_id}-dialog" class="card">', body)
        self.assertIn(f'<textarea name="message" rows="2" class="big-input" form="{reinstate_id}-form">', body)
        self.assertIn("Confirm rebooking", body)

    def test_my_bookings_table_has_no_reinstate_button_before_canceling(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertNotIn("/my/reinstate/", body)
        self.assertNotIn(">Rebook<", body)

    def test_my_bookings_table_has_no_reinstate_button_for_a_host_canceled_booking(self):
        # 2026-07-14, the operator (screenshot of a "Canceled by host" row still
        # showing this button): "a meeting that was canceled by HOST
        # should NOT have a reinstate button." A host cancellation means
        # the session itself isn't happening -- a guest reinstating
        # themselves can't undo that.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.store.cancel(reg.registration_id, canceled_by="host")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertNotIn("/my/reinstate/", body)
        self.assertNotIn(">Rebook<", body)

    def test_my_reinstate_is_a_no_op_for_a_host_canceled_booking(self):
        # Server-side guard matching the button-hiding above -- a crafted/
        # replayed POST must not be able to reinstate a host cancellation
        # just because the button is hidden.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.store.cancel(reg.registration_id, canceled_by="host")
        self._post_with_session(self.app.my_reinstate, (reg.registration_id,), {"message": ""}, environ)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

    def test_my_cancel_without_a_session_redirects_to_login_instead_of_403(self):
        # 2026-07-14, the operator: "Can the page please redirect to login when
        # the session times out?" -- covers every guest-action endpoint
        # that used to return a bare "403 Forbidden"/"log in first".
        status, headers, _body = self._post_with_session(
            self.app.my_cancel, ("bogus-reg-id",), {"message": ""}, {},
        )
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_my_reinstate_without_a_session_redirects_to_login_instead_of_403(self):
        status, headers, _body = self._post_with_session(
            self.app.my_reinstate, ("bogus-reg-id",), {"message": ""}, {},
        )
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_my_delete_account_without_a_session_redirects_to_login_instead_of_403(self):
        status, headers, _body = self._post_with_session(self.app.my_delete_account, (), {}, {})
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/my"), headers)

    def test_my_reinstate_includes_the_optional_comment_in_both_emails(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.sent_emails.clear()
        self._post_with_session(
            self.app.my_reinstate, (reg.registration_id,), {"message": "sorry, changed my mind"}, environ
        )
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("Message: sorry, changed my mind", participant_mail)
        # 2026-07-08, the operator: same participant-only "Dear NAME," greeting as
        # the cancellation email -- the admin copy right below stays bare.
        self.assertTrue(participant_mail.startswith("Dear Regular,\n\n"))
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Rebooked:"))
        self.assertIn("Message: sorry, changed my mind", admin_mail)
        self.assertFalse(admin_mail.startswith("Dear"))

    def test_my_reinstate_without_a_comment_omits_the_message_line(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.sent_emails.clear()
        self._post_with_session(self.app.my_reinstate, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertNotIn("Message:", participant_mail)

    def test_my_reinstate_confirms_again_when_capacity_allows(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CANCELED_BY_GUEST)
        self.sent_emails.clear()
        status, headers, _body = self._post_with_session(
            self.app.my_reinstate, (reg.registration_id,), {}, environ
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("You rebooked this booking", participant_mail)
        self.assertIn("you're confirmed again", participant_mail)

    def test_my_reinstate_waitlists_when_capacity_is_now_taken(self):
        # self.settings' yoga-class-1 course has capacity=1 (see setUp).
        user1, env1 = self._login_as_guest("first@example.org", name="First")
        self._book("first@example.org", name="First")
        user2, env2 = self._login_as_guest("second@example.org", name="Second")
        self._book("second@example.org", name="Second")
        reg1 = self.store.registrations_for_user(user1.user_id)[0]
        reg2 = self.store.registrations_for_user(user2.user_id)[0]
        self.assertEqual(reg1.status, STATUS_CONFIRMED)
        self.assertEqual(reg2.status, STATUS_WAITLISTED)
        # Canceling the confirmed spot auto-promotes the waitlisted guest.
        self._post_with_session(self.app.my_cancel, (reg1.registration_id,), {"message": ""}, env1)
        self.assertEqual(self.store.find_by_id(reg2.registration_id).status, STATUS_CONFIRMED)
        self.sent_emails.clear()
        self._post_with_session(self.app.my_reinstate, (reg1.registration_id,), {}, env1)
        self.assertEqual(self.store.find_by_id(reg1.registration_id).status, STATUS_WAITLISTED)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "first@example.org" and s.startswith("Rebooked:"))
        self.assertIn("you're back on the waitlist", participant_mail)

    def test_my_reinstate_ignores_someone_elses_registration(self):
        owner, owner_environ = self._login_as_guest("owner@example.org")
        self._book("owner@example.org", name="Owner")
        reg = self.store.registrations_for_user(owner.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, owner_environ)
        _other, other_environ = self._login_as_guest("other@example.org", name="Other")
        self._post_with_session(self.app.my_reinstate, (reg.registration_id,), {}, other_environ)
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CANCELED_BY_GUEST)

    def test_my_reinstate_does_nothing_for_a_past_occurrence(self):
        user, environ = self._login_as_guest("regular@example.org")
        reg = self.store.add_registration("yoga-class-1", "2020-01-01", user.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="guest")
        self._post_with_session(self.app.my_reinstate, (reg.registration_id,), {}, environ)
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CANCELED_BY_GUEST)

    def test_admin_reinstate_requires_admin_session(self):
        status, headers, _body = self.app.admin_reinstate("POST", "nonexistent-id", {"CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")})
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin/login")

    def test_admin_overview_shows_reinstate_button_for_future_canceled_row(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        self.assertIn(f'<form method="post" action="/admin/reinstate/{reg.registration_id}"', body)
        reinstate_id = f"admin-reinstate-{reg.registration_id}"
        self.assertIn(f'<dialog id="{reinstate_id}-dialog" class="card">', body)
        self.assertIn(f'<textarea name="message" rows="2" class="big-input" form="{reinstate_id}-form">', body)
        self.assertIn("Regular</b> (regular@example.org)", body)

    def test_admin_reinstate_includes_the_optional_comment_in_both_emails(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self.sent_emails.clear()
        self._post_with_session(
            self.app.admin_reinstate, (reg.registration_id,), {"message": "welcome back"}, admin_environ
        )
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("Message: welcome back", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Rebooked:"))
        self.assertIn("Message: welcome back", admin_mail)

    def test_admin_reinstate_confirms_again_and_notifies_both_sides(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self.sent_emails.clear()
        status, headers, _body = self._post_with_session(
            self.app.admin_reinstate, (reg.registration_id,), {}, admin_environ
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin")
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("The host rebooked this booking", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Rebooked:"))
        self.assertIn("You rebooked this booking", admin_mail)

    def test_admin_reinstate_redirect_preserves_past_query_param(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        status, headers, _body = self._post_with_session(
            self.app.admin_reinstate, (reg.registration_id,), {"past": "1"}, admin_environ
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin?past=1")

    def test_admin_reinstate_does_nothing_for_a_past_occurrence(self):
        user, environ = self._login_as_guest("regular@example.org")
        reg = self.store.add_registration("yoga-class-1", "2020-01-01", user.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="host")
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}"}
        self._post_with_session(self.app.admin_reinstate, (reg.registration_id,), {}, admin_environ)
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CANCELED_BY_HOST)

    # -- /reinstate/<token>: no-login "magic link" from the cancellation ---
    # email (2026-07-10, the operator: "for /my and /admin ... this POPUP should
    # be used ... Only from the email there will be a single page for
    # this ... WHAT, WHEN, WHERE like in the confirmation email").

    def test_guest_reinstate_page_shows_recap_and_message_field(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        token = new_token()
        self.store.cancel(reg.registration_id, canceled_by="guest", reinstate_token_hash=hash_token(token))
        _status, _headers, body = self.app.guest_reinstate("GET", token, {})
        self.assertIn("Dynamic Ashtanga Vinyasa Yoga", body)
        self.assertIn("17h15 - 18h55", body)
        self.assertIn("Example Community Gym, Room 1", body)
        self.assertIn('<textarea name="message" rows="2" class="big-input">', body)
        self.assertIn("Yes, rebook it", body)
        # 2026-07-11, the operator: audit of every single-submit-button direct-link page.
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_guest_reinstate_invalid_token_shows_invalid_message(self):
        _status, _headers, body = self.app.guest_reinstate("GET", "not-a-real-token", {})
        self.assertIn("invalid or already used", body)

    def test_guest_reinstate_token_reuse_shows_invalid_and_sends_no_duplicate_emails(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        token = participant_mail.split("/reinstate/")[1].split("\n")[0].strip()
        self._post(self.app.guest_reinstate, (token,), {"message": ""})
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)
        self.sent_emails.clear()
        # Token was for the CANCELED status -- now that it's reinstated
        # (confirmed), the same token no longer matches anything.
        _status, _headers, body = self.app.guest_reinstate("GET", token, {})
        self.assertIn("invalid or already used", body)
        self._post(self.app.guest_reinstate, (token,), {"message": ""})
        self.assertEqual(self.sent_emails, [])

    def test_guest_reinstate_email_includes_the_optional_comment(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        token = participant_mail.split("/reinstate/")[1].split("\n")[0].strip()
        self.sent_emails.clear()
        self._post(self.app.guest_reinstate, (token,), {"message": "sorry, my mistake"})
        reinstated_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("Message: sorry, my mistake", reinstated_mail)

    def test_guest_reinstate_rejects_a_past_occurrence(self):
        user, environ = self._login_as_guest("regular@example.org")
        reg = self.store.add_registration("yoga-class-1", "2020-01-01", user.user_id, hash_token(new_token()))
        token = new_token()
        self.store.cancel(reg.registration_id, canceled_by="guest", reinstate_token_hash=hash_token(token))
        _status, _headers, body = self.app.guest_reinstate("GET", token, {})
        self.assertIn("can no longer be rebooked", body)
        self._post(self.app.guest_reinstate, (token,), {"message": ""})
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CANCELED_BY_GUEST)

    # -- /host-reinstate/<reg_id>: no-login "magic link" from the admin's --
    # own copy of the cancellation email.

    def test_host_reinstate_needs_no_admin_session(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.store.cancel(reg.registration_id, canceled_by="host")
        status, _headers, body = self.app.host_reinstate("GET", reg.registration_id, {})
        self.assertIn("200", status)
        self.assertNotIn("/admin/login", status + str(_headers))
        self.assertIn("Dynamic Ashtanga Vinyasa Yoga", body)
        # 2026-07-11, the operator: audit of every single-submit-button direct-link page.
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_host_reinstate_invalid_id_shows_invalid_message(self):
        _status, _headers, body = self.app.host_reinstate("GET", "no-such-id", {})
        self.assertIn("invalid", body)

    def test_host_reinstate_confirms_again_and_notifies_both_sides(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.store.cancel(reg.registration_id, canceled_by="host")
        self.sent_emails.clear()
        status, _headers, body = self._post(self.app.host_reinstate, (reg.registration_id,), {"message": "welcome back"})
        self.assertIn("200", status)
        self.assertEqual(self.store.find_by_id(reg.registration_id).status, STATUS_CONFIRMED)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Rebooked:"))
        self.assertIn("The host rebooked this booking", participant_mail)
        self.assertIn("Message: welcome back", participant_mail)

    def test_host_reinstate_rejects_a_past_occurrence(self):
        user, environ = self._login_as_guest("regular@example.org")
        reg = self.store.add_registration("yoga-class-1", "2020-01-01", user.user_id, hash_token(new_token()))
        self.store.cancel(reg.registration_id, canceled_by="host")
        _status, _headers, body = self.app.host_reinstate("GET", reg.registration_id, {})
        self.assertIn("can no longer be rebooked", body)

    # -- /host-cancel: no-login "magic link" from the calendar event -------

    def test_host_cancel_needs_no_admin_session(self):
        # 2026-07-09, the operator, screenshot of being bounced to /admin/login:
        # "instead it should be a magic link that does not need a password."
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        status, _headers, body = self.app.host_cancel("GET", reg.registration_id, {})
        self.assertIn("200", status)
        self.assertNotIn("/admin/login", status + str(_headers))

    def test_host_cancel_confirm_page_shows_what_where_when_and_reason_field(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        _status, _headers, body = self.app.host_cancel("GET", reg.registration_id, {})
        self.assertIn("What:</b>", body)
        self.assertIn("Where:</b>", body)
        self.assertIn("When:</b>", body)
        self.assertIn('<textarea name="message"', body)
        self.assertIn("Confirm cancellation", body)
        # 2026-07-11, the operator (screenshot of this exact page): "please add a
        # 'Never mind' button also here that brings you back to the homepage!"
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_host_cancel_notifies_both_sides_with_reason(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post(self.app.host_cancel, (reg.registration_id,), {"message": "instructor is sick"})
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, "canceled_by_host")
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)
        participant_mail = next(b for t, s2, b in self.sent_emails if t == "regular@example.org" and s2.startswith("Canceled:"))
        self.assertIn("The host canceled this booking:", participant_mail)
        self.assertIn("Message from the host: instructor is sick", participant_mail)

    def test_host_cancel_unknown_registration_is_404_not_redirect(self):
        status, _headers, body = self.app.host_cancel("GET", "00000000-0000-0000-0000-000000000000", {})
        self.assertIn("404", status)

    def test_host_cancel_resubmission_does_not_send_duplicate_emails(self):
        # 2026-07-10 fix -- a magic link like this one can plausibly be
        # tapped twice (slow first tap + retry) even without a browser
        # back-button involved.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post(self.app.host_cancel, (reg.registration_id,), {"message": ""})
        self.sent_emails.clear()
        self._post(self.app.host_cancel, (reg.registration_id,), {"message": ""})
        self.assertEqual(self.sent_emails, [])

    # -- 2026-07-13: host_cancel_occurrence -- "cancel the entire session" --

    def test_host_cancel_occurrence_needs_no_admin_session(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        status, _headers, body = self.app.host_cancel_occurrence("GET", "yoga-class-1", self.occ_date, {})
        self.assertIn("200", status)
        self.assertNotIn("/admin/login", status + str(_headers))

    def test_host_cancel_occurrence_confirm_page_lists_every_participant(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        self._login_as_guest("second@example.org")
        self._book("second@example.org", name="Second")
        _status, _headers, body = self.app.host_cancel_occurrence("GET", "yoga-class-1", self.occ_date, {})
        self.assertIn("Cancel <b>EVERY</b> registration", body)
        self.assertIn("Regular (regular@example.org)", body)
        self.assertIn("Second (second@example.org)", body)
        self.assertIn('<textarea name="message"', body)
        self.assertIn("Confirm -- cancel entire session", body)
        self.assertIn('href="/" class="link-button">Never mind</a>', body)

    def test_host_cancel_occurrence_nobody_booked_is_not_an_error(self):
        # Unlike host_cancel()'s own registration_id (invalid = 404), an
        # occurrence with nothing live on it is a perfectly normal state
        # (e.g. the link tapped twice) -- not an invalid link.
        status, _headers, body = self.app.host_cancel_occurrence("GET", "yoga-class-1", self.occ_date, {})
        self.assertIn("200", status)
        self.assertIn("nothing to cancel", body)

    def test_host_cancel_occurrence_unknown_course_is_404(self):
        status, _headers, body = self.app.host_cancel_occurrence("GET", "no-such-course", self.occ_date, {})
        self.assertIn("404", status)

    def test_host_cancel_occurrence_cancels_and_notifies_every_participant(self):
        user1, _ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        user2, _ = self._login_as_guest("second@example.org")
        self._book("second@example.org", name="Second", occ_date=self.occ_date)
        self.sent_emails.clear()

        self._post(self.app.host_cancel_occurrence, ("yoga-class-1", self.occ_date), {"message": "venue flooded"})

        for user in (user1, user2):
            reg = self.store.registrations_for_user(user.user_id)[0]
            self.assertEqual(reg.status, "canceled_by_host")
        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("regular@example.org", to_addrs)
        self.assertIn("second@example.org", to_addrs)
        participant_mail = next(b for t, s2, b in self.sent_emails if t == "regular@example.org" and s2.startswith("Canceled:"))
        self.assertIn("Message from the host: venue flooded", participant_mail)
        self.assertIn("exception rather than the rule", participant_mail)
        # 2026-07-09, the operator (c): no reinstate link for a host-initiated
        # cancel's participant copy, even for the whole-occurrence path.
        self.assertNotIn("/reinstate/", participant_mail)

    def test_host_cancel_occurrence_resubmission_does_not_send_duplicate_emails(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        self._post(self.app.host_cancel_occurrence, ("yoga-class-1", self.occ_date), {"message": ""})
        self.sent_emails.clear()
        self._post(self.app.host_cancel_occurrence, ("yoga-class-1", self.occ_date), {"message": ""})
        self.assertEqual(self.sent_emails, [])

    def test_host_cancel_occurrence_route_is_wired_up(self):
        self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        status, _headers, body = self.app.route(
            "GET", f"/host-cancel-occurrence/yoga-class-1/{self.occ_date}", {}
        )
        self.assertIn("200", status)
        self.assertIn("Cancel <b>EVERY</b> registration", body)

    # -- 2026-07-06: past-3 cap, "New booking", homepage link --------------

    def _import_past(self, user_id: str, occurrence_date: str, registration_id: str, status=STATUS_CONFIRMED):
        self.store.import_historical_registration(
            registration_id=registration_id,
            course_shortname="yoga-class-1",
            occurrence_date=occurrence_date,
            user_id=user_id,
            status=status,
            registered_at=f"{occurrence_date}T00:00:00",
        )

    def test_past_bookings_capped_at_three_most_recent(self):
        user, environ = self._login_as_guest("regular@example.org")
        for i, d in enumerate(["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]):
            self._import_past(user.user_id, d, f"past-{i}")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("Past (most recent 3)", body)
        # Most recent 3 by date: 2026-05-01, 2026-04-01, 2026-03-01
        self.assertIn("2026-05-01", body)
        self.assertIn("2026-04-01", body)
        self.assertIn("2026-03-01", body)
        self.assertNotIn("2026-02-01", body)
        self.assertNotIn("2026-01-01", body)

    def test_upcoming_bookings_are_never_capped(self):
        user, environ = self._login_as_guest("regular@example.org")
        # book() only offers real future occurrences -- seed several
        # confirmed upcoming rows directly instead, same as the past ones,
        # just with future dates (any date >= "today" counts as upcoming).
        for i, d in enumerate(["2027-01-01", "2027-02-01", "2027-03-01", "2027-04-01"]):
            self._import_past(user.user_id, d, f"future-{i}")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("<h3>Upcoming</h3>", body)
        for d in ["2027-01-01", "2027-02-01", "2027-03-01", "2027-04-01"]:
            self.assertIn(d, body)

    def test_no_past_bookings_shows_a_friendly_message(self):
        # 2026-07-09 behavior change, the operator: "What about PAST meetings?",
        # asked while looking at an account with no bookings at all -- the
        # Past section used to be omitted entirely when empty, which looked
        # indistinguishable from broken/missing rather than genuinely
        # empty. Now mirrors Upcoming's own empty-state message exactly.
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")  # one upcoming booking only
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("Past (most recent", body)
        self.assertIn("You have no past bookings.", body)

    def test_no_upcoming_bookings_shows_a_friendly_message(self):
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("You have no upcoming bookings.", body)

    def test_my_page_has_new_booking_button_linking_to_courses(self):
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<a href="/courses">', body)
        self.assertIn("New booking", body)

    def test_my_page_no_longer_has_its_own_separate_homepage_link(self):
        # 2026-07-09, the operator: "Now we can get rid of the ugly green
        # sentence behind New bookings as we have https://booking.example.org in
        # the top-bar" -- the banner's own homepage link (see
        # test_my_page_shows_the_same_session_banner_as_courses_and_book
        # below) replaces this dedicated new-tab link.
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertNotIn(f'<a href="{self.settings.base_url}" target="_blank"', body)
        self.assertNotIn("opens in a new tab", body)

    def test_my_page_shows_the_same_session_banner_as_courses_and_book(self):
        # 2026-07-09, the operator: "Rather use the BANNER as here to be
        # CONSISTENT!!" -- /my now shows the same _session_banner_html()
        # banner /courses and /book already show, instead of a separate,
        # redundant "Log out" button at the bottom of the page.
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("regular@example.org", body)

    def test_my_pages_own_banner_omits_the_my_bookings_link(self):
        # 2026-07-09, the operator, screenshot of /my's own banner: "My bookings
        # link on the my bookings page (in top-bar) :(" -- a link back to
        # the exact page you're already on is dead weight. /courses and
        # /book still show it (see SessionBannerTest below).
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        banner = body[body.index('class="session-banner"'):body.index("</div>")]
        self.assertNotIn(">My bookings<", banner)
        self.assertIn(self.settings.base_url, banner)  # homepage + Log out still there

    def test_my_page_bottom_row_has_no_log_out_button_or_delete_account_button(self):
        # The banner's own Logout replaces the old standalone "Log out"
        # button here. 2026-07-14, the operator: "please move the delete button
        # under 'Account settings': and rename to 'DELETE this account'"
        # -- the destructive delete-account action moved to /my/settings
        # (see MySettingsTest), so /my's own bottom row no longer has
        # either button.
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        bottom = body.split("<h3>Past")[1]
        self.assertNotIn(">Log out<", bottom)
        self.assertNotIn("Delete my account", bottom)
        self.assertNotIn("delete-account-form", bottom)


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
        self._keys = [f"admin:203.0.113.{i}" for i in range(1, 6)]
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

    def test_lockout_shows_a_disabled_button_with_a_live_countdown(self):
        # 2026-07-05: the operator asked for a visible countdown wherever there's
        # a login cooldown, matching the resend-button UX -- the button
        # itself is disabled server-side (not just cosmetically) via a
        # server-computed (not guessed) remaining-seconds value.
        ip = "203.0.113.5"
        for _ in range(5):
            self._post("wrong", forwarded_for=ip)
        body = self._post("wrong", forwarded_for=ip)
        self.assertIn("Too many attempts", body)
        self.assertIn('id="admin-login-btn"', body)
        self.assertIn("btn.disabled = true;", body)
        self.assertIn("Log in", body)

    def test_lockout_script_is_byte_stable_across_different_remaining_seconds(self):
        # 2026-07-07, the operator (repeatable console CSP violation on /my's
        # lockout screen, confirmed via two screenshots showing DIFFERENT
        # hashes for what should be "the same" script): the countdown
        # script used to interpolate `seconds` directly into the <script>
        # text, so its hash changed every render and could never match a
        # fixed CSP allow-list. The seconds value must now travel via a
        # data-* attribute instead, so the <script> block itself never
        # changes no matter how much lockout time is left.
        import re

        def lockout_script(body: str) -> str:
            m = re.search(r"<script>\s*\(function\(\) \{\s*var btn = document\.querySelector\(\"\[data-lockout-btn\]\"\).*?</script>", body, re.DOTALL)
            self.assertIsNotNone(m)
            return m.group(0)

        ip_a, ip_b = "203.0.113.6", "203.0.113.7"
        for ip in (ip_a, ip_b):
            for _ in range(5):
                self._post("wrong", forwarded_for=ip)
        body_a = self._post("wrong", forwarded_for=ip_a)
        body_b = self._post("wrong", forwarded_for=ip_b)
        self.assertEqual(lockout_script(body_a), lockout_script(body_b))
        # But the actual remaining-seconds value is still server-computed
        # and DOES reach the page, just via the button's data attribute.
        self.assertIn("data-lockout-seconds=", body_a)


class MyLoginAsAdminTest(unittest.TestCase):
    """2026-07-06: "/my should accept email: admin and the admin password
    in order to login to the /admin space." -- /my's login form gains this
    as an extra accepted credential pair, reusing admin_login()'s own
    rate-limiter bucket (per client IP) rather than the guest one, so this
    can't be used to dodge -- or worsen -- either lockout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.admin_password = "correct horse battery staple"
        self.settings = make_settings(admin_password_hash=hash_admin_password(self.admin_password))
        self.app = App(self.settings, self.store)
        self._keys = [f"admin:203.0.113.{i}" for i in range(20, 30)] + ["admin:unknown"]
        for k in self._keys:
            webapp.login_limiter.reset(k)
        self.addCleanup(lambda: [webapp.login_limiter.reset(k) for k in self._keys])

    def _post(self, email: str, password: str, *, forwarded_for: str | None = None, next_path: str | None = None):
        form = {"email": email, "password": password}
        if next_path is not None:
            form["next"] = next_path
        body = urlencode(form).encode()
        environ = {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if forwarded_for is not None:
            environ["HTTP_X_FORWARDED_FOR"] = forwarded_for
        return self.app.my("POST", environ)

    def test_admin_email_with_correct_admin_password_logs_into_admin(self):
        status, headers, _body = self._post("admin", self.admin_password, forwarded_for="203.0.113.20")
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin")
        self.assertTrue(any(h[0] == "Set-Cookie" for h in headers))
        sid = next(h[1] for h in headers if h[0] == "Set-Cookie").split("session=")[1].split(";")[0]
        self.assertEqual(webapp.SESSIONS[sid]["kind"], "admin")

    def test_admin_login_via_my_ignores_next_and_still_goes_to_admin(self):
        # 2026-07-11, the operator: "Login link returns to originating page" --
        # that's a GUEST-only affordance; the admin shortcut (email:
        # "admin") must always land on /admin regardless of any next=
        # a guest-facing page happened to attach to the login link.
        status, headers, _body = self._post(
            "admin", self.admin_password, forwarded_for="203.0.113.21", next_path="/courses",
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/admin")

    def test_admin_email_case_insensitive(self):
        status, _headers, _body = self._post("Admin", self.admin_password, forwarded_for="203.0.113.21")
        self.assertEqual(status, "302 Found")

    def test_admin_email_with_wrong_password_shows_generic_mismatch_error(self):
        _status, _headers, body = self._post("admin", "wrong password", forwarded_for="203.0.113.22")
        self.assertIn("Email and/or password did not match.", body)
        # Never confirm "admin" is special -- same wording a real guest
        # mismatch gets, not admin_login()'s own "Wrong password."
        self.assertNotIn("Wrong password", body)

    def test_no_admin_session_created_on_wrong_password(self):
        # webapp.SESSIONS is a shared module-level dict that other tests in
        # this same process may have already populated -- only check for a
        # NEW session created by this specific call, not global state.
        before = set(webapp.SESSIONS.keys())
        self._post("admin", "wrong password", forwarded_for="203.0.113.23")
        self.assertEqual(set(webapp.SESSIONS.keys()), before)

    def test_shares_admin_logins_rate_limit_bucket_not_the_guest_one(self):
        # Same IP hammering "admin" via /my must trip the exact same
        # per-IP bucket admin_login() itself uses -- proven by exhausting
        # it here and then confirming admin_login() is ALSO locked out.
        ip = "203.0.113.24"
        for _ in range(5):
            self._post("admin", "wrong password", forwarded_for=ip)
        _status, _headers, body = self._post("admin", "wrong password", forwarded_for=ip)
        self.assertIn("Too many attempts", body)

        admin_login_body = f"password=wrong".encode()
        environ = {
            "CONTENT_LENGTH": str(len(admin_login_body)), "wsgi.input": io.BytesIO(admin_login_body),
            "HTTP_X_FORWARDED_FOR": ip,
        }
        _status, _headers, admin_body = self.app.admin_login("POST", environ)
        self.assertIn("Too many attempts", admin_body)

    def test_lockout_here_does_not_affect_a_different_ip(self):
        attacker_ip, admin_ip = "203.0.113.25", "203.0.113.26"
        for _ in range(5):
            self._post("admin", "wrong password", forwarded_for=attacker_ip)
        self._post("admin", "wrong password", forwarded_for=attacker_ip)  # now locked
        # The real admin, from a different IP, is unaffected.
        status, _headers, _body = self._post("admin", self.admin_password, forwarded_for=admin_ip)
        self.assertEqual(status, "302 Found")

    def test_guest_login_still_works_unaffected(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter2222")
        self.store.set_password(user.user_id, h, s)
        status, headers, _body = self._post("regular@example.org", "hunter2222", forwarded_for="203.0.113.27")
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")

    def test_email_field_is_plain_text_input_not_type_email(self):
        # type="email" would block the browser from ever submitting the
        # literal string "admin" (no "@") client-side.
        _status, _headers, body = self.app.my("GET", {})
        self.assertIn('name="email" type="text"', body)


if __name__ == "__main__":
    unittest.main()
