"""Direct tests of calendar_sync.sync_occurrence's invite body -- the
active/waiting/canceled participant tables (status, name, email,
self/guest, timestamp), and the zero-active removal condition. See
calendar_sync.py's own docstring for what's being tested here."""
import tempfile
import unittest
from datetime import date

from app.caldav_client import CalDAVClient, CalDAVConflictError, Response
from app.calendar_sync import (
    _SYNC_CONFLICT_MAX_ATTEMPTS, event_uid, guest_cancel_ics, guest_invite_ics,
    resync_after_course_rename, sync_occurrence,
)
from app.ics import parse_uid
from app.storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED,
    Store, format_display_timestamp,
)

from .helpers import make_course, make_settings

EMPTY_REPORT = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>"""


def _report_with_event(uid: str, etag: str = '"e1"') -> str:
    return f"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/caldav/Bookings/{uid}.ics</D:href>
    <D:propstat><D:prop>
      <D:getetag>{etag}</D:getetag>
      <C:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:{uid}
DTSTART:20260801T151500Z
DTEND:20260801T165500Z
SUMMARY:Test
END:VEVENT
END:VCALENDAR
</C:calendar-data>
    </D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


class FakeTransport:
    def __init__(self, report_body: str = EMPTY_REPORT, conflicts_before_success: int = 0):
        self.calls = []
        self.report_body = report_body
        # 2026-07-07, the operator (a real production 500 on /my/confirm,
        # root-caused to a stale-ETag CalDAV 412): simulates that race --
        # the first `conflicts_before_success` PUT/DELETE calls return 412
        # Precondition Failed (a stale If-Match), then every one after
        # that succeeds normally, same shape as the real incident (a
        # same-second retry worked).
        self.conflicts_before_success = conflicts_before_success

    def __call__(self, method, url, body="", extra_headers=None):
        self.calls.append((method, url, body, extra_headers or {}))
        if method == "REPORT":
            return Response(207, {}, self.report_body)
        if method in ("PUT", "DELETE"):
            if self.conflicts_before_success > 0:
                self.conflicts_before_success -= 1
                return Response(412, {}, '<D:error xmlns:D="DAV:"/>')
            if method == "PUT":
                return Response(201, {"etag": '"new"'}, "")
            return Response(204, {}, "")
        raise AssertionError(f"unexpected {method} {url}")


class SyncOccurrenceInviteBodyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-class-1", title="Yoga", capacity=2)
        self.settings = make_settings(courses=(self.course,), booking_calendar="Bookings")
        self.occ_date = date(2026, 8, 1)

    def _sync(self, report_body: str = EMPTY_REPORT, conflicts_before_success: int = 0):
        transport = FakeTransport(report_body, conflicts_before_success=conflicts_before_success)
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )
        sync_occurrence(client, "/caldav/Bookings/", self.store, self.settings, self.course, self.occ_date)
        return transport

    def _add(self, email: str, status: str, cancel: bool = False, canceled_by: str = "guest") -> str:
        user = self.store.upsert_user_for_booking(email, email.split("@")[0].title())
        reg = self.store.add_registration(
            "yoga-class-1", self.occ_date.isoformat(), user.user_id, "tok-hash", status=status,
        )
        if cancel:
            self.store.cancel(reg.registration_id, canceled_by=canceled_by)
        return reg.registration_id

    # -- active/waiting: table with name/email/self-guest/registered_at ----

    def test_active_and_waiting_lines_show_name_email_self_and_registered_at(self):
        self.store.upsert_user_for_booking("alice@example.org", "Alice")
        reg = self.store.add_registration(
            "yoga-class-1", self.occ_date.isoformat(),
            self.store.find_user_by_email("alice@example.org").user_id, "tok-hash", status=STATUS_CONFIRMED,
        )
        self.store.upsert_user_for_booking("bob@example.org", "Bob")
        wl = self.store.add_registration(
            "yoga-class-1", self.occ_date.isoformat(),
            self.store.find_user_by_email("bob@example.org").user_id, "tok-hash", status=STATUS_WAITLISTED,
        )
        transport = self._sync()
        put_bodies = [b for m, _u, b, _h in transport.calls if m == "PUT"]
        self.assertEqual(len(put_bodies), 1)
        unfolded = put_bodies[0].replace("\r\n ", "")

        reloaded = self.store.find_by_id(reg.registration_id)
        reloaded_wl = self.store.find_by_id(wl.registration_id)
        self.assertIn("Participants:", unfolded)
        self.assertIn(
            f"- confirmed | Alice | alice@example.org | self | "
            f"registered {format_display_timestamp(reloaded.registered_at)} |",
            unfolded,
        )
        self.assertIn(
            f"- waitlisted #1 | Bob | bob@example.org | self | "
            f"registered {format_display_timestamp(reloaded_wl.registered_at)} |",
            unfolded,
        )

    def test_includes_cancel_entire_session_link_alongside_per_participant_links(self):
        # 2026-07-13, the operator: "the CALDAV invite needs BOTH: cancel link per
        # participant AND the course cancel link for ALL of them" -- a
        # second, always-present link for canceling the whole occurrence at
        # once (app.cancel_flow.cancel_occurrence via
        # app/webapp.py::host_cancel_occurrence), alongside each
        # participant's own "cancel:" line, not instead of it.
        reg_id = self._add("alice@example.org", STATUS_CONFIRMED)
        transport = self._sync()
        put_bodies = [b for m, _u, b, _h in transport.calls if m == "PUT"]
        unfolded = put_bodies[0].replace("\r\n ", "")
        self.assertIn(f"cancel: {self.settings.base_url}/host-cancel/{reg_id}", unfolded)
        self.assertIn(
            f"cancel entire session (all participants): "
            f"{self.settings.base_url}/host-cancel-occurrence/yoga-class-1/{self.occ_date.isoformat()}",
            unfolded,
        )

    def test_guest_row_shows_guest_of_leader_instead_of_self(self):
        leader = self.store.upsert_user_for_booking("leader@example.org", "Leader")
        guest = self.store.upsert_user_for_booking("guest@example.org", "Guest Person")
        self.store.add_party_registrations_checking_capacity(
            "yoga-class-1", self.occ_date.isoformat(),
            [(leader.user_id, "tok-hash-1"), (guest.user_id, "tok-hash-2")], capacity=2,
        )
        transport = self._sync()
        unfolded = [b for m, _u, b, _h in transport.calls if m == "PUT"][0].replace("\r\n ", "")
        self.assertIn("| Leader | leader@example.org | self |", unfolded)
        self.assertIn("| Guest Person | guest@example.org | guest of Leader |", unfolded)

    # -- new: canceled group ------------------------------------------------

    def test_canceled_participant_appears_with_canceled_at_not_registered_at(self):
        # One active (so the event isn't deleted) + one canceled -- the
        # canceled row must show up in its own group with canceled_at, and
        # must NOT be counted among active/waiting.
        self._add("stays@example.org", STATUS_CONFIRMED)
        canceled_id = self._add("left@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="guest")
        transport = self._sync()

        put_bodies = [b for m, _u, b, _h in transport.calls if m == "PUT"]
        self.assertEqual(len(put_bodies), 1)
        unfolded = put_bodies[0].replace("\r\n ", "")

        canceled_reg = self.store.find_by_id(canceled_id)
        self.assertTrue(canceled_reg.canceled_at)  # sanity: a timestamp was actually recorded
        self.assertIn("Canceled:", unfolded)
        self.assertIn("| Left | left@example.org | self |", unfolded)
        self.assertIn(f"canceled {format_display_timestamp(canceled_reg.canceled_at)} by guest", unfolded)
        # Not double-counted as active: exactly one "- confirmed |" line
        # before the Canceled: section (the "stays" guest) -- the canceled
        # guest's line lives only in the Canceled: section, tagged
        # "canceled_by_guest", never as a second "confirmed" line.
        active_section = unfolded.split("Canceled:")[0]
        self.assertIn("Yoga -- 1/2 registered", active_section)
        self.assertEqual(active_section.count("- confirmed |"), 1)
        self.assertNotIn("canceled_by_guest", active_section)

    def test_canceled_by_host_shown_distinctly_from_canceled_by_guest(self):
        self._add("stays@example.org", STATUS_CONFIRMED)
        self._add("guest-left@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="guest")
        self._add("host-left@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="host")
        transport = self._sync()
        unfolded = [b for m, _u, b, _h in transport.calls if m == "PUT"][0].replace("\r\n ", "")
        self.assertIn("- canceled_by_guest |", unfolded)
        self.assertIn("- canceled_by_host |", unfolded)
        self.assertIn("by guest", unfolded)
        self.assertIn("by host", unfolded)
        self.assertIn("2 canceled", unfolded)

    def test_no_canceled_registrants_omits_the_section_entirely(self):
        self._add("stays@example.org", STATUS_CONFIRMED)
        transport = self._sync()
        unfolded = [b for m, _u, b, _h in transport.calls if m == "PUT"][0].replace("\r\n ", "")
        self.assertNotIn("Canceled:", unfolded)
        self.assertNotIn("canceled", unfolded)

    # -- removal condition: unchanged ---------------------------------------

    def test_zero_active_deletes_event_even_with_canceled_and_waitlisted_present(self):
        # The one confirmed registrant cancels -- zero active remain. Even
        # though a waitlisted and a canceled row still exist for this
        # occurrence, the event must still be deleted (the operator's spec: only
        # ALL participants canceling -- i.e. zero ACTIVE -- removes the
        # invite; canceled/waitlisted counts never factor in).
        self._add("waiter@example.org", STATUS_WAITLISTED)
        self._add("left@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="guest")
        uid = "example-org-yoga-class-1-2026-08-01@example.org"
        transport = self._sync(report_body=_report_with_event(uid))
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertIn("DELETE", methods)
        self.assertNotIn("PUT", methods)

    def test_one_active_remaining_keeps_and_updates_the_event(self):
        # Sanity check on the flip side: as long as >=1 active remains, the
        # event is PUT (updated), never deleted, regardless of canceled count.
        self._add("stays@example.org", STATUS_CONFIRMED)
        for i in range(3):
            self._add(f"left{i}@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="guest")
        transport = self._sync()
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertIn("PUT", methods)
        self.assertNotIn("DELETE", methods)

    # -- reminders (2026-07-07, the operator: "make the reminders (list) a setting.
    # But default to NO reminders" for the trainer's own event) ------------

    def test_operator_event_has_no_alarms_by_default(self):
        self._add("stays@example.org", STATUS_CONFIRMED)
        transport = self._sync()
        put_body = [b for m, _u, b, _h in transport.calls if m == "PUT"][0]
        self.assertNotIn("BEGIN:VALARM", put_body)

    def test_operator_event_honors_a_configured_trainer_reminder(self):
        self.settings = make_settings(
            courses=(self.course,), booking_calendar="Bookings",
            trainer_calendar_reminder_minutes=(30,),
        )
        self._add("stays@example.org", STATUS_CONFIRMED)
        transport = self._sync()
        put_body = [b for m, _u, b, _h in transport.calls if m == "PUT"][0].replace("\r\n ", "")
        self.assertIn("BEGIN:VALARM", put_body)
        self.assertIn("TRIGGER:-PT30M", put_body)

    # -- stale-ETag conflict retry (2026-07-07, the operator: a real production
    # 500 on /my/confirm, root-caused via journalctl to "PUT ... -> HTTP
    # 412 ... a newer version of the appointment already exists" -- a
    # same-second retry worked on its own, i.e. genuinely transient) ------

    def test_put_conflict_is_retried_with_a_fresh_etag_and_succeeds(self):
        self._add("stays@example.org", STATUS_CONFIRMED)
        transport = self._sync(conflicts_before_success=1)  # doesn't raise
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertEqual(methods.count("PUT"), 2)
        # One REPORT before the first attempt, one more to re-fetch a
        # fresh ETag before the retry.
        self.assertEqual(methods.count("REPORT"), 2)

    def test_put_conflict_gives_up_after_max_attempts(self):
        self._add("stays@example.org", STATUS_CONFIRMED)
        with self.assertRaises(CalDAVConflictError):
            self._sync(conflicts_before_success=_SYNC_CONFLICT_MAX_ATTEMPTS)

    def test_delete_conflict_is_retried_with_a_fresh_etag_and_succeeds(self):
        self._add("left@example.org", STATUS_CONFIRMED, cancel=True, canceled_by="guest")
        uid = "example-org-yoga-class-1-2026-08-01@example.org"
        transport = self._sync(report_body=_report_with_event(uid), conflicts_before_success=1)
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertEqual(methods.count("DELETE"), 2)


class ResyncAfterCourseRenameTest(unittest.TestCase):
    """2026-07-08, the operator: "rename lux-wed-mindfulness to lux-wed-mind ...
    provide a command to migrate the existing data" -- event_uid() bakes
    the course_shortname directly into the calendar event's UID, so a
    renamed course's already-synced future occurrences would otherwise be
    orphaned under their OLD uid forever, with a fresh duplicate created
    under the new one the next time anything calls sync_occurrence(). See
    calendar_sync.py's own docstring for the full mechanism -- these tests
    assume Store.rename_course_shortname has ALREADY run (registrations.csv
    rows are under `new_shortname` by the time resync_after_course_rename
    itself is called, same sequencing `my-bt admin rename-course` uses)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.new_course = make_course(shortname="lux-wed-mind", title="Mindfulness", capacity=20)
        self.settings = make_settings(courses=(self.new_course,), booking_calendar="Bookings", base_url="https://example.org")
        self.today = date(2026, 7, 8)

    def _client(self, report_body: str = EMPTY_REPORT):
        transport = FakeTransport(report_body)
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )
        return client, transport

    def _confirm(self, email: str, occurrence_date: str, course: str = "lux-wed-mind") -> None:
        user = self.store.upsert_user_for_booking(email, email.split("@")[0].title())
        self.store.add_registration(course, occurrence_date, user.user_id, "tok-hash", status=STATUS_CONFIRMED)

    def test_deletes_old_uid_event_and_recreates_under_new_uid(self):
        self._confirm("alice@example.org", "2026-07-10")
        old_uid = event_uid(self.settings, "lux-wed-mindfulness", date(2026, 7, 10))
        client, transport = self._client(report_body=_report_with_event(old_uid))

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 1)
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertIn("DELETE", methods)
        self.assertIn("PUT", methods)
        deleted_urls = [u for m, u, _b, _h in transport.calls if m == "DELETE"]
        self.assertTrue(any(old_uid in u for u in deleted_urls))

    def test_no_old_event_found_still_creates_the_new_one(self):
        self._confirm("alice@example.org", "2026-07-10")
        client, transport = self._client()  # empty REPORT -- nothing to delete

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 1)
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertNotIn("DELETE", methods)
        self.assertIn("PUT", methods)

    def test_dates_with_nothing_confirmed_are_skipped(self):
        # A waitlisted-only occurrence never had a calendar event in the
        # first place (sync_occurrence's own "0 confirmed -- no entry"
        # rule) -- resync must not touch it at all.
        user = self.store.upsert_user_for_booking("wl@example.org", "Waity")
        self.store.add_registration("lux-wed-mind", "2026-07-10", user.user_id, "tok-hash", status=STATUS_WAITLISTED)
        client, transport = self._client()

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 0)
        self.assertEqual(transport.calls, [])

    def test_past_occurrences_are_not_touched(self):
        self._confirm("alice@example.org", "2026-07-01")  # before self.today
        client, transport = self._client()

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 0)
        self.assertEqual(transport.calls, [])

    def test_today_itself_is_touched(self):
        self._confirm("alice@example.org", self.today.isoformat())
        client, transport = self._client()

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 1)

    def test_multiple_occurrences_all_fixed(self):
        self._confirm("alice@example.org", "2026-07-10")
        self._confirm("bob@example.org", "2026-07-17")
        client, transport = self._client()

        fixed = resync_after_course_rename(
            client, "/caldav/Bookings/", self.store, self.settings,
            "lux-wed-mindfulness", "lux-wed-mind", today=self.today,
        )

        self.assertEqual(fixed, 2)

    def test_raises_if_new_shortname_not_in_settings(self):
        client, _transport = self._client()
        with self.assertRaises(ValueError):
            resync_after_course_rename(
                client, "/caldav/Bookings/", self.store, self.settings,
                "lux-wed-mindfulness", "no-such-course", today=self.today,
            )


class GuestInviteAndCancelIcsTest(unittest.TestCase):
    """2026-07-09, the operator: "Can you please attach a calendar invite also in
    the email that is sent to the participant?" -- guest_invite_ics()
    (confirmed booking, METHOD:PUBLISH) and guest_cancel_ics() (later
    cancellation, METHOD:CANCEL, "Let's be nice :)"). Both are personal,
    single-guest .ics builders meant as EMAIL ATTACHMENTS, distinct from
    sync_occurrence()'s own shared operator-facing calendar event above."""

    def setUp(self):
        self.course = make_course(shortname="yoga-class-1", title="Yoga", location="Studio 1", description="Bring a mat.")
        self.settings = make_settings(courses=(self.course,), base_url="https://example.org")

    def test_invite_is_a_publish_with_no_status(self):
        filename, ics_text = guest_invite_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertTrue(filename.endswith(".ics"))
        self.assertIn("METHOD:PUBLISH", ics_text)
        self.assertNotIn("STATUS:", ics_text)
        self.assertIn("SEQUENCE:0", ics_text)
        self.assertIn("Bring a mat.", ics_text)

    def test_cancel_is_a_cancel_with_cancelled_status_and_bumped_sequence(self):
        filename, ics_text = guest_cancel_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertTrue(filename.endswith(".ics"))
        self.assertIn("METHOD:CANCEL", ics_text)
        self.assertIn("STATUS:CANCELLED", ics_text)
        self.assertIn("SEQUENCE:1", ics_text)

    def test_invite_and_cancel_share_the_same_uid_as_the_operators_own_event(self):
        # So a client that DOES correlate by UID (even though these are
        # separate, standalone .ics files, never PUT to CalDAV) sees the
        # same event identity as the operator's own synced calendar entry.
        expected_uid = event_uid(self.settings, self.course.shortname, date(2026, 8, 1))
        _f1, invite_ics = guest_invite_ics(self.settings, self.course, date(2026, 8, 1))
        _f2, cancel_ics = guest_cancel_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertEqual(parse_uid(invite_ics), expected_uid)
        self.assertEqual(parse_uid(cancel_ics), expected_uid)

    def test_cancel_has_no_alarms(self):
        _filename, ics_text = guest_cancel_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertNotIn("BEGIN:VALARM", ics_text)

    def test_invite_defaults_to_exactly_one_reminder_1h_before(self):
        # 2026-07-07, the operator: "The invites to the course participants should
        # have reminder 1h before the meeting."
        _filename, ics_text = guest_invite_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertEqual(ics_text.count("BEGIN:VALARM"), 1)
        self.assertIn("TRIGGER:-PT60M", ics_text)

    def test_invite_honors_a_configured_guest_reminder(self):
        settings = make_settings(
            courses=(self.course,), base_url="https://example.org",
            guest_calendar_reminder_minutes=(15, 60),
        )
        _filename, ics_text = guest_invite_ics(settings, self.course, date(2026, 8, 1))
        self.assertEqual(ics_text.count("BEGIN:VALARM"), 2)
        self.assertIn("TRIGGER:-PT15M", ics_text)
        self.assertIn("TRIGGER:-PT60M", ics_text)


if __name__ == "__main__":
    unittest.main()
