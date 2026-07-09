"""app/serve.py's check_directory_fsync_support_at_startup() -- the
"impossible to miss, even if nobody ever runs `my-bt admin health`" half
of the 2026-07-15 directory-fsync capability probe (see app/atomic_io.py
and app/cli_checks.py::check_directory_fsync_support for the other half,
re-checkable any time via `my-bt admin setup`/`admin health`).

Asserts on app.serve.log.error being CALLED (mocked directly) rather
than via unittest's assertLogs -- tests/helpers.py does a suite-wide
`logging.disable(logging.CRITICAL)` (deliberate: real WARNING/ERROR
lines from other tests' expected-failure paths would otherwise spam
every test run's output), which also defeats assertLogs since that
relies on the real logging pipeline being live. Mocking the module's own
`log` object sidesteps that entirely.

main() itself (argparse + wsgiref.make_server + serve_forever) is
exercised via systemd, not tests, same convention as app/watchdog.py's
own main()."""
import tempfile
import unittest
from unittest import mock

from app.serve import check_directory_fsync_support_at_startup

from .helpers import make_settings


class CheckDirectoryFsyncSupportAtStartupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.settings = make_settings()
        self.sent: list[tuple] = []

    def _fake_send_mail(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def test_supported_returns_true_and_sends_no_email(self):
        result = check_directory_fsync_support_at_startup(
            self.data_dir, self.settings, send_mail_fn=self._fake_send_mail,
        )
        self.assertTrue(result)
        self.assertEqual(self.sent, [])

    def test_unsupported_returns_false_and_logs_at_error_level(self):
        with mock.patch("app.serve.probe_dir_fsync_support", return_value=False), \
                mock.patch("app.serve.log") as m_log:
            result = check_directory_fsync_support_at_startup(
                self.data_dir, self.settings, send_mail_fn=self._fake_send_mail,
            )
        self.assertFalse(result)
        m_log.error.assert_called_once()
        self.assertIn("STARTUP CHECK FAILED", m_log.error.call_args[0][0])

    def test_unsupported_emails_admin_email(self):
        with mock.patch("app.serve.probe_dir_fsync_support", return_value=False), \
                mock.patch("app.serve.log"):
            check_directory_fsync_support_at_startup(
                self.data_dir, self.settings, send_mail_fn=self._fake_send_mail,
            )
        self.assertEqual(len(self.sent), 1)
        args, kwargs = self.sent[0]
        self.assertEqual(args[0], self.settings)
        self.assertEqual(args[1], self.settings.admin_email)
        self.assertIn("fsync", args[2].lower())

    def test_a_failed_alert_email_never_raises_or_blocks_startup(self):
        # SMTP being unreachable at boot must not be able to prevent the
        # server from starting -- see the function's own docstring.
        def broken_send_mail(*a, **kw):
            raise OSError("smtp unreachable")

        with mock.patch("app.serve.probe_dir_fsync_support", return_value=False), \
                mock.patch("app.serve.log") as m_log:
            try:
                result = check_directory_fsync_support_at_startup(
                    self.data_dir, self.settings, send_mail_fn=broken_send_mail,
                )
            except Exception as exc:
                self.fail(f"a failed alert email must not raise, raised {exc!r}")
        self.assertFalse(result)
        # The failure was still logged -- once for the original problem
        # (error), once for the alert email itself failing (warning) --
        # neither swallowed silently.
        m_log.error.assert_called_once()
        m_log.warning.assert_called_once()

    def test_default_send_mail_fn_is_the_real_emailer(self):
        # Confirms main() gets the real thing when it doesn't override
        # send_mail_fn itself -- only the tests above inject a fake.
        import app.emailer

        from app.serve import check_directory_fsync_support_at_startup as fn

        self.assertIs(fn.__defaults__[0], app.emailer.send_mail)


if __name__ == "__main__":
    unittest.main()
