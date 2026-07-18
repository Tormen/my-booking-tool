"""app/ics_feed.py -- whole-feed parsing, VTIMEZONE offset resolution
and RRULE expansion. Fixtures are shaped like the real published Outlook
feed this was built against (Windows TZID names with VTIMEZONE rules,
X-MICROSOFT-CDO-BUSYSTATUS, RECURRENCE-ID overrides, folded lines) but
carry synthetic data only."""
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import ics_feed

from .helpers import make_course  # noqa: F401  (imports helpers' logging.disable)

TZ = ZoneInfo("Europe/Berlin")

VTIMEZONE_W_EUROPE = """BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""


def feed_text(*vevents: str, timezones: str = VTIMEZONE_W_EUROPE) -> str:
    body = "\n".join(vevents)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\n{timezones}\n{body}\nEND:VCALENDAR\n"


def vevent(uid="e1@x", dtstart="DTSTART;TZID=W. Europe Standard Time:20260708T171500",
           dtend="DTEND;TZID=W. Europe Standard Time:20260708T185500",
           summary="Busy thing", extra=""):
    return (
        f"BEGIN:VEVENT\nUID:{uid}\n{dtstart}\n{dtend}\nSUMMARY:{summary}\n"
        f"{extra}END:VEVENT"
    )


def window(y1, m1, d1, y2, m2, d2):
    return (
        datetime(y1, m1, d1, tzinfo=timezone.utc),
        datetime(y2, m2, d2, tzinfo=timezone.utc),
    )


class VTimezoneTest(unittest.TestCase):
    def test_windows_named_zone_resolves_summer_and_winter_offsets(self):
        feed = ics_feed.parse_feed(feed_text(vevent()))
        tz = feed.timezones["W. Europe Standard Time"]
        self.assertEqual(tz.utc_offset(datetime(2026, 7, 15, 12, 0)).total_seconds(), 7200)
        self.assertEqual(tz.utc_offset(datetime(2026, 1, 15, 12, 0)).total_seconds(), 3600)

    def test_timed_event_converted_via_its_vtimezone(self):
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(vevent())), *window(2026, 7, 1, 2026, 7, 31), TZ)
        (occ,) = occs
        # 17:15 CEST == 15:15 UTC.
        self.assertEqual(occ.start, datetime(2026, 7, 8, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(occ.end, datetime(2026, 7, 8, 16, 55, tzinfo=timezone.utc))

    def test_undeclared_tzid_falls_back_to_default_tz(self):
        # Outlook routinely references TZIDs it never declares ("Romance
        # Standard Time" etc.) -- the site timezone is the fallback.
        ev = vevent(dtstart="DTSTART;TZID=Romance Standard Time:20260708T171500",
                    dtend="DTEND;TZID=Romance Standard Time:20260708T185500")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertEqual(occs[0].start, datetime(2026, 7, 8, 15, 15, tzinfo=timezone.utc))


class ParseFeedTest(unittest.TestCase):
    def test_busystatus_transp_and_summary_parsed(self):
        ev = vevent(extra="TRANSP:TRANSPARENT\nX-MICROSOFT-CDO-BUSYSTATUS:OOF\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        (occ,) = occs
        self.assertEqual(occ.busy_status, "OOF")
        self.assertTrue(occ.transparent)
        self.assertEqual(occ.summary, "Busy thing")

    def test_folded_summary_is_unfolded(self):
        ev = "BEGIN:VEVENT\nUID:f@x\nDTSTART:20260708T151500Z\nDTEND:20260708T165500Z\nSUMMARY:A very long conference titl\n e with #mark\n er inside\nEND:VEVENT"
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertIn("#marker", occs[0].summary)

    def test_cancelled_events_are_skipped(self):
        ev = vevent(extra="STATUS:CANCELLED\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertEqual(occs, [])

    def test_all_day_multi_day_event(self):
        ev = ("BEGIN:VEVENT\nUID:ad@x\nDTSTART;VALUE=DATE:20260706\n"
              "DTEND;VALUE=DATE:20260709\nSUMMARY:Off\n"
              "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE\nEND:VEVENT")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        (occ,) = occs
        self.assertTrue(occ.all_day)
        self.assertEqual(occ.day_start, date(2026, 7, 6))
        self.assertEqual(occ.day_end, date(2026, 7, 9))  # DTEND exclusive

    def test_duration_instead_of_dtend(self):
        ev = ("BEGIN:VEVENT\nUID:d@x\nDTSTART:20260708T151500Z\n"
              "DURATION:PT1H40M\nSUMMARY:With duration\nEND:VEVENT")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertEqual(occs[0].end, datetime(2026, 7, 8, 16, 55, tzinfo=timezone.utc))


class RRuleExpansionTest(unittest.TestCase):
    def test_weekly_rule_repeats_on_the_dtstart_weekday(self):
        ev = vevent(extra="RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=WE\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertEqual(
            [o.start.date() for o in occs],
            [date(2026, 7, 8), date(2026, 7, 15), date(2026, 7, 22), date(2026, 7, 29)],
        )

    def test_exdate_removes_one_instance(self):
        ev = vevent(extra=(
            "RRULE:FREQ=WEEKLY;BYDAY=WE\n"
            "EXDATE;TZID=W. Europe Standard Time:20260715T171500\n"
        ))
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertNotIn(date(2026, 7, 15), [o.start.date() for o in occs])
        self.assertEqual(len(occs), 3)

    def test_recurrence_id_override_replaces_the_instance(self):
        master = vevent(extra="RRULE:FREQ=WEEKLY;BYDAY=WE\n")
        override = (
            "BEGIN:VEVENT\nUID:e1@x\n"
            "RECURRENCE-ID;TZID=W. Europe Standard Time:20260715T171500\n"
            "DTSTART;TZID=W. Europe Standard Time:20260715T090000\n"
            "DTEND;TZID=W. Europe Standard Time:20260715T100000\n"
            "SUMMARY:Moved instance\nX-MICROSOFT-CDO-BUSYSTATUS:OOF\nEND:VEVENT"
        )
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(master, override)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        moved = [o for o in occs if o.start.date() == date(2026, 7, 15)]
        (occ,) = moved
        self.assertEqual(occ.start, datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(occ.summary, "Moved instance")
        self.assertEqual(occ.busy_status, "OOF")
        self.assertEqual(len(occs), 4)

    def test_count_limits_the_instances(self):
        ev = vevent(extra="RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=2\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 8, 31), TZ)
        self.assertEqual(len(occs), 2)

    def test_until_limits_the_instances(self):
        ev = vevent(extra="RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20260716T000000Z\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 8, 31), TZ)
        self.assertEqual([o.start.date() for o in occs], [date(2026, 7, 8), date(2026, 7, 15)])

    def test_monthly_last_friday(self):
        ev = vevent(
            dtstart="DTSTART;TZID=W. Europe Standard Time:20260626T120000",
            dtend="DTEND;TZID=W. Europe Standard Time:20260626T130000",
            extra="RRULE:FREQ=MONTHLY;BYDAY=-1FR\n",
        )
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 8, 31), TZ)
        self.assertEqual([o.start.date() for o in occs], [date(2026, 7, 31), date(2026, 8, 28)])

    def test_unsupported_rrule_part_falls_back_to_single_occurrence(self):
        ev = vevent(extra="RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1\n")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 8, 31), TZ)
        # Not silently dropped, not wrongly expanded -- DTSTART only.
        self.assertEqual([o.start.date() for o in occs], [date(2026, 7, 8)])

    def test_weekly_all_day_recurrence(self):
        ev = ("BEGIN:VEVENT\nUID:wad@x\nDTSTART;VALUE=DATE:20260706\n"
              "DTEND;VALUE=DATE:20260707\nSUMMARY:Weekly off day\n"
              "RRULE:FREQ=WEEKLY;BYDAY=MO\nEND:VEVENT")
        occs = ics_feed.expand(ics_feed.parse_feed(feed_text(ev)), *window(2026, 7, 1, 2026, 7, 31), TZ)
        self.assertEqual(
            [o.day_start for o in occs],
            [date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 20), date(2026, 7, 27)],
        )
        self.assertTrue(all(o.all_day for o in occs))
