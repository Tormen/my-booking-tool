import unittest
from datetime import datetime, timezone

from app.ics import VEvent, parse_uid, parse_window


class VEventTest(unittest.TestCase):
    def test_roundtrip_uid(self):
        ev = VEvent(
            uid="example-org-lux-wed-yoga-2026-07-08@example.org",
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


if __name__ == "__main__":
    unittest.main()
