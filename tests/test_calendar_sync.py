"""Direct tests of calendar_sync.sync_occurrence's invite body -- the
active/waiting/canceled participant tables (status, name, email,
self/guest, timestamp), and the zero-active removal condition. See
calendar_sync.py's own docstring for what's being tested here."""
import tempfile
import unittest
from datetime import date

from app.caldav_client import CalDAVClient, Response
from app.calendar_sync import event_uid, guest_cancel_ics, guest_invite_ics, sync_occurrence
from app.ics import parse_uid
from app.storage import STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED, Store

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
    def __init__(self, report_body: str = EMPTY_REPORT):
        self.calls = []
        self.report_body = report_body

    def __call__(self, method, url, body="", extra_headers=None):
        self.calls.append((method, url, body, extra_headers or {}))
        if method == "REPORT":
            return Response(207, {}, self.report_body)
        if method == "PUT":
            return Response(201, {"etag": '"new"'}, "")
        if method == "DELETE":
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

    def _sync(self, report_body: str = EMPTY_REPORT):
        transport = FakeTransport(report_body)
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
            f"- confirmed | Alice | alice@example.org | self | registered {reloaded.registered_at} |",
            unfolded,
        )
        self.assertIn(
            f"- waitlisted #1 | Bob | bob@example.org | self | registered {reloaded_wl.registered_at} |",
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
        self.assertIn(f"canceled {canceled_reg.canceled_at} by guest", unfolded)
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

    def test_invite_still_has_alarms(self):
        _filename, ics_text = guest_invite_ics(self.settings, self.course, date(2026, 8, 1))
        self.assertIn("BEGIN:VALARM", ics_text)


if __name__ == "__main__":
    unittest.main()
