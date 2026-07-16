import unittest
from datetime import datetime, timezone

from app.ics import VEvent, is_all_day, parse_uid, parse_window


class IsAllDayTest(unittest.TestCase):
    """Feeds the conflict check's all-day filter (see webapp.py::
    _conflict_checker and [calendar].
    conflict_calendar_all_day_events_also_block_the_course)."""

    def test_date_only_dtstart_is_all_day(self):
        ics = "BEGIN:VEVENT\r\nUID:x@y\r\nDTSTART;VALUE=DATE:20260708\r\nDTEND;VALUE=DATE:20260709\r\nEND:VEVENT\r\n"
        self.assertTrue(is_all_day(ics))

    def test_timed_dtstart_is_not_all_day(self):
        ics = "BEGIN:VEVENT\r\nUID:x@y\r\nDTSTART:20260708T171500Z\r\nDTEND:20260708T185500Z\r\nEND:VEVENT\r\n"
        self.assertFalse(is_all_day(ics))

    def test_timed_dtstart_with_tzid_param_is_not_all_day(self):
        # A TZID parameter must not be mistaken for VALUE=DATE -- the
        # value itself still has a time-of-day.
        ics = "BEGIN:VEVENT\r\nUID:x@y\r\nDTSTART;TZID=Europe/Luxembourg:20260708T191500\r\nEND:VEVENT\r\n"
        self.assertFalse(is_all_day(ics))

    def test_no_dtstart_is_not_all_day(self):
        self.assertFalse(is_all_day("BEGIN:VEVENT\r\nUID:x@y\r\nEND:VEVENT\r\n"))


class VEventTest(unittest.TestCase):
    def test_roundtrip_uid(self):
        ev = VEvent(
            uid="example-org-yoga-class-1-2026-07-08@example.org",
            summary="Test (1/14)",
            description="line1\nline2",
            location="Somewhere",
            start=datetime(2026, 7, 8, 17, 15, tzinfo=timezone.utc),
            end=datetime(2026, 7, 8, 18, 55, tzinfo=timezone.utc),
        )
        ics = ev.to_ics()
        self.assertEqual(parse_uid(ics), ev.uid)
        start, end = parse_window(ics)
        self.assertEqual(start, ev.start)
        self.assertEqual(end, ev.end)

    def test_escapes_special_characters(self):
        ev = VEvent(
            uid="x@y",
            summary="a; b, c\\d",
            description="line1\nline2",
            location="loc",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
        ics = ev.to_ics()
        self.assertIn("a\\; b\\, c\\\\d", ics)

    def test_alarms_present(self):
        ev = VEvent(
            uid="x@y", summary="s", description="d", location="l",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
            alarms_minutes_before=(60,),
        )
        self.assertEqual(ev.to_ics().count("BEGIN:VALARM"), 1)

    def test_long_line_is_folded(self):
        ev = VEvent(
            uid="x@y", summary="s" * 200, description="d", location="l",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
        for line in ev.to_ics().split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_no_organizer_or_attendee_lines_by_default(self):
        # 2026-07-14: host_calendar_entry_cc_list's organizer/attendees
        # fields must be byte-identical-absent when unused, same
        # convention as alarms_minutes_before.
        ev = VEvent(
            uid="x@y", summary="s", description="d", location="l",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
        ics = ev.to_ics()
        self.assertNotIn("ORGANIZER", ics)
        self.assertNotIn("ATTENDEE", ics)

    def test_organizer_and_attendee_lines_present_when_configured(self):
        ev = VEvent(
            uid="x@y", summary="s", description="d", location="l",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
            organizer="calendar@example.org",
            attendees=("cc1@example.org", "cc2@example.org"),
        )
        # Unfolded -- these lines are long enough that RFC 5545 line-folding
        # (_fold(), see module docstring) splits them across a "\r\n "
        # continuation, same as every other long-line assertion in this
        # test module/tests/test_calendar_sync.py.
        unfolded = ev.to_ics().replace("\r\n ", "")
        self.assertIn("ORGANIZER:mailto:calendar@example.org", unfolded)
        self.assertIn("ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:cc1@example.org", unfolded)
        self.assertIn("ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:cc2@example.org", unfolded)


if __name__ == "__main__":
    unittest.main()
