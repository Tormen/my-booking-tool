import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlencode

from app import webapp
from app.caldav_client import CalDAVClient, Response
from app.security import hash_secret
from app.slots import Occurrence, build_occurrences
from app.storage import STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store
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

    def test_subtitle_defaults_to_weekday_and_location(self):
        app = self._app()
        course = make_course(weekday="sat", location="Trier")
        _, _, html = app._book_page(course, [self._occ()])
        self.assertIn('<p class="subtitle">Saturdays -- Trier</p>', html)

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

        patcher = patch(
            "app.webapp.send_mail",
            side_effect=lambda settings, to, subject, body: self.sent_emails.append((to, subject, body)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertEqual(subjects, ["Confirm your account"])

    def test_returning_unconfirmed_email_adds_another_pending_and_resends(self):
        self._book("newguest@example.org", occ_date=self.occ_date)
        self._book("newguest@example.org", name="Alice Again", occ_date=self.occ_date)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(len(self.store.registrations_for_user(user.user_id)), 2)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your account", "Confirm your account"])

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
        h, s = hash_secret("hunter2")
        self.store.set_password(user.user_id, h, s)
        _status, _headers, body = self._book("regular@example.org", name="Regular")
        self.assertIn("Booked!", body)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 1)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertIn("Booking confirmed: Dynamic Ashtanga Vinyasa Yoga on " + self.occ_date, subjects)

    def test_confirmed_account_waitlisted_when_full(self):
        for i in range(2):
            user = self.store.upsert_user_for_booking(f"guest{i}@example.org", f"Guest{i}")
            h, s = hash_secret("hunter2")
            self.store.set_password(user.user_id, h, s)
        self._book("guest0@example.org", name="Guest0")  # capacity=1, fills it
        _status, _headers, body = self._book("guest1@example.org", name="Guest1")
        self.assertIn("waitlist", body)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertTrue(any(s.startswith("Waitlisted:") for s in subjects))

    # -- my_confirm: sets password, promotes pending ------------------------

    def test_my_confirm_invalid_token_shows_error(self):
        _status, _headers, body = self._post(self.app.my_confirm, ("bogus-token",), {"password": "hunter2"})
        self.assertIn("invalid", body.lower())

    def test_my_confirm_sets_password_and_promotes_pending_booking(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, headers, body = self._post(self.app.my_confirm, (token,), {"password": "hunter2"})
        self.assertIn("Account confirmed", body)
        self.assertTrue(any(h[0] == "Set-Cookie" for h in headers))
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertNotEqual(user.password_hash, "")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.assertEqual(reg.status, STATUS_CONFIRMED)
        self.assertEqual(self.store.count_confirmed("yoga-class-1", self.occ_date), 1)
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertIn("Booking confirmed: Dynamic Ashtanga Vinyasa Yoga on " + self.occ_date, subjects)

    def test_my_confirm_recheck_capacity_lands_on_waitlist_if_filled_meanwhile(self):
        # capacity=1: someone else confirms and fills the only spot WHILE
        # the first guest's account is still unconfirmed.
        self._book("newguest@example.org")
        other = self.store.upsert_user_for_booking("other@example.org", "Other")
        h, s = hash_secret("hunter2")
        self.store.set_password(other.user_id, h, s)
        self._book("other@example.org", name="Other")  # instantly confirmed, fills capacity=1

        # other's booking was instant (no confirm email) -- the newguest's
        # confirm link is still the FIRST email ever sent in this test.
        token = self.sent_emails[0][2].split("/my/confirm/")[1].split("\n")[0].strip()
        self._post(self.app.my_confirm, (token,), {"password": "hunter2"})
        user = self.store.find_user_by_email("newguest@example.org")
        reg = self.store.registrations_for_user(user.user_id)[0]
        self.assertEqual(reg.status, STATUS_WAITLISTED)

    def test_my_confirm_rejects_a_too_short_password(self):
        self._book("newguest@example.org")
        token = self._confirm_token_from_last_email()
        _status, _headers, body = self._post(self.app.my_confirm, (token,), {"password": "ab"})
        self.assertIn("at least 4 characters", body)
        user = self.store.find_user_by_email("newguest@example.org")
        self.assertEqual(user.password_hash, "")

    # -- my_reset: unified resend/forgot-password, never leaks existence ---

    def test_my_reset_same_response_whether_or_not_email_exists(self):
        _s1, _h1, body_known = self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        _s2, _h2, body_unknown = self._post(self.app.my_reset, (), {"email": "nobody@example.org"})
        self.assertEqual(body_known, body_unknown)

    def test_my_reset_emails_unconfirmed_account_a_confirm_link(self):
        self._book("newguest@example.org")
        self.sent_emails.clear()
        self._post(self.app.my_reset, (), {"email": "newguest@example.org"})
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Confirm your account"])

    def test_my_reset_emails_confirmed_account_a_reset_link(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter2")
        self.store.set_password(user.user_id, h, s)
        self._post(self.app.my_reset, (), {"email": "regular@example.org"})
        subjects = [s for _, s, _ in self.sent_emails]
        self.assertEqual(subjects, ["Reset your password"])

    # -- /my password login --------------------------------------------------

    def test_my_login_succeeds_with_correct_password(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter2")
        self.store.set_password(user.user_id, h, s)
        _status, headers, _body = self._post(self.app.my, (), {"email": "regular@example.org", "password": "hunter2"})
        self.assertTrue(any(h[0] == "Set-Cookie" for h in headers))

    def test_my_login_fails_with_wrong_password(self):
        user = self.store.upsert_user_for_booking("regular@example.org", "Regular")
        h, s = hash_secret("hunter2")
        self.store.set_password(user.user_id, h, s)
        _status, headers, body = self._post(self.app.my, (), {"email": "regular@example.org", "password": "wrong"})
        self.assertFalse(any(h[0] == "Set-Cookie" for h in headers))
        self.assertIn("Email/password", body)
        self.assertIn("match", body)

    def test_my_login_fails_for_a_still_unconfirmed_account(self):
        self._book("newguest@example.org")
        _status, headers, body = self._post(
            self.app.my, (), {"email": "newguest@example.org", "password": "anything"}
        )
        self.assertFalse(any(h[0] == "Set-Cookie" for h in headers))
        self.assertIn("Email/password", body)
        self.assertIn("match", body)


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
