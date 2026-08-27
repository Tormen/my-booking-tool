"""app/date_overrides.py -- the append-only store behind console-managed
exceptional dates.

The behaviours worth locking down here are the ones that are easy to get
subtly wrong later: that removal never deletes a line, that origin
decides who wins, and above all that a config `remove` row cannot kill an
admin entry (the trap the takeover flow walks straight into, since taking
a date over comments its settings.toml block out, which then makes
reconciliation append exactly that row)."""
import tempfile
import unittest
from pathlib import Path

from app import date_overrides as do


class OverrideStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = do.OverrideStore(self._tmp.name)

    def test_no_file_yet_is_empty_and_creates_nothing(self):
        self.assertEqual(self.store.read_all(), [])
        self.assertEqual(self.store.effective(), {})
        self.assertFalse(self.store.path.exists())

    def test_a_set_row_becomes_an_effective_entry(self):
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
            start_time="09:45", message="early",
        )
        entry = self.store.effective()[("sat-yoga", "2026-09-05")]
        self.assertEqual(entry.start_time, "09:45")
        self.assertEqual(entry.message, "early")
        self.assertIsNone(entry.duration_minutes)
        self.assertEqual(entry.origin, do.ORIGIN_ADMIN)

    def test_the_latest_row_per_date_wins(self):
        for start in ("09:45", "09:00", "08:30"):
            self.store.append(
                origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
                course_shortname="sat-yoga", occurrence_date="2026-09-05",
                start_time=start,
            )
        self.assertEqual(
            self.store.effective()[("sat-yoga", "2026-09-05")].start_time, "08:30"
        )

    def test_removal_appends_a_row_and_never_deletes_one(self):
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
            start_time="09:45",
        )
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_REMOVE,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
        )
        self.assertEqual(self.store.effective(), {})
        # The history is the reason this file exists at all: both the
        # setting and the un-setting must still be readable afterwards.
        history = self.store.history_for("sat-yoga", "2026-09-05")
        self.assertEqual([r["action"] for r in history], [do.ACTION_SET, do.ACTION_REMOVE])

    def test_config_rows_are_history_and_never_take_effect(self):
        # settings.toml entries reach the effective set from settings.toml
        # itself (see app.config), never from this file -- a config row
        # here is only a record that the entry existed.
        self.store.append(
            origin=do.ORIGIN_CONFIG, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
            start_time="09:45",
        )
        self.assertEqual(self.store.effective(), {})
        self.assertEqual(len(self.store.read_all()), 1)

    def test_a_config_remove_cannot_kill_an_admin_entry(self):
        # THE trap this design has to survive. Taking a config-owned date
        # over in the console comments its settings.toml block out, so the
        # next reconciliation appends a config `remove` for that exact
        # key -- newer than the admin row that caused it. If effect were
        # computed over all origins, the console's own change would
        # silently undo itself on the next restart.
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
            start_time="09:00",
        )
        self.store.append(
            origin=do.ORIGIN_CONFIG, action=do.ACTION_REMOVE,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
        )
        entry = self.store.effective().get(("sat-yoga", "2026-09-05"))
        self.assertIsNotNone(entry, "the admin entry must survive a config remove")
        self.assertEqual(entry.start_time, "09:00")

    def test_duration_is_optional_and_a_corrupt_cell_does_not_raise(self):
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
            start_time="09:45", duration_minutes=60,
        )
        self.assertEqual(
            self.store.effective()[("sat-yoga", "2026-09-05")].duration_minutes, 60
        )
        # A hand-mangled cell must degrade to "keep the normal duration",
        # not take the booking page down.
        text = self.store.path.read_text(encoding="utf-8").replace(",60,", ",banana,")
        self.store.path.write_text(text, encoding="utf-8")
        self.assertIsNone(
            self.store.effective()[("sat-yoga", "2026-09-05")].duration_minutes
        )

    def test_entries_for_different_dates_and_courses_are_independent(self):
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="sat-yoga", occurrence_date="2026-09-05", start_time="09:45",
        )
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="wed-yoga", occurrence_date="2026-09-05", start_time="16:00",
        )
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_REMOVE,
            course_shortname="sat-yoga", occurrence_date="2026-09-05",
        )
        self.assertEqual(list(self.store.effective()), [("wed-yoga", "2026-09-05")])


class ReconcileConfigRowsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = do.OverrideStore(self._tmp.name)

    def _entry(self, date="2026-09-05", start="09:45", dur=None, msg=""):
        return do.OverrideEntry(
            course_shortname="sat-yoga", date=date, start_time=start,
            duration_minutes=dur, message=msg, origin=do.ORIGIN_CONFIG, created_at="",
        )

    def test_a_new_config_entry_is_recorded(self):
        appended = do.reconcile_config_rows(self.store, [self._entry()])
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["action"], do.ACTION_SET)
        self.assertEqual(appended[0]["origin"], do.ORIGIN_CONFIG)

    def test_an_unchanged_entry_appends_nothing(self):
        do.reconcile_config_rows(self.store, [self._entry()])
        self.assertEqual(do.reconcile_config_rows(self.store, [self._entry()]), [])
        # No second write means no git commit and no growing file on every
        # single service start -- this runs on every startup.
        self.assertEqual(len(self.store.read_all()), 1)

    def test_a_changed_entry_is_recorded_as_a_new_set(self):
        do.reconcile_config_rows(self.store, [self._entry(start="09:45")])
        appended = do.reconcile_config_rows(self.store, [self._entry(start="09:00")])
        self.assertEqual([r["action"] for r in appended], [do.ACTION_SET])
        self.assertEqual(appended[0]["start_time"], "09:00")

    def test_an_entry_gone_from_settings_is_recorded_as_removed(self):
        do.reconcile_config_rows(self.store, [self._entry()])
        appended = do.reconcile_config_rows(self.store, [])
        self.assertEqual([r["action"] for r in appended], [do.ACTION_REMOVE])
        self.assertEqual(
            [r["action"] for r in self.store.history_for("sat-yoga", "2026-09-05")],
            [do.ACTION_SET, do.ACTION_REMOVE],
        )

    def test_reconciliation_never_touches_admin_entries(self):
        self.store.append(
            origin=do.ORIGIN_ADMIN, action=do.ACTION_SET,
            course_shortname="wed-yoga", occurrence_date="2026-09-09", start_time="16:00",
        )
        do.reconcile_config_rows(self.store, [])
        self.assertIn(("wed-yoga", "2026-09-09"), self.store.effective())


if __name__ == "__main__":
    unittest.main()
