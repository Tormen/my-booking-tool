"""app/conflict.py -- the [[conflict_calendar]] engine: blocks/requires
semantics, show_as/title/all-day matching, per-course scoping, and the
ICS last-known-good cache + rate-limited WARNING email policy (operator's
explicit design, 2026-07-18). Every side effect injected -- no network,
no SMTP."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.caldav_client import CalDAVError
from app.conflict import ConflictEngine, _matches_entry
from app import ics_feed

from .helpers import make_conflict_calendar, make_course, make_settings

# The standard occurrence window: Wednesday 2026-07-08, 17:15-18:55
# Europe/Berlin (make_settings' timezone) == 15:15-16:55 UTC.
OCC_START = datetime(2026, 7, 8, 15, 15, tzinfo=timezone.utc)
OCC_END = datetime(2026, 7, 8, 16, 55, tzinfo=timezone.utc)


def ics_with(*vevents: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "\n".join(vevents) + "\nEND:VCALENDAR\n"


def timed_event(start="20260708T140000Z", end="20260708T180000Z",
                busy="OOF", summary="Out of office", uid="w1@x") -> str:
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTART:{start}\nDTEND:{end}\n"
        f"SUMMARY:{summary}\nX-MICROSOFT-CDO-BUSYSTATUS:{busy}\nEND:VEVENT"
    )


def all_day_event(day="20260708", busy="OOF", summary="Off site", transp="") -> str:
    extra = f"TRANSP:{transp}\n" if transp else ""
    return (
        f"BEGIN:VEVENT\nUID:ad@x\nDTSTART;VALUE=DATE:{day}\n"
        f"SUMMARY:{summary}\nX-MICROSOFT-CDO-BUSYSTATUS:{busy}\n{extra}END:VEVENT"
    )


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "conflict_cache"
        self.course = make_course()  # shortname yoga-class-1
        self.sent_mails: list[tuple[str, str]] = []
        self.fetch_result: str | Exception = ics_with()
        self.now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

    def engine(self, *entries, caldav_events=None) -> ConflictEngine:
        settings = make_settings(courses=(self.course,), conflict_calendars=tuple(entries))

        def fetch(url):
            if isinstance(self.fetch_result, Exception):
                raise self.fetch_result
            return self.fetch_result

        class FakeClient:
            def __init__(self, events):
                self.events = events or []

            def list_calendars(self):
                return {"Bookings": "/caldav/Bookings/", "Work": "/caldav/Work/"}

            def query_events(self, href, start, end):
                out = []
                for text in self.events:
                    win = None
                    for occ in ics_feed.expand(
                        ics_feed.parse_feed(text), start, end,
                        timezone.utc,
                    ):
                        win = occ
                    if win is not None:
                        out.append(("uid", text, "etag"))
                return out

        fake_client = FakeClient(caldav_events)
        return ConflictEngine(
            settings, self.cache_dir,
            booking_client_fn=lambda: fake_client,
            booking_href_fn=lambda: "/caldav/Bookings/",
            client_factory=lambda entry: fake_client,
            fetch=fetch,
            send_warning_mail=lambda subject, body: self.sent_mails.append((subject, body)),
            now_fn=lambda: self.now,
        )

    def hidden(self, engine) -> bool:
        return engine.occurrence_is_hidden(self.course, OCC_START, OCC_END)


class RequiresModeTest(EngineFixture):
    def entry(self, **overrides):
        defaults = dict(name="work", mode="requires", show_as="oof",
                        use_booking_calendar=False, ics_url="https://x/feed.ics")
        defaults.update(overrides)
        return make_conflict_calendar(**defaults)

    def test_single_spanning_oof_event_makes_the_date_possible(self):
        self.fetch_result = ics_with(timed_event())
        self.assertFalse(self.hidden(self.engine(self.entry())))

    def test_no_matching_event_hides_the_date(self):
        self.fetch_result = ics_with()
        self.assertTrue(self.hidden(self.engine(self.entry())))

    def test_partially_covering_event_is_not_enough(self):
        # 14:00-16:00 UTC only covers part of the 15:15-16:55 window --
        # a SINGLE event must span the whole from-till range.
        self.fetch_result = ics_with(timed_event(end="20260708T160000Z"))
        self.assertTrue(self.hidden(self.engine(self.entry())))

    def test_two_adjacent_events_do_not_combine(self):
        self.fetch_result = ics_with(
            timed_event(end="20260708T160000Z", uid="a@x"),
            timed_event(start="20260708T160000Z", end="20260708T180000Z", uid="b@x"),
        )
        self.assertTrue(self.hidden(self.engine(self.entry())))

    def test_busy_event_does_not_satisfy_show_as_oof(self):
        self.fetch_result = ics_with(timed_event(busy="BUSY"))
        self.assertTrue(self.hidden(self.engine(self.entry())))
        self.assertFalse(self.hidden(self.engine(self.entry(show_as="busy"))))

    def test_title_contains_filter(self):
        self.fetch_result = ics_with(timed_event(summary="Homeoffice Luxembourg"))
        self.assertTrue(self.hidden(self.engine(self.entry(title_contains="Trier"))))
        self.assertFalse(self.hidden(self.engine(self.entry(title_contains="luxembourg"))))

    def test_from_till_override_widens_the_required_window(self):
        # Event covers 14:00-18:00 UTC = 16:00-20:00 local; requiring
        # 08:00-18:00 local can't be satisfied by it.
        self.fetch_result = ics_with(timed_event())
        self.assertTrue(self.hidden(self.engine(self.entry(from_hm="08:00", till_hm="18:00"))))

    def test_entry_scoped_to_other_course_imposes_no_requirement(self):
        self.fetch_result = ics_with()
        entry = self.entry(courses=("some-other-course",))
        self.assertFalse(self.hidden(self.engine(entry)))

    def test_all_day_oof_event_satisfies_the_requirement(self):
        self.fetch_result = ics_with(all_day_event())
        self.assertFalse(self.hidden(self.engine(self.entry())))

    def test_all_day_event_ignored_when_knob_off(self):
        self.fetch_result = ics_with(all_day_event())
        self.assertTrue(self.hidden(self.engine(self.entry(all_day_events_also_count=False))))


class BlocksModeTest(EngineFixture):
    def entry(self, **overrides):
        defaults = dict(name="own-calendar", mode="blocks", show_as="any",
                        use_booking_calendar=True)
        defaults.update(overrides)
        return make_conflict_calendar(**defaults)

    def test_overlapping_event_blocks(self):
        engine = self.engine(self.entry(), caldav_events=[ics_with(timed_event(busy="BUSY"))])
        self.assertTrue(self.hidden(engine))

    def test_empty_calendar_does_not_block(self):
        engine = self.engine(self.entry(), caldav_events=[])
        self.assertFalse(self.hidden(engine))

    def test_own_synced_event_excluded(self):
        own = timed_event(uid="example-org-yoga-class-1-2026-07-08@example.org")
        engine = self.engine(self.entry(), caldav_events=[ics_with(own)])
        self.assertFalse(self.hidden(engine))

    def test_ics_source_can_also_block(self):
        self.fetch_result = ics_with(timed_event(busy="BUSY"))
        entry = self.entry(use_booking_calendar=False, ics_url="https://x/feed.ics", name="feed")
        self.assertTrue(self.hidden(self.engine(entry)))

    def test_all_day_free_event_does_not_block_by_default(self):
        self.fetch_result = ics_with(all_day_event(busy="FREE", transp="TRANSPARENT"))
        entry = self.entry(use_booking_calendar=False, ics_url="https://x/feed.ics", name="feed")
        self.assertFalse(self.hidden(self.engine(entry)))

    def test_all_day_marker_escape_hatch(self):
        self.fetch_result = ics_with(all_day_event(summary="Conference #yoga-ok"))
        entry = self.entry(use_booking_calendar=False, ics_url="https://x/feed.ics",
                           name="feed", all_day_non_blocking_title_marker="#yoga-ok")
        self.assertFalse(self.hidden(self.engine(entry)))


class SourceErrorPolicyTest(EngineFixture):
    """The 2026-07-18 operator design: last-known-good ICS cache used
    indefinitely on fetch errors, WARNING-prefixed email to admin at most
    once per day per source; no cache yet (or a CalDAV error) hides the
    affected dates fail-closed, same email."""

    def entry(self, **overrides):
        defaults = dict(name="work", mode="requires", show_as="oof",
                        use_booking_calendar=False, ics_url="https://x/feed.ics")
        defaults.update(overrides)
        return make_conflict_calendar(**defaults)

    def test_successful_fetch_writes_the_last_known_good_copy(self):
        self.fetch_result = ics_with(timed_event())
        self.assertFalse(self.hidden(self.engine(self.entry())))
        cached = self.cache_dir / "work.ics"
        self.assertTrue(cached.exists())
        self.assertIn("BEGIN:VCALENDAR", cached.read_text())

    def test_cache_dir_is_gitignored_for_data_dir_snapshots(self):
        # conflict_cache/ lives inside data_dir, which git_snapshot
        # stages wholesale -- the fetched feeds must stay out of history.
        self.fetch_result = ics_with(timed_event())
        self.hidden(self.engine(self.entry()))
        gitignore = self.cache_dir.parent / ".gitignore"
        self.assertIn("conflict_cache/", gitignore.read_text())

    def test_fetch_error_falls_back_to_last_known_good_and_emails_once(self):
        self.fetch_result = ics_with(timed_event())
        self.assertFalse(self.hidden(self.engine(self.entry())))
        self.fetch_result = OSError("connection refused")
        engine = self.engine(self.entry())
        self.assertFalse(self.hidden(engine))  # cached copy still satisfies
        self.assertEqual(len(self.sent_mails), 1)
        subject, body = self.sent_mails[0]
        self.assertTrue(subject.startswith("WARNING:"), subject)
        self.assertIn("work", subject)
        self.assertIn("last successfully fetched copy", body)

    def test_warning_email_rate_limited_to_one_per_day(self):
        self.fetch_result = OSError("down")
        self.hidden(self.engine(self.entry()))
        self.hidden(self.engine(self.entry()))
        self.assertEqual(len(self.sent_mails), 1)
        self.now += timedelta(hours=25)
        self.hidden(self.engine(self.entry()))
        self.assertEqual(len(self.sent_mails), 2)

    def test_fetch_error_with_no_cache_hides_the_date(self):
        self.fetch_result = OSError("down")
        self.assertTrue(self.hidden(self.engine(self.entry())))
        self.assertEqual(len(self.sent_mails), 1)
        self.assertIn("HIDDEN", self.sent_mails[0][1])

    def test_non_ics_response_is_treated_as_an_error(self):
        self.fetch_result = "<html>login page</html>"
        self.assertTrue(self.hidden(self.engine(self.entry())))
        self.assertIn("not an ICS calendar", self.sent_mails[0][1])

    def test_caldav_error_hides_and_emails(self):
        class BrokenClient:
            def list_calendars(self):
                raise CalDAVError("HTTP 503")

            def query_events(self, href, start, end):
                raise CalDAVError("HTTP 503")

        settings = make_settings(courses=(self.course,), conflict_calendars=(
            make_conflict_calendar(),
        ))
        engine = ConflictEngine(
            settings, self.cache_dir,
            booking_client_fn=lambda: BrokenClient(),
            booking_href_fn=lambda: "/caldav/Bookings/",
            send_warning_mail=lambda s, b: self.sent_mails.append((s, b)),
            now_fn=lambda: self.now,
        )
        self.assertTrue(engine.occurrence_is_hidden(self.course, OCC_START, OCC_END))
        self.assertTrue(self.sent_mails[0][0].startswith("WARNING:"))

    def test_in_process_cache_avoids_refetching_within_ttl(self):
        calls = []
        self.fetch_result = ics_with(timed_event())
        engine = self.engine(self.entry())
        real_fetch = engine._fetch

        def counting_fetch(url):
            calls.append(url)
            return real_fetch(url)

        engine._fetch = counting_fetch
        self.hidden(engine)
        self.hidden(engine)
        self.assertEqual(len(calls), 1)


class MatchesEntryTest(unittest.TestCase):
    def test_transp_fallback_when_no_busystatus(self):
        entry = make_conflict_calendar(
            name="x", mode="blocks", show_as="free",
            use_booking_calendar=False, ics_url="https://x/f.ics",
        )
        occ = ics_feed.FeedOccurrence(
            start=OCC_START, end=OCC_END, all_day=False, summary="s",
            busy_status="", transparent=True,
        )
        self.assertTrue(_matches_entry(entry, occ))
        busy = ics_feed.FeedOccurrence(
            start=OCC_START, end=OCC_END, all_day=False, summary="s",
            busy_status="", transparent=False,
        )
        self.assertFalse(_matches_entry(entry, busy))
