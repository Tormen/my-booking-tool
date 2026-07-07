import io
import json
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from http import cookies
from unittest.mock import patch
from urllib.parse import urlencode

from app import webapp
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
        self.assertIn('<a href="/my">Login</a>', body)

    def test_book_form_shows_banner_when_logged_in(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn('class="session-banner"', body)
        self.assertIn("regular@example.org", body)

    def test_book_form_shows_login_banner_when_anonymous(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn('class="session-banner"', body)
        self.assertIn("Not logged in", body)
        self.assertIn('<a href="/my">Login</a>', body)

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
        # Deliberately NOT given the anonymous "Login" banner -- this page
        # already IS the Login/Sign up form (_my_login_page()), so a
        # "Login" link banner above it would be redundant.
        _status, _headers, body = self.app.my("GET", {})
        self.assertNotIn('class="session-banner"', body)
        self.assertIn('id="my-tab-login"', body)

    # -- 2026-07-09: booking-page name/email prefilled+locked when logged in --

    def test_book_page_prefills_and_locks_name_email_when_logged_in(self):
        environ = self._login_environ("regular@example.org")
        _status, _headers, body = self.app.book("GET", "yoga-class-1", environ)
        self.assertIn('name="name" value="Regular" readonly required', body)
        self.assertIn('name="email" type="email" value="regular@example.org" readonly required', body)
        # Irrelevant once already logged in with a password.
        self.assertNotIn("First time booking with this email?", body)

    def test_book_page_fields_stay_editable_when_anonymous(self):
        _status, _headers, body = self.app.book("GET", "yoga-class-1", {})
        self.assertIn('<input class="big-input" name="name" required>', body)
        self.assertIn('<input class="big-input" name="email" type="email" required>', body)
        # Only the CSS selector "input[readonly]" (always present in the
        # <style> block) should mention "readonly" here -- no actual input
        # tag should have the attribute for an anonymous visitor.
        self.assertNotIn('name="name" value=', body)
        self.assertNotIn('name="email" type="email" value=', body)
        self.assertIn("First time booking with this email?", body)

    def test_book_page_error_retry_keeps_fields_locked_when_logged_in(self):
        # the operator's fields must stay prefilled+readonly even on a re-render
        # after a validation error -- not just the fresh GET.
        environ = self._login_environ("regular@example.org")
        form = {"occurrence_date": self._occ_date(), "name": "Regular", "email": "regular@example.org"}  # no agree
        body_bytes = urlencode(form).encode()
        post_environ = dict(environ, CONTENT_LENGTH=str(len(body_bytes)), **{"wsgi.input": io.BytesIO(body_bytes)})
        _status, _headers, body = self.app.book("POST", "yoga-class-1", post_environ)
        self.assertIn("acknowledge the participation terms", body)
        self.assertIn('name="name" value="Regular" readonly required', body)
        self.assertIn('name="email" type="email" value="regular@example.org" readonly required', body)

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

        def recorder(settings, to, subject, body, html_body=None, ics_attachment=None):
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
        status, _headers, _body = self.app.my_settings("GET", {})
        self.assertEqual(status, "403 Forbidden")

    def test_settings_name_post_requires_login(self):
        status, _headers, _body = self.app.my_settings_name("POST", self._post_environ({}, {"name": "X"}))
        self.assertEqual(status, "403 Forbidden")

    def test_settings_email_post_requires_login(self):
        status, _headers, _body = self.app.my_settings_email(
            "POST", self._post_environ({}, {"email": "x@example.org"})
        )
        self.assertEqual(status, "403 Forbidden")

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
        occs = build_occurrences(
            course, self.settings, datetime.now(timezone.utc),
            lambda sn, d: 0, lambda start, end: False,
        )
        self.occ_date = occs[0].date.isoformat()

        recorder = lambda settings, to, subject, body, html_body=None, ics_attachment=None: self.sent_emails.append((to, subject, body))
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

    def test_returning_unconfirmed_email_adds_another_pending_and_resends(self):
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", name="Alice Again", occ_date=self.occ_date)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(len(self.store.registrations_for_user(user.user_id)), 2)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your example.org account", "Confirm your example.org account"])

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

    def test_confirmed_booking_email_attaches_a_publish_ics(self):
        # 2026-07-09, the operator: "Can you please attach a calendar invite also
        # in the email that is sent to the participant?"
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None):
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

    def test_waitlisted_booking_email_has_no_ics_attachment(self):
        # No confirmed slot yet -- nothing real to add to a calendar.
        for i in range(2):
            user = self.store.upsert_user_for_booking(f"guest{i}@example.org", f"Guest{i}")
            h, s = hash_secret("hunter22")
            self.store.set_password(user.user_id, h, s)
        captured = {}

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None):
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

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None):
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

    def test_my_bookings_table_shows_title_time_location_not_shortname(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn("Dynamic Ashtanga Vinyasa Yoga", body)
        self.assertIn("17h15 - 18h55", body)
        self.assertIn("Example Community Gym, Room 1", body)
        self.assertNotIn("yoga-class-1", body)

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

    def test_my_bookings_delete_account_dialog_has_exact_requested_wording(self):
        _user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertIn('<dialog id="delete-account-dialog" class="card">', body)
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

    def test_my_logout_clears_session_and_redirects_to_my(self):
        _user, environ = self._login_as_guest("regular@example.org")
        sid = cookies.SimpleCookie()
        sid.load(environ["HTTP_COOKIE"])
        session_id = sid["session"].value
        status, headers, _body = self.app.my_logout("POST", environ)
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], "/my")
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
        self.assertIn("Message: can't make it", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Canceled:"))
        self.assertIn("Regular <regular@example.org> canceled this booking:", admin_mail)
        self.assertIn("Message: can't make it", admin_mail)
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", admin_mail)

    def test_my_cancel_email_offers_a_rebook_link_to_the_participant_only(self):
        # 2026-07-10, the operator: "With the reschedule button the email could
        # also contain it: If this was a mistake... The what can be a link
        # to the booking page for this course."
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.sent_emails.clear()
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Canceled:"))
        self.assertIn("If this was a mistake, you can book again here: https://example.org/book/yoga-class-1", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Canceled:"))
        self.assertNotIn("book again", admin_mail)

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
        self.assertIn("Message: car trouble", participant_mail)
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

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None):
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
        user, environ = self._login_as_guest("erased-guest2@example.org")
        self._book("erased-guest2@example.org", name="ErasedGuest2")
        erase_user_by_email(self.store, self.settings, "erased-guest2@example.org", today=date.fromisoformat(self.occ_date))
        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)
        # times-booked column should show 1, not 0, for the erased row
        row_start = body.index("[erased]")
        row_html = body[row_start:row_start + 400]
        self.assertIn("<td>1</td>", row_html)

    def test_admin_overview_auto_merges_pre_erasure_registrations_on_load(self):
        # 2026-07-10, the operator: "the merge should be automatically done if you
        # also display the history in the /admin page" -- if an erased
        # guest books again with the SAME email, book() creates a brand-new
        # live user_id (the old email no longer exists in the live table --
        # it's now a hash on the archived row). Merely LOADING /admin now
        # physically moves that old registration onto the new live user_id
        # (Store.merge_archived_registrations, via app.cli_history.run_merge)
        # -- no separate button/action needed, and no more display-only
        # "(incl. N pre-erasure)" annotation, since the count is now the
        # real thing rather than an estimate.
        email = "comeback-guest@example.org"
        user, environ = self._login_as_guest(email)
        self._book(email, name="ComebackGuest")
        erase_user_by_email(self.store, self.settings, email, today=date.fromisoformat(self.occ_date))

        # Same email books again post-erasure -- brand-new live user_id.
        self._book(email, name="ComebackGuest")
        live_user = self.store.find_user_by_email(email)

        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)

        # Both registrations now resolve to the live account -- the old
        # archived row's user_id was rewritten to the live user_id by the
        # merge, so there's no more separate "[erased]"/hashed row for this
        # email, and no display-only annotation either.
        self.assertNotIn("[erased]", body)
        self.assertNotIn("pre-erasure", body)
        self.assertEqual(body.count(f"<td>{email}</td>"), 2)
        self.assertIn("<td>2</td>", body)  # true combined "Times booked"
        self.assertEqual(len(self.store.registrations_for_user(live_user.user_id)), 2)

    def test_admin_overview_merge_is_idempotent_on_repeated_loads(self):
        # A second GET right after the first must find nothing left to
        # merge and be a pure no-op -- merging is a side effect of a GET
        # here, so it must not do anything surprising on a plain reload.
        email = "comeback-guest2@example.org"
        self._login_as_guest(email)
        self._book(email, name="ComebackGuest2")
        erase_user_by_email(self.store, self.settings, email, today=date.fromisoformat(self.occ_date))
        self._book(email, name="ComebackGuest2")
        live_user = self.store.find_user_by_email(email)

        admin_sid = webapp._new_session({"kind": "admin"})
        admin_environ = {"HTTP_COOKIE": f"session={admin_sid}", "QUERY_STRING": "past=1"}
        self.app.admin_overview("GET", admin_environ)
        _status, _headers, body = self.app.admin_overview("GET", admin_environ)

        self.assertEqual(len(self.store.registrations_for_user(live_user.user_id)), 2)
        self.assertNotIn("[erased]", body)

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
        self.assertIn("Message: course canceled this week", participant_mail)
        admin_mail = next(b for t, s2, b in self.sent_emails if t == "admin@example.org" and s2.startswith("Canceled:"))
        self.assertIn("You canceled this booking:", admin_mail)

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
        self.assertIn("Reinstate", body)

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
        self.assertIn("Confirm reinstatement", body)

    def test_my_bookings_table_has_no_reinstate_button_before_canceling(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        _status, _headers, body = self.app.my("GET", environ)
        self.assertNotIn("/my/reinstate/", body)
        self.assertNotIn(">Reinstate<", body)

    def test_my_reinstate_includes_the_optional_comment_in_both_emails(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.sent_emails.clear()
        self._post_with_session(
            self.app.my_reinstate, (reg.registration_id,), {"message": "sorry, changed my mind"}, environ
        )
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Reinstated:"))
        self.assertIn("Message: sorry, changed my mind", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Reinstated:"))
        self.assertIn("Message: sorry, changed my mind", admin_mail)

    def test_my_reinstate_without_a_comment_omits_the_message_line(self):
        user, environ = self._login_as_guest("regular@example.org")
        self._book("regular@example.org", name="Regular")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self._post_with_session(self.app.my_cancel, (reg.registration_id,), {"message": ""}, environ)
        self.sent_emails.clear()
        self._post_with_session(self.app.my_reinstate, (reg.registration_id,), {"message": ""}, environ)
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Reinstated:"))
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
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Reinstated:"))
        self.assertIn("You reinstated this booking", participant_mail)
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
        participant_mail = next(b for t, s, b in self.sent_emails if t == "first@example.org" and s.startswith("Reinstated:"))
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
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Reinstated:"))
        self.assertIn("Message: welcome back", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Reinstated:"))
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
        participant_mail = next(b for t, s, b in self.sent_emails if t == "regular@example.org" and s.startswith("Reinstated:"))
        self.assertIn("The host reinstated this booking", participant_mail)
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org" and s.startswith("Reinstated:"))
        self.assertIn("You reinstated this booking", admin_mail)

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
        self.assertIn("Message: instructor is sick", participant_mail)

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

    def test_my_page_bottom_row_has_only_the_delete_account_button(self):
        # The banner's own Logout replaces the old standalone "Log out"
        # button here -- only the destructive "Delete my account" action
        # remains in the bottom row now.
        user, environ = self._login_as_guest("regular@example.org")
        _status, _headers, body = self.app.my("GET", environ)
        bottom = body.split("<h3>Past")[1]
        self.assertNotIn(">Log out<", bottom)
        self.assertIn("Delete my account", bottom)


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

    def _post(self, email: str, password: str, *, forwarded_for: str | None = None):
        form = {"email": email, "password": password}
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
