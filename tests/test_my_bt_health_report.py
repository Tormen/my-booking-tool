"""`my-bt admin health report`/`errors` (scripts/my-bt::
cmd_admin_health_report/cmd_admin_health_errors) -- 2026-07-13: on-demand
forensic log aggregator across nginx global/vhost logs, the app log,
sshd, and the my-booking service/timer journals, for a --last/--since/
--till window (default: since nginx's own last restart). scripts/my-bt
has no .py extension and lives outside `app/`, so unittest can't import it
directly -- same importlib.machinery.SourceFileLoader workaround
tests/test_my_bt_status.py already established."""
import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MY_BT_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "my-bt")
_loader = importlib.machinery.SourceFileLoader("my_bt_health_report_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


class JournalctlLinesTest(unittest.TestCase):
    def test_returns_lines_with_trailing_newline(self):
        import datetime
        start = datetime.datetime(2026, 7, 13, 11, 0, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.timezone.utc)
        fake_result = types.SimpleNamespace(returncode=0, stdout="line one\nline two")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            lines = my_bt_mod._journalctl_lines("sshd", start, end)
        self.assertEqual(lines, ["line one\n", "line two\n"])
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["journalctl", "-u", "sshd"])

    def test_nonzero_exit_returns_empty(self):
        import datetime
        start = end = datetime.datetime(2026, 7, 13, 11, 0, tzinfo=datetime.timezone.utc)
        fake_result = types.SimpleNamespace(returncode=1, stdout="")
        with patch("subprocess.run", return_value=fake_result):
            self.assertEqual(my_bt_mod._journalctl_lines("sshd", start, end), [])

    def test_oserror_returns_empty(self):
        import datetime
        start = end = datetime.datetime(2026, 7, 13, 11, 0, tzinfo=datetime.timezone.utc)
        with patch("subprocess.run", side_effect=OSError("no journalctl")):
            self.assertEqual(my_bt_mod._journalctl_lines("sshd", start, end), [])


class CmdAdminHealthReportErrorsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.settings_path = self.home / "settings.toml"
        self.settings_path.write_text('[site]\nadmin_email = "admin@example.org"\n', encoding="utf-8")

    def _args(self, last=None, since=None, till=None):
        return types.SimpleNamespace(settings=str(self.settings_path), last=last, since=since, till=till)

    def _run(self, func, *a, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            func(*a, **kw)
        return out.getvalue()

    def test_report_prints_header_and_calls_journalctl_three_times(self):
        with patch("app.cli_checks.health_report_log_sources", return_value=[]), \
             patch.object(my_bt_mod, "_journalctl_lines", return_value=[]) as mock_j:
            text = self._run(my_bt_mod.cmd_admin_health_report, self._args(last="2h"))
        self.assertIn("my-bt admin health report", text)
        self.assertIn("last 2h", text)
        # sshd + my-booking.service + my-booking-watchdog.service
        self.assertEqual(mock_j.call_count, 3)
        units = [c.args[0] for c in mock_j.call_args_list]
        self.assertEqual(units, ["sshd", "my-booking.service", "my-booking-watchdog.service"])

    def test_errors_prints_errors_header(self):
        with patch("app.cli_checks.health_report_log_sources", return_value=[]), \
             patch.object(my_bt_mod, "_journalctl_lines", return_value=[]):
            text = self._run(my_bt_mod.cmd_admin_health_errors, self._args(last="1h"))
        self.assertIn("my-bt admin health errors", text)

    def test_errors_filters_sshd_to_failed_password_lines(self):
        def fake_journalctl(unit, start, end):
            if unit == "sshd":
                return ["Accepted password for tormen\n", "Failed password for root\n"]
            return []

        with patch("app.cli_checks.health_report_log_sources", return_value=[]), \
             patch.object(my_bt_mod, "_journalctl_lines", side_effect=fake_journalctl):
            text = self._run(my_bt_mod.cmd_admin_health_errors, self._args(last="1h"))
        self.assertNotIn("Accepted password", text)
        self.assertIn("Failed password for root", text)

    def test_invalid_duration_exits_with_error(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                my_bt_mod.cmd_admin_health_report(self._args(last="not-a-duration"))
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
