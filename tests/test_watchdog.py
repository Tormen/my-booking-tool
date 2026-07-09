import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.storage import STATUS_PENDING_CONFIRMATION, Store
from app.watchdog import (
    check_app_log_rate_limit_blocks,
    check_nginx_bursts,
    check_now,
    check_pending_signup_burst,
    check_sshd_failures,
    run_watchdog,
)

from .helpers import make_settings

NOW = datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc)


class PendingSignupBurstTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(watchdog_pending_signup_threshold=3)

    def _pending(self, registered_at: str, email: str):
        u = self.store.upsert_user_for_booking(email, "Guest")
        self.store.add_registration(
            "c", "2099-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION
        )
        # add_registration always stamps registered_at=now(); overwrite it
        # directly since we need controlled timestamps for the window test.
        regs = self.store.all_registrations()
        reg = regs[-1]
        from dataclasses import replace
        self.store.replace_all_registrations(
            [r if r.registration_id != reg.registration_id else replace(r, registered_at=registered_at)
             for r in regs]
        )

    def test_below_threshold_is_silent(self):
        self._pending("2026-07-05T14:50:00+00:00", "a@example.org")
        self._pending("2026-07-05T14:51:00+00:00", "b@example.org")
        self.assertEqual(check_pending_signup_burst(self.store, self.settings, now=NOW), [])

    def test_at_threshold_fires(self):
        for i in range(3):
            self._pending("2026-07-05T14:5%d:00+00:00" % i, f"user{i}@example.org")
        alerts = check_pending_signup_burst(self.store, self.settings, now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("3 new pending", alerts[0])

    def test_outside_window_does_not_count(self):
        for i in range(3):
            self._pending("2026-07-05T10:0%d:00+00:00" % i, f"user{i}@example.org")  # ~5h before NOW
        self.assertEqual(check_pending_signup_burst(self.store, self.settings, now=NOW), [])

    def test_zero_threshold_disables_check(self):
        settings = make_settings(watchdog_pending_signup_threshold=0)
        for i in range(10):
            self._pending("2026-07-05T14:5%d:00+00:00" % (i % 6), f"user{i}@example.org")
        self.assertEqual(check_pending_signup_burst(self.store, settings, now=NOW), [])


class AppLogRateLimitBlocksTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(watchdog_rate_limit_block_threshold=3)

    def _line(self, minute: int) -> str:
        return f"2026-07-05 14:{minute:02d}:00,123 WARNING rate limit blocked: guest login for k***@example.org"

    def test_below_threshold_is_silent(self):
        lines = [self._line(50), self._line(51)]
        self.assertEqual(check_app_log_rate_limit_blocks(lines, self.settings, now=NOW), [])

    def test_at_threshold_fires(self):
        lines = [self._line(50), self._line(51), self._line(52)]
        alerts = check_app_log_rate_limit_blocks(lines, self.settings, now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("3 rate-limiter rejections", alerts[0])

    def test_unrelated_lines_are_ignored(self):
        lines = [
            "2026-07-05 14:59:00,000 WARNING retention purge: nothing to remove (0 rows checked)",
            "2026-07-05 14:59:01,000 DEBUG sending mail",
        ]
        self.assertEqual(check_app_log_rate_limit_blocks(lines, self.settings, now=NOW), [])

    def test_lines_outside_window_do_not_count(self):
        lines = [self._line(m) for m in (0, 1, 2)]  # 14:00-14:02, > 15 min before NOW=15:00
        self.assertEqual(check_app_log_rate_limit_blocks(lines, self.settings, now=NOW), [])

    def test_unparseable_timestamp_is_not_counted(self):
        lines = ["garbage rate limit blocked: guest login for x@example.org"] * 5
        self.assertEqual(check_app_log_rate_limit_blocks(lines, self.settings, now=NOW), [])


class NginxBurstsTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(
            watchdog_nginx_access_log="/var/log/nginx/access.log",
            watchdog_nginx_request_threshold=5,
            watchdog_nginx_error_rate_threshold=0.5,
        )

    def _line(self, ip: str, minute: int, status: str = "200") -> str:
        return (
            f'{ip} - - [05/Jul/2026:14:{minute:02d}:00 +0000] '
            f'"GET /book/yoga-class-1 HTTP/1.1" {status} 512 "-" "curl/7.0"'
        )

    def test_disabled_when_no_access_log_configured(self):
        settings = make_settings(watchdog_nginx_access_log=None)
        lines = [self._line("203.0.113.5", 50) for _ in range(20)]
        self.assertEqual(check_nginx_bursts(lines, settings, now=NOW), [])

    def test_request_burst_from_one_ip_fires(self):
        lines = [self._line("203.0.113.5", 55) for _ in range(5)]
        alerts = check_nginx_bursts(lines, self.settings, now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("203.0.113.5 made 5 requests", alerts[0])

    def test_below_request_threshold_is_silent(self):
        lines = [self._line("203.0.113.5", 55) for _ in range(4)]
        self.assertEqual(check_nginx_bursts(lines, self.settings, now=NOW), [])

    def test_error_rate_only_evaluated_with_enough_volume(self):
        # 3 requests, all errors -- 100% error rate, but too few requests to
        # be meaningful (below _MIN_REQUESTS_FOR_ERROR_RATE).
        lines = [self._line("203.0.113.9", 55, status="404") for _ in range(3)]
        self.assertEqual(check_nginx_bursts(lines, self.settings, now=NOW), [])

    def test_high_error_rate_with_enough_volume_fires(self):
        # request_threshold raised well above the 20-request total here, so
        # only the error-rate branch (the elif) can fire, not the
        # request-count branch.
        settings = make_settings(
            watchdog_nginx_access_log="/var/log/nginx/access.log",
            watchdog_nginx_request_threshold=1000,
            watchdog_nginx_error_rate_threshold=0.5,
        )
        ip = "203.0.113.9"
        # All within the last 15 min before NOW=15:00 (window is 14:45-15:00).
        lines = [self._line(ip, 50, status="404") for _ in range(15)]
        lines += [self._line(ip, 50, status="200") for _ in range(5)]  # 15/20 = 75% errors
        alerts = check_nginx_bursts(lines, settings, now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("75%", alerts[0])

    def test_requests_outside_window_are_ignored(self):
        lines = [self._line("203.0.113.5", m) for m in range(5)]  # 14:00-14:04, way before NOW=15:00
        self.assertEqual(check_nginx_bursts(lines, self.settings, now=NOW), [])

    def test_malformed_lines_are_ignored(self):
        lines = ["not a valid nginx line at all"] * 10
        self.assertEqual(check_nginx_bursts(lines, self.settings, now=NOW), [])


class SshdFailuresTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(watchdog_sshd_failure_threshold=3)

    def test_below_threshold_is_silent(self):
        lines = ["sshd[1]: Failed password for root from 203.0.113.5 port 1 ssh2"] * 2
        self.assertEqual(check_sshd_failures(lines, self.settings), [])

    def test_at_threshold_fires(self):
        lines = ["sshd[1]: Failed password for root from 203.0.113.5 port 1 ssh2"] * 3
        alerts = check_sshd_failures(lines, self.settings)
        self.assertEqual(len(alerts), 1)
        self.assertIn("3 failed SSH password attempts", alerts[0])

    def test_zero_threshold_disables_check(self):
        settings = make_settings(watchdog_sshd_failure_threshold=0)
        lines = ["sshd[1]: Failed password for root from 203.0.113.5 port 1 ssh2"] * 10
        self.assertEqual(check_sshd_failures(lines, settings), [])

    def test_unrelated_lines_do_not_count(self):
        lines = ["sshd[1]: Accepted password for tormen from 203.0.113.5 port 1 ssh2"] * 5
        self.assertEqual(check_sshd_failures(lines, self.settings), [])


class RunWatchdogTest(unittest.TestCase):
    """Integration across all four checks plus the single combined email."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.settings = make_settings(
            watchdog_pending_signup_threshold=100,
            watchdog_rate_limit_block_threshold=100,
            watchdog_nginx_access_log=None,
            watchdog_sshd_failure_threshold=100,
        )

    def test_clean_run_sends_no_email(self):
        with patch("app.watchdog.send_mail") as mock_send:
            alerts = run_watchdog(self.store, self.settings, now=NOW)
        self.assertEqual(alerts, [])
        mock_send.assert_not_called()

    def test_one_firing_check_sends_one_combined_email(self):
        settings = make_settings(
            watchdog_pending_signup_threshold=100,
            watchdog_rate_limit_block_threshold=1,
            watchdog_nginx_access_log=None,
            watchdog_sshd_failure_threshold=100,
        )
        line = "2026-07-05 14:59:00,000 WARNING rate limit blocked: admin login from 203.0.113.5"
        with patch("app.watchdog.send_mail") as mock_send:
            alerts = run_watchdog(self.store, settings, app_log_lines=[line], now=NOW)
        self.assertEqual(len(alerts), 1)
        mock_send.assert_called_once()
        call_settings, to_addr, subject, body = mock_send.call_args[0]
        self.assertEqual(to_addr, settings.admin_email)
        self.assertIn("watchdog", subject.lower())
        self.assertIn("rate-limiter rejections", body)

    def test_disabled_watchdog_never_checks_anything(self):
        settings = make_settings(watchdog_enabled=False, watchdog_pending_signup_threshold=1)
        u = self.store.upsert_user_for_booking("a@example.org", "A")
        self.store.add_registration("c", "2099-01-01", u.user_id, "", status=STATUS_PENDING_CONFIRMATION)
        with patch("app.watchdog.send_mail") as mock_send:
            alerts = run_watchdog(self.store, settings, now=NOW)
        self.assertEqual(alerts, [])
        mock_send.assert_not_called()


class CheckNowTest(unittest.TestCase):
    """check_now() -- the real-I/O gathering step factored out of main()
    2026-07-14 so `my-bt admin watchdog-check` (scripts/my-bt) can run the
    exact same sweep on demand, not just from the systemd timer. Patches
    every real-I/O helper so this stays a pure unit test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)

    def test_gathers_every_source_and_forwards_to_run_watchdog(self):
        settings = make_settings(
            watchdog_rate_limit_block_threshold=1, watchdog_sshd_failure_threshold=1,
        )
        with (
            patch("app.watchdog._read_lines", side_effect=lambda path: [f"line-from-{path}"]) as mock_read,
            patch("app.watchdog._sshd_lines_since", return_value=["sshd-line"]) as mock_sshd,
            patch("app.watchdog._read_fail2ban_banned_ips", return_value=frozenset({"203.0.113.5"})) as mock_banned,
            patch("app.watchdog.run_watchdog", return_value=["some alert"]) as mock_run,
        ):
            result = check_now(self.store, settings)
        self.assertEqual(result, ["some alert"])
        mock_read.assert_any_call(settings.log_file)
        mock_read.assert_any_call(settings.watchdog_nginx_access_log)
        mock_sshd.assert_called_once_with(settings.watchdog_window_minutes)
        mock_banned.assert_called_once()
        _store_arg, _settings_arg = mock_run.call_args[0]
        kwargs = mock_run.call_args[1]
        self.assertEqual(kwargs["sshd_log_lines"], ["sshd-line"])
        self.assertEqual(kwargs["banned_ips"], frozenset({"203.0.113.5"}))

    def test_skips_sshd_journal_when_threshold_disabled(self):
        settings = make_settings(watchdog_sshd_failure_threshold=0)
        with (
            patch("app.watchdog._read_lines", return_value=[]),
            patch("app.watchdog._sshd_lines_since") as mock_sshd,
            patch("app.watchdog._read_fail2ban_banned_ips", return_value=frozenset()),
            patch("app.watchdog.run_watchdog", return_value=[]),
        ):
            check_now(self.store, settings)
        mock_sshd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
