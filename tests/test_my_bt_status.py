"""_print_live_status (scripts/my-bt) -- 2026-07-13: the "logged-in
users" table now reuses app.cli_checks.active_sessions_rows for its row
shaping (name/email/session start/last page/last activity/timeout), and
prints a `my-bt admin logout` hint whenever anyone's logged in -- both so
this table and `my-bt setup`'s own active-session gate/warning (and,
indirectly, the RPM's %pre gate, which just shells out to `my-bt status`
and reuses this rendering verbatim) can never drift apart. scripts/my-bt
has no .py extension and lives outside `app/`, so unittest can't import it
directly -- same importlib.machinery.SourceFileLoader workaround
tests/test_my_bt_argparse.py already established (see that file's own
docstring)."""
import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import cli_checks
from app.storage import USER_FIELDS, Store, _LockedCsv

MY_BT_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "my-bt")
_loader = importlib.machinery.SourceFileLoader("my_bt_status_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


def _payload(*sessions, maintenance_enabled=False):
    return {
        "version": "1.0.0",
        "server_time": "2026-07-13T09:24:00+00:00",
        "maintenance": {"enabled": maintenance_enabled, "message": "", "set_at": None},
        "sessions": list(sessions),
        "session_timeout_seconds": 4 * 3600,
    }


class PrintLiveStatusSessionsTableTest(unittest.TestCase):
    def _run(self, payload):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            my_bt_mod._print_live_status(payload)
        return out.getvalue()

    def test_no_sessions_shows_no_table(self):
        text = self._run(_payload())
        self.assertNotIn("logged-in users", text)

    def test_table_includes_name_email_and_timeout_columns(self):
        text = self._run(_payload({
            "kind": "guest", "who": "guest.one@example.org", "name": "Guest One",
            "connected_since": "2026-07-13T06:49:00+00:00",
            "last_seen": "2026-07-13T07:37:00+00:00", "last_page": "/my",
        }))
        self.assertIn("logged-in users:", text)
        self.assertIn("name", text)
        self.assertIn("email", text)
        self.assertIn("timeout (no activity)", text)
        self.assertIn("Guest One", text)
        self.assertIn("guest.one@example.org", text)
        self.assertIn("4h", text)

    def test_admin_session_shown_without_an_email(self):
        text = self._run(_payload({"kind": "admin", "who": "admin", "name": ""}))
        self.assertIn("admin", text)

    def test_logout_hint_shown_when_sessions_active(self):
        text = self._run(_payload({"kind": "admin", "who": "admin", "name": ""}))
        self.assertIn("my-bt admin logout EMAIL", text)
        self.assertIn("my-bt admin logout --all", text)

    def test_no_logout_hint_when_nobody_logged_in(self):
        text = self._run(_payload())
        self.assertNotIn("my-bt admin logout", text)


class ActivitySummaryStatsTest(unittest.TestCase):
    """app.cli_checks's 24h activity counters (2026-07-14: status should
    be a 360-degree summary incl. log stats and sessions/logins in the
    last 24h) -- the pure halves; the gathering/rendering is
    _print_activity_summary, tested end-to-end below."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def _user_with_login(self, email: str, last_login_at: str):
        user = self.store.upsert_user_for_booking(email, "X")
        with _LockedCsv(self.store.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user.user_id:
                    row["last_login_at"] = last_login_at
            write(rows, "test setup")
        return user

    def test_counts_logins_within_24h_only(self):
        self._user_with_login("fresh@example.org", "2026-07-14T09:00:00+00:00")   # 3h ago
        self._user_with_login("stale@example.org", "2026-07-12T09:00:00+00:00")   # 2 days ago
        self._user_with_login("never@example.org", "")
        self.assertEqual(cli_checks.count_recent_logins(self.store, now=self.now), 1)

    def test_counts_registrations_within_24h_only(self):
        u = self.store.upsert_user_for_booking("x@example.org", "X")
        fresh = self.store.add_registration("c", "2026-07-20", u.user_id, "")
        stale = self.store.add_registration("c", "2026-07-21", u.user_id, "")
        from app.storage import REG_FIELDS
        with _LockedCsv(self.store.registrations_path, REG_FIELDS) as (rows, write):
            for row in rows:
                row["registered_at"] = (
                    "2026-07-14T08:00:00+00:00" if row["registration_id"] == fresh.registration_id
                    else "2026-07-01T08:00:00+00:00"
                )
            write(rows, "test setup")
        self.assertEqual(cli_checks.count_recent_registrations(self.store, now=self.now), 1)

    def test_log_activity_stats_counts_nginx_window_and_errors(self):
        lines = [
            '1.2.3.4 - - [14/Jul/2026:11:00:00 +0000] "GET / HTTP/1.1" 200 1\n',
            '1.2.3.4 - - [14/Jul/2026:11:01:00 +0000] "GET /nope HTTP/1.1" 404 1\n',
            '1.2.3.4 - - [12/Jul/2026:11:00:00 +0000] "GET / HTTP/1.1" 500 1\n',  # outside window
        ]
        stats = cli_checks.log_activity_stats(lines, [], now=self.now)
        self.assertEqual(stats["nginx_requests"], 2)
        self.assertEqual(stats["nginx_errors"], 1)

    def test_log_activity_stats_none_nginx_lines_stays_none_not_zero(self):
        # "couldn't read the log" must be distinguishable from "quiet".
        stats = cli_checks.log_activity_stats(None, [], now=self.now)
        self.assertIsNone(stats["nginx_requests"])
        self.assertIsNone(stats["nginx_errors"])

    def test_log_activity_stats_counts_app_warnings_windowed_for_file_lines(self):
        app_lines = [
            "2026-07-14 10:00:00,000 WARNING rate limit blocked: guest login for a***@x.org\n",
            "2026-07-14 10:00:01,000 DEBUG GET /courses\n",                      # not WARNING+
            "2026-07-01 10:00:00,000 ERROR unhandled exception for GET /x\n",    # outside window
            "Jul 14 10:00:00 host my-bt[1]: WARNING journal-style line\n",       # journal: pre-windowed,
            # no file-format timestamp to parse -- counts as in-window
        ]
        stats = cli_checks.log_activity_stats(None, app_lines, now=self.now)
        self.assertEqual(stats["app_warnings"], 2)


class PrintActivitySummaryTest(unittest.TestCase):
    """_print_activity_summary end-to-end (real Store + settings file,
    journal mocked) -- the block `my-bt status` appends after the live
    summary."""

    def _run(self, with_settings: bool = True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        user = store.upsert_user_for_booking("active@example.org", "Active")
        store.touch_login(user.user_id)  # a real login, just now
        store.add_registration("c", "2026-07-20", user.user_id, "")
        settings_path = str(Path(tmp.name) / "settings.toml")
        if with_settings:
            Path(settings_path).write_text("[site]\nbase_url = 'https://example.org'\n", encoding="utf-8")
        args = my_bt_mod.build_parser().parse_args(
            ["--settings", settings_path, "--data-dir", tmp.name, "status"]
        )
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_journalctl_lines", return_value=[
            "Jul 14 10:00:00 host my-bt[1]: WARNING watchdog: nothing unusual found\n",
        ]):
            with contextlib.redirect_stdout(out):
                my_bt_mod._print_activity_summary(args)
        return out.getvalue()

    def test_shows_all_four_counters_and_the_detail_pointers(self):
        text = self._run()
        self.assertIn("activity (last 24h):", text)
        self.assertIn("accounts logged in", text)
        self.assertIn("bookings made", text)
        self.assertIn("nginx requests", text)
        self.assertIn("(not available -- no readable vhost access log)", text)
        self.assertIn("app warnings/errors (journal)", text)
        self.assertIn("my-bt admin log-errors", text)
        self.assertIn("my-bt admin health", text)
        # the freshly-logged-in account and the booking both counted
        self.assertRegex(text, r"accounts logged in\s+: 1")
        self.assertRegex(text, r"bookings made\s+: 1")
        self.assertRegex(text, r"app warnings/errors \(journal\)\s+: 1")

    def test_survives_a_missing_settings_toml(self):
        # status's liveness summary must never be taken down by the
        # activity block -- a fresh box with no settings.toml still works.
        text = self._run(with_settings=False)
        self.assertIn("activity (last 24h):", text)


if __name__ == "__main__":
    unittest.main()
