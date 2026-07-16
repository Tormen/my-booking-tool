"""`my-bt admin csp-violations` (scripts/my-bt::cmd_admin_csp_violations) --
2026-07-13: full, ungrouped-beyond-5 detail on CSP violation reports
browsers have sent (app/webapp.py::csp_report), on demand -- same
app.cli_checks.find_csp_violations() parsing `my-bt health`/`admin setup`
already summarize. scripts/my-bt has no .py extension and lives outside
`app/`, so unittest can't import it directly -- same
importlib.machinery.SourceFileLoader workaround tests/test_my_bt_status.py
already established."""
import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

MY_BT_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "my-bt")
_loader = importlib.machinery.SourceFileLoader("my_bt_csp_violations_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


class CmdAdminCspViolationsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.log_path = self.home / "my-booking.log"
        self.settings_path = self.home / "settings.toml"

    def _write_settings(self, log_file: str | None) -> None:
        # None = no [logging] section at all (the on-by-default case,
        # since 2026-07-16); "" = explicitly disabled file logging.
        lines = ["[site]", 'admin_email = "admin@example.org"', ""]
        if log_file is not None:
            lines += ["[logging]", f'log_file = "{log_file}"', ""]
        self.settings_path.write_text("\n".join(lines), encoding="utf-8")

    def _run(self):
        args = types.SimpleNamespace(settings=str(self.settings_path))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            my_bt_mod.cmd_admin_csp_violations(args)
        return out.getvalue()

    def test_log_file_explicitly_disabled(self):
        self._write_settings(log_file="")
        text = self._run()
        self.assertIn("disabled", text)

    def test_default_log_file_not_existing_yet_is_graceful(self):
        # No [logging] section -> config.DEFAULT_LOG_FILE applies (file
        # logging is on by default since 2026-07-16); before the service
        # ever started, that file doesn't exist -- must say so and exit
        # cleanly, not error out. DEFAULT_LOG_FILE is patched to a path
        # inside this test's tmpdir so the test can't be poisoned by a
        # real /var/lib/my-booking on the machine running the suite.
        self._write_settings(log_file=None)
        with mock.patch("app.config.DEFAULT_LOG_FILE", str(self.home / "never-created.log")):
            text = self._run()
        self.assertIn("does not exist yet", text)

    def test_log_file_configured_but_no_violations(self):
        self._write_settings(log_file=str(self.log_path))
        self.log_path.write_text("2026-07-13 12:00:00,000 WARNING nothing relevant\n", encoding="utf-8")
        text = self._run()
        self.assertIn("no CSP violations", text)

    def test_violations_are_listed_with_counts(self):
        self._write_settings(log_file=str(self.log_path))
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"{ts},000 WARNING CSP violation report from 1.2.3.4: "
            "blocked-uri='eval' violated-directive='script-src' document-uri='https://example.org/'\n"
        )
        self.log_path.write_text(line * 2, encoding="utf-8")
        text = self._run()
        self.assertIn("2 CSP violation report(s)", text)
        self.assertIn("2x blocked-uri='eval'", text)


if __name__ == "__main__":
    unittest.main()
