"""Direct tests of calendar_sync.sync_occurrence's invite body -- the
active/waiting/canceled participant tables (status, name, email,
self/guest, timestamp), and the zero-active removal condition. See
calendar_sync.py's own docstring for what's being tested here."""
import os
import re
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from app.caldav_client import CalDAVClient, CalDAVConflictError, Response
from app.calendar_sync import (
    CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME, ResyncResult, _SYNC_CONFLICT_MAX_ATTEMPTS, event_uid,
    guest_cancel_ics, guest_invite_ics, record_resync_skips,
    resync_after_course_rename, resync_all_future_calendar_events, resync_if_format_changed, sync_occurrence,
)
from app.ics import parse_uid
from app.storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED,
    Store, format_display_timestamp,
)

from .helpers import make_course, make_settings

EMPTY_REPORT = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>"""

def _report_with_event(uid: str, etag: str = '"e1"', sequence: int | None = None) -> str:
    # `sequence`: added 2026-07-16 for the SEQUENCE-tracking incident (see
    # calendar_sync.py's parse_sequence() note) -- None (the default)
    # omits the SEQUENCE line entirely, same fixture shape as before this
    # was added, so every EXISTING test using this helper is unaffected
    # (parse_sequence() treats an absent line as 0, matching RFC 5545).
    sequence_line = f"SEQUENCE:{sequence}\n" if sequence is not None else ""
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
{sequence_line}SUMMARY:Test
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
        # 2026-07-07: a real production 500 on /my/confirm was
        # root-caused to a stale-ETag CalDAV 412; simulates that race --
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
        # 2026-07-13: the CALDAV invite needs BOTH a cancel link per
        # participant AND the course cancel link for ALL of them -- a
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
        # occurrence, the event must still be deleted (by spec: only
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

    # -- reminders (2026-07-07: the reminders (list) became a setting,
    # defaulting to NO reminders, for the trainer's own event) ------------

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

    # -- stale-ETag conflict retry (2026-07-07: a real production
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

    def test_persistent_conflict_logs_a_debug_hint_when_the_etag_never_actually_changes(self):
        # 2026-07-16: retrying more often wasn't fixing the underlying
        # calendar problem, so debug output is collected instead -- if the
        # etag we re-read after a 412
        # is the exact SAME one every time (as simulated here via
        # _report_with_event's fixed etag), that's specifically NOT what
        # a genuinely concurrent writer racing us should look like (that
        # writer's own change should have produced a NEW etag) -- see
        # _SYNC_CONFLICT_MAX_ATTEMPTS's own docstring. Confirms the debug
        # log actually flags this.
        self._add("stays@example.org", STATUS_CONFIRMED)
        uid = "example-org-yoga-class-1-2026-08-01@example.org"
        with mock.patch("app.calendar_sync.log") as m_log:
            with self.assertRaises(CalDAVConflictError):
                self._sync(
                    report_body=_report_with_event(uid), conflicts_before_success=_SYNC_CONFLICT_MAX_ATTEMPTS,
                )
        debug_messages = [call.args[0] % call.args[1:] for call in m_log.debug.call_args_list]
        self.assertTrue(any("UNCHANGED" in msg for msg in debug_messages))


class SyncOccurrenceSequenceTest(unittest.TestCase):
    """2026-07-16: root-caused via collected DEBUG output
    (not by retrying harder) -- a real production incident where EVERY
    single UPDATE to an already-existing operator calendar event failed
    with a permanent (not intermittent) HTTP 412, while a brand-new
    create succeeded fine. Open-Xchange's own error said why:
    "Concurrent modification [id 1081, client sequence 0, actual
    sequence 1]" -- sync_occurrence() always built its VEvent with the
    default sequence=0 and never incremented it, so the ETag matched but
    the server's own SEQUENCE check still rejected it, every time,
    forever. These tests use a transport that mimics that real
    Open-Xchange enforcement (reject unless the incoming SEQUENCE
    strictly exceeds what the server has tracked) -- they would have
    failed (raised CalDAVConflictError after exhausting all 3 attempts)
    against the OLD, fixed-sequence=0 code, and pass against the fix."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-class-1", title="Yoga", capacity=2)
        self.settings = make_settings(courses=(self.course,), booking_calendar="Bookings")
        self.occ_date = date(2026, 8, 1)
        self.store.upsert_user_for_booking("stays@example.org", "Stays")
        self.store.add_registration(
            "yoga-class-1", self.occ_date.isoformat(),
            self.store.find_user_by_email("stays@example.org").user_id, "tok-hash", status=STATUS_CONFIRMED,
        )
        self.uid = event_uid(self.settings, "yoga-class-1", self.occ_date)

    def _oxlike_transport(self, report_body_fn, put_bodies: list):
        """report_body_fn() is called fresh on every REPORT (so a retry
        can see a DIFFERENT server-tracked sequence than the first read,
        same as the real server would show after any update -- ours or
        anyone else's). PUT is accepted unconditionally if there's no
        EXISTING event yet (a fresh create, If-None-Match -- nothing to
        conflict with, same as production); otherwise only if the
        incoming SEQUENCE strictly exceeds whatever SEQUENCE the CURRENT
        report_body_fn() reports -- i.e. the exact Open-Xchange behavior
        from the incident, not just a canned pass/fail sequence."""
        def transport(method, url, body="", extra_headers=None):
            if method == "REPORT":
                return Response(207, {}, report_body_fn())
            if method == "PUT":
                put_bodies.append(body)
                current_report = report_body_fn()
                if f"UID:{self.uid}" in current_report:
                    sent = parse_sequence_for_test(body)
                    current = parse_sequence_for_test(current_report)
                    if sent <= current:
                        return Response(412, {}, '<D:error xmlns:D="DAV:"/>')
                return Response(204, {}, "")
            raise AssertionError(f"unexpected {method} {url}")
        return transport

    def test_put_sends_current_sequence_plus_one_not_a_fixed_zero(self):
        report_body = _report_with_event(self.uid, sequence=1)
        put_bodies: list = []
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self._oxlike_transport(lambda: report_body, put_bodies),
        )
        sync_occurrence(client, "/caldav/Bookings/", self.store, self.settings, self.course, self.occ_date)
        # Succeeds on the FIRST attempt now -- the old fixed-sequence=0
        # code would have needed (and never gotten) 3 failed attempts.
        self.assertEqual(len(put_bodies), 1)
        self.assertIn("SEQUENCE:2", put_bodies[0])

    def test_a_brand_new_event_still_starts_at_sequence_zero(self):
        # No existing event at all (empty REPORT) -- must NOT try to
        # increment past a sequence that doesn't exist yet.
        put_bodies: list = []
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self._oxlike_transport(lambda: EMPTY_REPORT, put_bodies),
        )
        sync_occurrence(client, "/caldav/Bookings/", self.store, self.settings, self.course, self.occ_date)
        self.assertEqual(len(put_bodies), 1)
        self.assertIn("SEQUENCE:0", put_bodies[0])

    def test_retry_re_reads_sequence_if_the_server_advanced_again_meanwhile(self):
        # Simulates the server's own tracked sequence moving AGAIN between
        # our first read and our first PUT attempt (e.g. a real, separate
        # update landed in between) -- our first attempt (computed from
        # the stale read) is correctly rejected, but the RETRY must
        # re-read and use the NEW current value, not just repeat the same
        # already-rejected one forever (that repetition is exactly the
        # 2026-07-16 bug).
        state = {"sequence": 1}

        def current_report():
            return _report_with_event(self.uid, sequence=state["sequence"])

        put_bodies: list = []
        real_transport = self._oxlike_transport(current_report, put_bodies)

        def flaky_transport(method, url, body="", extra_headers=None):
            resp = real_transport(method, url, body=body, extra_headers=extra_headers)
            # The FIRST PUT attempt targets sequence 1+1=2, but by the
            # time it arrives the server has already moved to 2 (as if
            # someone/something else updated it in between) -- so it's
            # rejected even though our math was right against the STALE
            # read; only the retry (which re-reads first) can succeed.
            if method == "PUT" and len(put_bodies) == 1:
                state["sequence"] = 2
                return Response(412, {}, '<D:error xmlns:D="DAV:"/>')
            return resp

        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=flaky_transport,
        )
        sync_occurrence(client, "/caldav/Bookings/", self.store, self.settings, self.course, self.occ_date)
        self.assertEqual(len(put_bodies), 2)
        self.assertIn("SEQUENCE:2", put_bodies[0])  # first attempt: stale read (1) + 1
        self.assertIn("SEQUENCE:3", put_bodies[1])  # retry: fresh read (2) + 1, succeeds


def parse_sequence_for_test(ics_or_report_text: str) -> int:
    """Independent of app.ics.parse_sequence (the code under test) so
    SyncOccurrenceSequenceTest's fake Open-Xchange-like transport isn't
    just testing itself against its own parsing logic."""
    m = re.search(r"SEQUENCE:(-?\d+)", ics_or_report_text)
    return int(m.group(1)) if m else 0


class ResyncAfterCourseRenameTest(unittest.TestCase):
    """2026-07-08: renaming lux-wed-mindfulness to lux-wed-mind required
    a command to migrate the existing data -- event_uid() bakes
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


class ResyncAllFutureCalendarEventsTest(unittest.TestCase):
    """2026-07-09: a real occurrence's calendar invite was found to be
    still missing the "cancel entire session" line added days earlier, so
    existing (future) calendar invites now get updated as well -- then
    narrowed to HOST-side events only, since an
    already-emailed guest .ics can't be retroactively edited. See
    resync_all_future_calendar_events's own docstring. Same discovery logic
    as ResyncAfterCourseRenameTest above, just across every configured
    course instead of one, and no old-uid deletion step (the shortname
    never changes here -- every occurrence is re-synced under its OWN
    already-current uid)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course_a = make_course(shortname="yoga-class-1", title="Yoga A", capacity=14)
        self.course_b = make_course(shortname="yoga-class-2", title="Yoga B", capacity=10)
        self.settings = make_settings(
            courses=(self.course_a, self.course_b), booking_calendar="Bookings", base_url="https://example.org",
        )
        self.today = date(2026, 7, 8)

    def _client(self, report_body: str = EMPTY_REPORT):
        transport = FakeTransport(report_body)
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )
        return client, transport

    def _confirm(self, email: str, occurrence_date: str, course: str) -> None:
        user = self.store.upsert_user_for_booking(email, email.split("@")[0].title())
        self.store.add_registration(course, occurrence_date, user.user_id, "tok-hash", status=STATUS_CONFIRMED)

    def test_resyncs_confirmed_future_occurrence(self):
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 1)
        self.assertEqual(result.skipped, [])
        methods = [m for m, _u, _b, _h in transport.calls]
        self.assertIn("PUT", methods)

    def test_covers_every_configured_course_not_just_the_first(self):
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        self._confirm("bob@example.org", "2026-07-11", "yoga-class-2")
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 2)
        put_urls = [u for m, u, _b, _h in transport.calls if m == "PUT"]
        self.assertTrue(any(event_uid(self.settings, "yoga-class-1", date(2026, 7, 10)) in u for u in put_urls))
        self.assertTrue(any(event_uid(self.settings, "yoga-class-2", date(2026, 7, 11)) in u for u in put_urls))

    def test_waitlisted_only_occurrence_is_skipped(self):
        # No confirmed registrant -- sync_occurrence's own rule means there
        # was never a calendar event here at all, so this must be left
        # alone entirely (no PUT, no DELETE).
        user = self.store.upsert_user_for_booking("wl@example.org", "Waity")
        self.store.add_registration("yoga-class-1", "2026-07-10", user.user_id, "tok-hash", status=STATUS_WAITLISTED)
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 0)
        self.assertEqual(transport.calls, [])

    def test_past_occurrences_are_not_touched(self):
        self._confirm("alice@example.org", "2026-07-01", "yoga-class-1")  # before self.today
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 0)
        self.assertEqual(transport.calls, [])

    def test_today_itself_is_touched(self):
        self._confirm("alice@example.org", self.today.isoformat(), "yoga-class-1")
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 1)

    def test_no_confirmed_registrations_anywhere_touches_nothing(self):
        client, transport = self._client()

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 0)
        self.assertEqual(transport.calls, [])

    def test_a_persistent_conflict_on_one_occurrence_does_not_abort_the_rest(self):
        # 2026-07-15, real production incident (VPS, `my-bt admin setup -i`'s
        # new step 13): one occurrence hit a genuinely PERSISTENT
        # CalDAVConflictError (still a stale ETag after all
        # _SYNC_CONFLICT_MAX_ATTEMPTS retries -- something else kept
        # touching that exact event). sync_occurrence() re-raises on its
        # final attempt; this used to abort the WHOLE batch, silently
        # skipping every occurrence not yet reached. Now the stuck one is
        # skipped (logged AND recorded in result.skipped), everything else
        # still gets resynced.
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        self._confirm("bob@example.org", "2026-07-11", "yoga-class-2")
        stuck_uid = event_uid(self.settings, "yoga-class-1", date(2026, 7, 10))

        def transport(method, url, body="", extra_headers=None):
            if method == "REPORT":
                return Response(207, {}, EMPTY_REPORT)
            if method == "PUT":
                if stuck_uid in url:
                    return Response(412, {}, '<D:error xmlns:D="DAV:"/>')
                return Response(201, {"etag": '"new"'}, "")
            raise AssertionError(f"unexpected {method} {url}")

        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        # Only yoga-class-2's occurrence actually succeeded -- the stuck
        # yoga-class-1 one doesn't count, but also doesn't raise/abort --
        # it shows up in `skipped` instead.
        self.assertEqual(result.fixed, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("yoga-class-1", result.skipped[0])
        self.assertIn("2026-07-10", result.skipped[0])

    def test_persistent_conflict_gets_the_same_attempt_count_as_a_live_request(self):
        # 2026-07-16: after a real bulk resync hit persistent conflicts on
        # 3 occurrences, a prior version of this gave the bulk resync path
        # MORE attempts with real backoff than a live request gets,
        # reasoning it was probably just an active concurrent writer.
        # A follow-up report showed retrying more often wasn't fixing the
        # underlying calendar problem, and debug output was needed
        # instead. So the bulk resync
        # now gets the EXACT same _SYNC_CONFLICT_MAX_ATTEMPTS (3,
        # zero-delay) as a live booking/cancellation request -- no
        # special case, no sleeping. This test locks that in.
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        put_attempts = {"n": 0}

        def flaky_transport(method, url, body="", extra_headers=None):
            if method == "REPORT":
                return Response(207, {}, EMPTY_REPORT)
            if method == "PUT":
                put_attempts["n"] += 1
                return Response(412, {}, '<D:error xmlns:D="DAV:"/>')
            raise AssertionError(f"unexpected {method} {url}")

        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=flaky_transport,
        )

        result = resync_all_future_calendar_events(
            client, "/caldav/Bookings/", self.store, self.settings, today=self.today,
        )

        self.assertEqual(result.fixed, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(put_attempts["n"], _SYNC_CONFLICT_MAX_ATTEMPTS)


class ResyncIfFormatChangedTest(unittest.TestCase):
    """2026-07-14: the "on install" half of a standing request
    (2026-07-09: resync either on install or on the next moment this
    calendar invite is touched again) -- resync_if_format_changed() only
    actually resyncs when CALENDAR_INVITE_FORMAT_VERSION doesn't match a
    marker file recorded under the data dir, so `my-bt setup -i` can call
    this unconditionally on every run without re-syncing every single
    time it's invoked. 2026-07-16: after a previous incident's fix didn't
    take effect until `my-bt admin resync-calendar` was separately run by
    hand, this ALSO now resyncs whenever a
    previous attempt left pending skips recorded, independent of the
    format-version marker; see test_pending_skips_trigger_a_resync_even_
    when_the_format_marker_already_matches below."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.store = Store(self.data_dir)
        self.course = make_course(shortname="yoga-class-1", capacity=14)
        self.settings = make_settings(courses=(self.course,), booking_calendar="Bookings", base_url="https://example.org")
        self.today = date(2026, 7, 8)

    def _client(self, report_body: str = EMPTY_REPORT):
        transport = FakeTransport(report_body)
        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )
        return client, transport

    def _confirm(self, email: str, occurrence_date: str, course: str) -> None:
        user = self.store.upsert_user_for_booking(email, email.split("@")[0].title())
        self.store.add_registration(course, occurrence_date, user.user_id, "tok-hash", status=STATUS_CONFIRMED)

    def test_no_marker_yet_runs_once_and_writes_it(self):
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        self.assertEqual(result.fixed, 1)
        self.assertEqual(result.skipped, [])
        marker = Path(self.data_dir) / ".calendar_invite_format_version"
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "1")
        # 2026-07-15: the marker write goes through atomic_io.
        # atomic_write_text (temp file + fsync + rename), not a bare
        # write_text() -- confirm its own temp file (see mkstemp's
        # prefix/suffix in atomic_write_text) isn't left lying around
        # (other data dir files -- users.csv etc, from self._confirm()
        # above -- are expected and not what this is checking).
        leftover_tmps = [p.name for p in Path(self.data_dir).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftover_tmps, [])
        # 2026-07-10: secure=True -- this marker lives in the same shared
        # data_dir as users.csv/registrations.csv and is exposed to the
        # exact same root-run-my-bt ownership problem (see
        # app.atomic_io.secure_data_path's docstring); group-readable
        # (0640), not owner-only (0600), confirms it's actually wired up.
        self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o640)

    def test_matching_marker_is_a_no_op(self):
        marker = Path(self.data_dir) / ".calendar_invite_format_version"
        marker.write_text("1\n", encoding="utf-8")
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        self.assertIsNone(result)
        self.assertEqual(transport.calls, [])

    def test_pending_skips_trigger_a_resync_even_when_the_format_marker_already_matches(self):
        # 2026-07-16: `my-bt setup` needed to catch this too -- a stale
        # skip marker left over from a PREVIOUS,
        # already-fixed-in-the-meantime incident used to just sit there
        # forever (repeated in every `admin health`/`admin setup` as a
        # WARN, exit 1) because the format-version marker already
        # matched, so this function returned None before ever attempting
        # a real resync that could clear it. Now the pending-skips
        # marker is its own, independent trigger.
        format_marker = Path(self.data_dir) / ".calendar_invite_format_version"
        format_marker.write_text("1\n", encoding="utf-8")
        skip_marker = Path(self.data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
        skip_marker.write_text("yoga-class-1 on 2026-07-10: HTTP 412 (now fixed)\n", encoding="utf-8")
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        # It actually ran (not None) and cleared the now-resolved skip.
        self.assertIsNotNone(result)
        self.assertEqual(result.fixed, 1)
        self.assertEqual(result.skipped, [])
        self.assertFalse(skip_marker.exists())
        # The format-version marker is untouched in meaning (still "1"),
        # even though we ran only because of the pending skip.
        self.assertEqual(format_marker.read_text(encoding="utf-8").strip(), "1")

    def test_stale_marker_triggers_a_resync_and_updates_the_marker(self):
        marker = Path(self.data_dir) / ".calendar_invite_format_version"
        marker.write_text("1\n", encoding="utf-8")
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=2,
        )

        self.assertEqual(result.fixed, 1)
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "2")

    def test_running_twice_in_a_row_only_resyncs_the_first_time(self):
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        client, transport = self._client()

        first = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )
        second = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        self.assertEqual(first.fixed, 1)
        self.assertIsNone(second)

    def test_persistent_conflict_on_one_occurrence_still_writes_the_marker(self):
        # 2026-07-15, the real production incident this closes: before
        # resync_all_future_calendar_events()'s own per-occurrence
        # resilience fix, ANY occurrence with a persistent CalDAV conflict
        # made this whole function raise -- so the marker below was NEVER
        # written, and every subsequent `setup -i` run hit the exact same
        # occurrence and failed the exact same way, forever. Now: the
        # stuck occurrence is skipped (not counted, but recorded in
        # result.skipped), everything else still resyncs, and the marker
        # gets written either way.
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        marker = Path(self.data_dir) / ".calendar_invite_format_version"

        def transport(method, url, body="", extra_headers=None):
            if method == "REPORT":
                return Response(207, {}, EMPTY_REPORT)
            if method == "PUT":
                return Response(412, {}, '<D:error xmlns:D="DAV:"/>')  # always stuck
            raise AssertionError(f"unexpected {method} {url}")

        client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=transport,
        )

        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        self.assertEqual(result.fixed, 0)  # the one occurrence there is never actually succeeded
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "1")  # but the marker is still written
        # 2026-07-15/16: resync_if_format_changed() also calls
        # record_resync_skips(), so this stays visible in a LATER
        # `admin health`/`admin setup` run too -- not just this one's own
        # printed output. See RecordResyncSkipsTest / app.cli_checks.
        # check_calendar_invite_resync_skips.
        skip_marker = Path(self.data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
        self.assertTrue(skip_marker.exists())
        self.assertIn("yoga-class-1", skip_marker.read_text(encoding="utf-8"))

    def test_a_clean_resync_after_a_previous_skip_clears_the_skip_marker(self):
        # The skip marker must reflect the LATEST attempt, not accumulate
        # forever -- once a previously-stuck occurrence resyncs cleanly
        # (or simply no longer exists), the marker should be removed.
        self._confirm("alice@example.org", "2026-07-10", "yoga-class-1")
        skip_marker = Path(self.data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
        skip_marker.write_text("stale leftover entry\n", encoding="utf-8")

        client, transport = self._client()
        result = resync_if_format_changed(
            client, "/caldav/Bookings/", self.store, self.settings, self.data_dir,
            today=self.today, format_version=1,
        )

        self.assertEqual(result.skipped, [])
        self.assertFalse(skip_marker.exists())


class RecordResyncSkipsTest(unittest.TestCase):
    """record_resync_skips() itself -- called by both resync_if_format_
    changed() (automatic) and `my-bt admin resync-calendar` (manual, see
    scripts/my-bt::cmd_admin_resync_calendar) after every real resync
    attempt, so app.cli_checks.check_calendar_invite_resync_skips() can
    keep flagging an unresolved conflict long after the run that
    discovered it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.marker_path = Path(self.data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME

    def test_writes_the_skipped_lines_when_present(self):
        result = ResyncResult(fixed=2, skipped=["yoga-class-1 on 2026-07-10: boom"])
        record_resync_skips(self.data_dir, result)
        self.assertEqual(self.marker_path.read_text(encoding="utf-8").strip(), "yoga-class-1 on 2026-07-10: boom")
        # 2026-07-10: secure=True -- same shared data_dir, same root-run-
        # my-bt exposure as users.csv/registrations.csv (see
        # app.atomic_io.secure_data_path's docstring).
        self.assertEqual(stat.S_IMODE(os.stat(self.marker_path).st_mode), 0o640)

    def test_multiple_skips_are_one_per_line(self):
        result = ResyncResult(fixed=0, skipped=["a: boom", "b: bang"])
        record_resync_skips(self.data_dir, result)
        self.assertEqual(self.marker_path.read_text(encoding="utf-8").splitlines(), ["a: boom", "b: bang"])

    def test_clean_result_removes_an_existing_marker(self):
        self.marker_path.write_text("stale\n", encoding="utf-8")
        record_resync_skips(self.data_dir, ResyncResult(fixed=3, skipped=[]))
        self.assertFalse(self.marker_path.exists())

    def test_clean_result_with_no_existing_marker_is_a_no_op(self):
        record_resync_skips(self.data_dir, ResyncResult(fixed=3, skipped=[]))  # must not raise
        self.assertFalse(self.marker_path.exists())


class GuestInviteAndCancelIcsTest(unittest.TestCase):
    """2026-07-09: a calendar invite is also attached to
    the email that is sent to the participant -- guest_invite_ics()
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
        # 2026-07-07: the invites to the course participants should
        # have a reminder 1h before the meeting.
        _filename, ics_text = guest_invite_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertEqual(ics_text.count("BEGIN:VALARM"), 1)
        self.assertIn("TRIGGER:-PT60M", ics_text)

    def test_date_override_shifts_dtstart_and_dtend(self):
        # 2026-07-16: the per-course date_overrides feature means the
        # guest's own .ics attachment must reflect the exceptional
        # shifted time too, same as the operator's synced calendar event
        # (both go through the shared occurrence_start_end()).
        from app.config import CourseDateOverride

        course = make_course(
            shortname="yoga-class-1", title="Yoga", location="Studio 1", description="",
            weekday="sat", start_time="17:15", duration_minutes=100,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        settings = make_settings(courses=(course,), base_url="https://example.org")
        _filename, ics_text = guest_invite_ics(settings, course, date(2026, 7, 18))
        # Europe/Berlin is CEST (+2) in July -- 09:45 local -> 07:45 UTC,
        # and the 100min duration is kept (unaffected, no override
        # duration_minutes set) -> ends 11:25 local -> 09:25 UTC.
        self.assertIn("DTSTART:20260718T074500Z", ics_text)
        self.assertIn("DTEND:20260718T092500Z", ics_text)

    def test_unrelated_date_is_unaffected_by_a_different_dates_override(self):
        from app.config import CourseDateOverride

        course = make_course(
            shortname="yoga-class-1", title="Yoga", location="Studio 1", description="",
            weekday="sat", start_time="17:15", duration_minutes=100,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        settings = make_settings(courses=(course,), base_url="https://example.org")
        _filename, ics_text = guest_invite_ics(settings, course, date(2026, 7, 25))
        # 17:15 local (normal, unaffected) -> 15:15 UTC.
        self.assertIn("DTSTART:20260725T151500Z", ics_text)

    def test_invite_honors_a_configured_participant_reminder(self):
        settings = make_settings(
            courses=(self.course,), base_url="https://example.org",
            participant_calendar_reminder_minutes=(15, 60),
        )
        _filename, ics_text = guest_invite_ics(settings, self.course, date(2026, 8, 1))
        self.assertEqual(ics_text.count("BEGIN:VALARM"), 2)
        self.assertIn("TRIGGER:-PT15M", ics_text)
        self.assertIn("TRIGGER:-PT60M", ics_text)


class SyncOccurrenceHostCalendarEntryCcListTest(unittest.TestCase):
    """2026-07-14: a list of email addresses that if set on a course
    in settings.toml will also be invited as optional (cc) so that they
    receive the same invite as well -- sync_occurrence()'s own HOST
    event, not the guest .ics (guest_invite_ics is untouched by this)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.occ_date = date(2026, 8, 1)

    def _sync(self, course):
        settings = make_settings(courses=(course,), booking_calendar="Bookings")
        user = self.store.upsert_user_for_booking("alice@example.org", "Alice")
        self.store.add_registration(
            course.shortname, self.occ_date.isoformat(), user.user_id, "tok-hash", status=STATUS_CONFIRMED,
        )
        transport = FakeTransport()
        client = CalDAVClient(
            settings.caldav_url, settings.caldav_username, settings.caldav_password, transport=transport,
        )
        sync_occurrence(client, "/caldav/Bookings/", self.store, settings, course, self.occ_date)
        put_bodies = [b for m, _u, b, _h in transport.calls if m == "PUT"]
        self.assertEqual(len(put_bodies), 1)
        return put_bodies[0].replace("\r\n ", "")  # unfold -- see test_ics.py's own note

    def test_no_organizer_or_attendee_when_cc_list_is_unset(self):
        course = make_course(shortname="no-cc")
        ics_text = self._sync(course)
        self.assertNotIn("ORGANIZER", ics_text)
        self.assertNotIn("ATTENDEE", ics_text)

    def test_organizer_and_attendees_added_when_cc_list_is_set(self):
        course = make_course(
            shortname="has-cc", host_calendar_entry_cc_list=("work.copy@example.org",),
        )
        ics_text = self._sync(course)
        # caldav_username is make_settings()'s default -- "calendar@example.org".
        self.assertIn("ORGANIZER:mailto:calendar@example.org", ics_text)
        self.assertIn(
            "ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:"
            "mailto:work.copy@example.org",
            ics_text,
        )

    def test_multiple_cc_addresses_each_get_their_own_attendee_line(self):
        course = make_course(
            shortname="has-cc-multi",
            host_calendar_entry_cc_list=("a@example.org", "b@example.org"),
        )
        ics_text = self._sync(course)
        self.assertIn("mailto:a@example.org", ics_text)
        self.assertIn("mailto:b@example.org", ics_text)
        self.assertEqual(ics_text.count("ATTENDEE;ROLE=OPT-PARTICIPANT"), 2)


if __name__ == "__main__":
    unittest.main()
