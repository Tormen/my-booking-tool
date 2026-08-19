"""app/conflict.py -- the [[conflict_calendar]] engine: blocks/requires
semantics, show_as/title/all-day matching, per-course scoping, and the
ICS last-known-good cache + rate-limited WARNING email policy (operator's
explicit design, 2026-07-18). Every side effect injected -- no network,
no SMTP."""
import logging
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
                        # Return the real event UID (like the production
                        # CalDAV client does via ics.parse_uid), so a
                        # UID-keyed consumer such as the cancellation-blocker
                        # check sees the true UID, not a placeholder.
                        out.append((win.uid, text, "etag"))
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


class CancellationBlockerAlwaysOnTest(EngineFixture):
    """A "cancel entire session" blocker on the booking calendar must hide
    the date for EVERY course, even one scoped OUT of the blocks-mode
    booking_calendar entry (via all_courses_but) so its real availability
    is decided by a different source. The blocker is a booking-tool
    internal, keyed on its deterministic UID -- a genuine personal event
    on the same calendar still respects the scoping. (2026-07-24)"""

    def scoped_out_entry(self, **overrides):
        # The booking_calendar blocks entry, but this course is excluded --
        # so _booking_calendar_blocks_cover() is False and the dedicated
        # blocker check is what must catch a cancellation.
        defaults = dict(name="own-calendar", mode="blocks", show_as="any",
                        use_booking_calendar=True,
                        all_courses_but=(self.course.shortname,))
        defaults.update(overrides)
        return make_conflict_calendar(**defaults)

    def _blocker_event(self) -> str:
        from app import calendar_sync
        from datetime import date
        # UID depends only on base_url + shortname + date, all deterministic
        # from make_settings' defaults -- no engine needed to compute it.
        uid = calendar_sync.cancellation_blocker_uid(
            make_settings(), self.course.shortname, date(2026, 7, 8))
        return ics_with(timed_event(uid=uid, busy="BUSY", summary="CANCELED: yoga"))

    def test_blocker_hides_a_scoped_out_course(self):
        engine = self.engine(self.scoped_out_entry(), caldav_events=[self._blocker_event()])
        self.assertTrue(self.hidden(engine))

    def test_personal_event_does_not_block_a_scoped_out_course(self):
        # A non-blocker event on the booking calendar must NOT hide a
        # course scoped out of the entry -- that's the whole point of
        # all_courses_but; only the tool's own blocker UID counts here.
        engine = self.engine(self.scoped_out_entry(),
                             caldav_events=[ics_with(timed_event(busy="BUSY", uid="personal@x"))])
        self.assertFalse(self.hidden(engine))

    def test_no_dedicated_query_when_entry_already_covers_the_course(self):
        # Course NOT scoped out: the entry applies, so the generic overlap
        # check catches the blocker and no separate booking-calendar query
        # is issued (booking_client_fn must not be called by the blocker path).
        calls = []
        entry = make_conflict_calendar(name="own-calendar", mode="blocks",
                                       show_as="any", use_booking_calendar=True)
        engine = self.engine(entry, caldav_events=[])
        real = engine._booking_client_fn
        engine._booking_client_fn = lambda: (calls.append(1), real())[1]
        self.hidden(engine)
        self.assertEqual(len(calls), 1)  # the entry's own query only, not a second blocker query

    def test_booking_calendar_unreachable_hides_a_scoped_out_course(self):
        # Fail-closed: "if the booking calendar is unreachable no booking
        # should be done" -- even for a course otherwise decided by the ICS.
        class BrokenClient:
            def list_calendars(self):
                raise CalDAVError("read operation timed out")

            def query_events(self, href, start, end):
                raise CalDAVError("read operation timed out")

        settings = make_settings(courses=(self.course,),
                                 conflict_calendars=(self.scoped_out_entry(),))
        engine = ConflictEngine(
            settings, self.cache_dir,
            booking_client_fn=lambda: BrokenClient(),
            booking_href_fn=lambda: "/caldav/Bookings/",
            send_warning_mail=lambda s, b: self.sent_mails.append((s, b)),
            now_fn=lambda: self.now,
        )
        self.assertTrue(engine.occurrence_is_hidden(self.course, OCC_START, OCC_END))


class RecoveryEmailTest(EngineFixture):
    """A conflict source that failed and is then read successfully sends a
    one-off 'RESOLVED:' email -- the "calendar is back" notice (2026-07-24)."""

    def _engine(self, fail):
        entry = make_conflict_calendar(name="own-calendar", mode="blocks",
                                       show_as="any", use_booking_calendar=True)
        settings = make_settings(courses=(self.course,), conflict_calendars=(entry,))
        test = self

        class Toggle:
            def list_calendars(self):
                return {"Bookings": "/caldav/Bookings/"}

            def query_events(self, href, start, end):
                if fail[0]:
                    raise CalDAVError("The read operation timed out")
                return []

        return ConflictEngine(
            settings, self.cache_dir,
            booking_client_fn=lambda: Toggle(),
            booking_href_fn=lambda: "/caldav/Bookings/",
            send_warning_mail=lambda s, b: test.sent_mails.append((s, b)),
            now_fn=lambda: test.now,
        )

    def test_resolved_email_sent_when_source_recovers(self):
        fail = [True]
        engine = self._engine(fail)
        self.assertTrue(engine.occurrence_is_hidden(self.course, OCC_START, OCC_END))
        self.assertEqual(len(self.sent_mails), 1)
        self.assertTrue(self.sent_mails[0][0].startswith("WARNING:"))

        fail[0] = False
        self.now = self.now + timedelta(hours=2)
        self.assertFalse(engine.occurrence_is_hidden(self.course, OCC_START, OCC_END))
        self.assertEqual(len(self.sent_mails), 2)
        subject, body = self.sent_mails[1]
        self.assertTrue(subject.startswith("RESOLVED:"))
        self.assertIn("own-calendar", subject)
        self.assertIn("about 2h", body)  # downtime reported

    def test_no_resolved_email_when_source_was_never_failing(self):
        engine = self._engine([False])
        self.assertFalse(engine.occurrence_is_hidden(self.course, OCC_START, OCC_END))
        self.assertEqual(self.sent_mails, [])

    def test_recovery_resets_the_rate_limit_so_a_new_failure_re_alerts(self):
        fail = [True]
        engine = self._engine(fail)
        engine.occurrence_is_hidden(self.course, OCC_START, OCC_END)   # WARNING #1
        fail[0] = False
        self.now = self.now + timedelta(hours=2)
        engine.occurrence_is_hidden(self.course, OCC_START, OCC_END)   # RESOLVED
        fail[0] = True
        self.now = self.now + timedelta(minutes=5)  # <24h after WARNING #1
        engine.occurrence_is_hidden(self.course, OCC_START, OCC_END)   # WARNING #2 (would be suppressed without the reset)
        subjects = [s for s, _ in self.sent_mails]
        self.assertEqual(sum(s.startswith("WARNING:") for s in subjects), 2)
        self.assertEqual(sum(s.startswith("RESOLVED:") for s in subjects), 1)


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

    def test_changed_refetch_rotates_previous_version_for_diffing(self):
        # 2026-07-22: every CHANGED fetch preserves the prior copy as
        # <name>.ics.prev, so the operator can `diff` what just arrived.
        v1 = ics_with(timed_event(uid="a@x"))
        self.fetch_result = v1
        self.hidden(self.engine(self.entry()))          # first fetch: no .prev yet
        prev = self.cache_dir / "work.ics.prev"
        self.assertFalse(prev.exists())
        v2 = ics_with(timed_event(uid="a@x"), timed_event(uid="b@x", start="20260708T170000Z"))
        self.fetch_result = v2
        self.hidden(self.engine(self.entry()))          # changed fetch: rotates
        self.assertEqual((self.cache_dir / "work.ics").read_text(), v2)
        self.assertEqual(prev.read_text(), v1)

    def test_unchanged_refetch_does_not_clobber_previous_version(self):
        # An identical re-fetch (every cache_minutes) must leave .prev
        # pointing at the last genuinely DIFFERENT version, not flatten it.
        v1 = ics_with(timed_event(uid="a@x"))
        self.fetch_result = v1
        self.hidden(self.engine(self.entry()))
        v2 = ics_with(timed_event(uid="a@x"), timed_event(uid="b@x", start="20260708T170000Z"))
        self.fetch_result = v2
        self.hidden(self.engine(self.entry()))          # v1 -> v2, .prev = v1
        self.fetch_result = v2                            # same content again
        self.hidden(self.engine(self.entry()))          # no change -> .prev untouched
        self.assertEqual((self.cache_dir / "work.ics.prev").read_text(), v1)

    def test_fetch_failure_is_logged_at_error_level(self):
        # 2026-07-22: any failure fetching the .ics is logged at ERROR, so it
        # stays visible in the default (MY_BOOKING_DEBUG-off) log even though
        # bookings continue against the cached copy. helpers.py disables
        # logging globally, so re-enable it just for this assertion.
        self.fetch_result = ics_with(timed_event())
        self.hidden(self.engine(self.entry()))            # seed the last-known-good copy
        self.fetch_result = OSError("connection refused")
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("my-booking.conflict", level="ERROR") as cm:
                self.hidden(self.engine(self.entry()))
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("fetch failed" in line for line in cm.output), cm.output)

    def test_successful_fetch_is_not_logged_above_debug(self):
        # The routine "fetched N bytes" line is DEBUG only -- a normal fetch
        # must never surface in the default WARNING-level log.
        self.fetch_result = ics_with(timed_event())
        logging.disable(logging.NOTSET)
        try:
            with self.assertNoLogs("my-booking.conflict", level="WARNING"):
                self.hidden(self.engine(self.entry()))
        finally:
            logging.disable(logging.CRITICAL)

    def test_debug_mode_backs_up_with_cp_a_before_fetch_and_traces(self):
        # 2026-07-22: `debug = true` on a source logs a full before/after
        # trace at WARNING and backs up the CURRENT .ics to .ics.prev with a
        # real /bin/cp -a BEFORE fetching -- so .ics.prev is byte-identical
        # to the pre-fetch .ics, and a second fetch in one request would show
        # a second FETCH BEGIN block.
        v1 = ics_with(timed_event(uid="a@x"))
        self.fetch_result = v1
        self.hidden(self.engine(self.entry(debug=True)))          # seed .ics = v1
        v2 = ics_with(timed_event(uid="a@x"), timed_event(uid="b@x", start="20260708T170000Z"))
        self.fetch_result = v2
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("my-booking.conflict", level="WARNING") as cm:
                self.hidden(self.engine(self.entry(debug=True)))  # fresh engine -> real fetch
        finally:
            logging.disable(logging.CRITICAL)
        out = "\n".join(cm.output)
        for marker in ("FETCH BEGIN", "BEFORE  .ics", "/bin/cp -a", "FETCHED", "AFTER   .ics", "FETCH END"):
            self.assertIn(marker, out, out)
        # cp -a copied the pre-fetch .ics (v1) verbatim into .prev
        self.assertEqual((self.cache_dir / "work.ics.prev").read_text(), v1)
        self.assertEqual((self.cache_dir / "work.ics").read_text(), v2)

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
