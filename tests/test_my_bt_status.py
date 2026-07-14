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
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
