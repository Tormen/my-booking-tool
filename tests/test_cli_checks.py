import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli_checks, maintenance, site_render


def _levels(checks):
    return {label: level for label, level, _ in checks}


def _both(checks):
    return {label: (label, level, detail) for label, level, detail in checks}


class CheckSecretsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _raw(self, **paths) -> dict:
        return {
            "booking_calendar": {"password_file": paths.get("caldav_password")},
            "smtp": {"password_file": paths.get("smtp_password")},
            "admin": {"password_hash_file": paths.get("admin_password_hash")},
            "privacy": {"erasure_pepper_file": paths.get("erasure_pepper")},
        }

    def test_not_configured_warns(self):
        checks = cli_checks.check_secrets(self._raw())
        self.assertTrue(all(level == "warn" for _, level, _ in checks))

    def test_missing_file_fails(self):
        p = str(self.dir / "caldav_password")
        checks = cli_checks.check_secrets(self._raw(caldav_password=p))
        self.assertEqual(_levels(checks)["secret: caldav_password"], "fail")

    def test_present_correct_mode_is_ok(self):
        p = self.dir / "smtp_password"
        p.write_text("hunter2")
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(smtp_password=str(p)))
        self.assertEqual(_levels(checks)["secret: smtp_password"], "ok")

    def test_wrong_mode_warns(self):
        p = self.dir / "smtp_password"
        p.write_text("hunter2")
        p.chmod(0o644)
        checks = cli_checks.check_secrets(self._raw(smtp_password=str(p)))
        self.assertEqual(_levels(checks)["secret: smtp_password"], "warn")

    def test_admin_password_hash_without_dollar_sign_fails(self):
        p = self.dir / "admin_password_hash"
        p.write_text("plaintextpassword")
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(admin_password_hash=str(p)))
        self.assertEqual(_levels(checks)["secret: admin_password_hash"], "fail")

    def test_admin_password_hash_with_dollar_sign_is_ok(self):
        p = self.dir / "admin_password_hash"
        p.write_text("scrypt$deadbeef$abc123")
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(admin_password_hash=str(p)))
        self.assertEqual(_levels(checks)["secret: admin_password_hash"], "ok")

    def test_erasure_pepper_wrong_length_warns(self):
        p = self.dir / "erasure_pepper"
        p.write_text("abcd")  # valid hex, but not 32 bytes
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(erasure_pepper=str(p)))
        self.assertEqual(_levels(checks)["secret: erasure_pepper"], "warn")

    def test_erasure_pepper_invalid_hex_fails(self):
        p = self.dir / "erasure_pepper"
        p.write_text("not-hex-at-all!!")
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(erasure_pepper=str(p)))
        self.assertEqual(_levels(checks)["secret: erasure_pepper"], "fail")

    def test_erasure_pepper_correct_is_ok(self):
        p = self.dir / "erasure_pepper"
        p.write_text("ab" * 32)
        p.chmod(0o600)
        checks = cli_checks.check_secrets(self._raw(erasure_pepper=str(p)))
        self.assertEqual(_levels(checks)["secret: erasure_pepper"], "ok")


class SummarizeProblemsTest(unittest.TestCase):
    """2026-07-08: all warnings are repeated at the end of setup
    and status explicitly. summarize_problems() is the shared formatter
    `my-bt admin health`/plain `my-bt admin setup`/`my-bt admin setup -i`
    all use to repeat every non-ok check right before their own final
    pass/fail summary line."""

    def test_ok_checks_are_dropped(self):
        checks = [("a", "ok", "fine"), ("b", "ok", "")]
        self.assertEqual(cli_checks.summarize_problems(checks), [])

    def test_warn_and_fail_are_kept_in_order_with_detail(self):
        checks = [
            ("a", "ok", "fine"),
            ("b", "warn", "needs a look"),
            ("c", "fail", "broken"),
        ]
        self.assertEqual(
            cli_checks.summarize_problems(checks),
            ["[WARN] b -- needs a look", "[FAIL] c -- broken"],
        )

    def test_blank_detail_has_no_trailing_dash(self):
        checks = [("a", "warn", "")]
        self.assertEqual(cli_checks.summarize_problems(checks), ["[WARN] a"])


class CheckRpmnewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_no_rpmnew_is_ok(self):
        p = self.dir / "settings.toml"
        p.write_text("x")
        checks = cli_checks.check_rpmnew([str(p)])
        self.assertEqual(checks[0][1], "ok")

    def test_rpmnew_present_warns_with_vimdiff_command(self):
        p = self.dir / "settings.toml"
        p.write_text("x")
        (self.dir / "settings.toml.rpmnew").write_text("y")
        checks = cli_checks.check_rpmnew([str(p)])
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("vimdiff", detail)

    def test_checks_multiple_paths_independently(self):
        a = self.dir / "a.toml"
        b = self.dir / "b.tmpl"
        a.write_text("x")
        b.write_text("y")
        (self.dir / "a.toml.rpmnew").write_text("z")
        checks = cli_checks.check_rpmnew([str(a), str(b)])
        levels = [level for _, level, _ in checks]
        self.assertEqual(levels, ["warn", "ok"])


def _sessions_payload(*sessions, timeout=14400):
    return {"sessions": list(sessions), "session_timeout_seconds": timeout}


class FormatSessionTimeoutTest(unittest.TestCase):
    def test_none_is_unknown(self):
        self.assertEqual(cli_checks.format_session_timeout(None), "?")

    def test_zero_is_unknown(self):
        self.assertEqual(cli_checks.format_session_timeout(0), "?")

    def test_exact_hours(self):
        self.assertEqual(cli_checks.format_session_timeout(4 * 3600), "4h")

    def test_hours_and_minutes(self):
        self.assertEqual(cli_checks.format_session_timeout(3600 + 30 * 60), "1h30m")


class ActiveSessionsRowsTest(unittest.TestCase):
    def test_guest_row_shows_name_and_email(self):
        payload = _sessions_payload({
            "kind": "guest", "who": "ines@example.org", "name": "Guest One",
            "connected_since": "2026-07-13T06:49:00+00:00", "last_seen": "2026-07-13T07:37:00+00:00",
            "last_page": "/my",
        })
        rows = cli_checks.active_sessions_rows(payload)
        self.assertEqual(rows[0]["name"], "Guest One")
        self.assertEqual(rows[0]["email"], "ines@example.org")
        self.assertEqual(rows[0]["last page"], "/my")
        self.assertEqual(rows[0]["timeout (no activity)"], "4h")

    def test_admin_row_has_no_email(self):
        payload = _sessions_payload({"kind": "admin", "who": "admin", "name": ""})
        rows = cli_checks.active_sessions_rows(payload)
        self.assertEqual(rows[0]["name"], "admin")
        self.assertEqual(rows[0]["email"], "-")

    def test_guest_with_no_name_on_file_shows_placeholder(self):
        payload = _sessions_payload({"kind": "guest", "who": "x@example.org", "name": ""})
        rows = cli_checks.active_sessions_rows(payload)
        self.assertEqual(rows[0]["name"], "(no name on file)")

    def test_never_navigated_again_shows_none_yet(self):
        payload = _sessions_payload({
            "kind": "guest", "who": "x@example.org", "name": "X",
            "connected_since": "2026-07-13T06:49:00+00:00", "last_seen": None,
        })
        rows = cli_checks.active_sessions_rows(payload)
        self.assertEqual(rows[0]["last activity"], "(none yet)")

    def test_empty_sessions_is_empty_rows(self):
        self.assertEqual(cli_checks.active_sessions_rows(_sessions_payload()), [])


class FormatActiveSessionsOverviewTest(unittest.TestCase):
    def test_no_sessions(self):
        self.assertEqual(cli_checks.format_active_sessions_overview(_sessions_payload()), "(no active sessions)")

    def test_includes_table_and_logout_hint(self):
        payload = _sessions_payload({
            "kind": "guest", "who": "ines@example.org", "name": "Guest One",
            "connected_since": "2026-07-13T06:49:00+00:00", "last_seen": "2026-07-13T07:37:00+00:00",
        })
        out = cli_checks.format_active_sessions_overview(payload)
        self.assertIn("Guest One", out)
        self.assertIn("ines@example.org", out)
        self.assertIn("my-bt admin logout EMAIL", out)
        self.assertIn("my-bt admin logout --all", out)


class CheckActiveSessionsTest(unittest.TestCase):
    def test_unreachable_service_is_ok_no_findings(self):
        with patch("app.cli_checks.fetch_active_sessions", return_value=(None, "Connection refused")):
            self.assertEqual(cli_checks.check_active_sessions(), [])

    def test_no_sessions_is_no_findings(self):
        with patch("app.cli_checks.fetch_active_sessions", return_value=(_sessions_payload(), None)):
            self.assertEqual(cli_checks.check_active_sessions(), [])

    def test_active_sessions_is_a_warn_with_a_compact_summary(self):
        payload = _sessions_payload(
            {"kind": "guest", "who": "ines@example.org", "name": "Ines"},
            {"kind": "admin", "who": "admin", "name": ""},
        )
        with patch("app.cli_checks.fetch_active_sessions", return_value=(payload, None)):
            checks = cli_checks.check_active_sessions()
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "active sessions")
        self.assertEqual(level, "warn")
        self.assertIn("2 active session(s)", detail)
        self.assertIn("ines@example.org", detail)
        self.assertIn("my-bt admin logout", detail)


class CheckGroupMembershipTest(unittest.TestCase):
    def test_root_is_always_ok(self):
        with patch.dict("os.environ", {"SUDO_USER": "root"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": []})()
            checks = cli_checks.check_group_membership()
        self.assertEqual(checks[0][1], "ok")

    def test_member_is_ok(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": ["alice"]})()
            checks = cli_checks.check_group_membership()
        self.assertEqual(checks[0][1], "ok")

    def test_non_member_warns_with_usermod_command(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": ["someoneelse"]})()
            checks = cli_checks.check_group_membership()
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("usermod -aG my-booking alice", detail)

    def test_group_missing_entirely_warns(self):
        import grp as real_grp
        with patch("grp.getgrnam", side_effect=KeyError("no such group")):
            checks = cli_checks.check_group_membership()
        self.assertEqual(checks[0][1], "warn")


class CheckSystemdTest(unittest.TestCase):
    def _run_side_effect(self, enabled: str, active: str):
        def _run(cmd, capture_output, text):
            out = enabled if cmd[1] == "is-enabled" else active
            return type("R", (), {"stdout": out})()
        return _run

    def test_enabled_and_active_is_ok(self):
        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/systemctl"), \
             patch("app.cli_checks.subprocess.run", side_effect=self._run_side_effect("enabled", "active")):
            checks = cli_checks.check_systemd()
        self.assertTrue(all(level == "ok" for _, level, _ in checks))

    def test_not_enabled_warns(self):
        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/systemctl"), \
             patch("app.cli_checks.subprocess.run", side_effect=self._run_side_effect("disabled", "inactive")):
            checks = cli_checks.check_systemd()
        self.assertTrue(all(level == "warn" for _, level, _ in checks))

    def test_systemctl_missing_is_a_single_warning(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            checks = cli_checks.check_systemd()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")


class CheckSettingsFreshTest(unittest.TestCase):
    """settings.toml is only read once, at app/serve.py startup -- see
    check_settings_fresh()'s docstring for the real-world incident this
    caught (an edited course description not showing up because the
    service was never restarted). `_run_side_effect` distinguishes the
    three different subprocess.run() calls this exercises (`systemctl
    is-active`, `systemctl show ... --value`, `date -d ... +%s`) by cmd
    shape, the same pattern CheckSystemdTest above uses for its two."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings_path = str(Path(self._tmp.name) / "settings.toml")
        Path(self.settings_path).write_text("# test\n", encoding="utf-8")

    def _run_side_effect(self, active: str, active_enter_ts: str, epoch: str):
        def _run(cmd, capture_output, text, timeout=None, check=None):
            if cmd[:2] == ["systemctl", "is-active"]:
                out = active
            elif cmd[:2] == ["systemctl", "show"]:
                out = active_enter_ts
            elif cmd[0] == "date":
                out = epoch
            else:
                out = ""
            return type("R", (), {"stdout": out})()
        return _run

    def _which(self, name):
        return f"/usr/bin/{name}" if name in ("systemctl", "date") else None

    def test_systemctl_missing_is_silent(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            checks = cli_checks.check_settings_fresh(self.settings_path)
        self.assertEqual(checks, [])

    def test_missing_settings_file_is_silent(self):
        with patch("app.cli_checks.shutil.which", side_effect=self._which):
            checks = cli_checks.check_settings_fresh(str(Path(self._tmp.name) / "nope.toml"))
        self.assertEqual(checks, [])

    def test_service_not_active_is_silent(self):
        with patch("app.cli_checks.shutil.which", side_effect=self._which), \
             patch("app.cli_checks.subprocess.run", side_effect=self._run_side_effect("inactive", "", "")):
            checks = cli_checks.check_settings_fresh(self.settings_path)
        self.assertEqual(checks, [])

    def test_cant_determine_start_time_warns(self):
        with patch("app.cli_checks.shutil.which", side_effect=self._which), \
             patch("app.cli_checks.subprocess.run", side_effect=self._run_side_effect("active", "n/a", "")):
            checks = cli_checks.check_settings_fresh(self.settings_path)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("can't determine", checks[0][2])

    def test_edited_after_start_warns_with_restart_command(self):
        # Service "started" at epoch 1000; settings.toml's mtime is set
        # comfortably after that.
        os.utime(self.settings_path, (2000, 2000))
        with patch("app.cli_checks.shutil.which", side_effect=self._which), \
             patch("app.cli_checks.subprocess.run",
                   side_effect=self._run_side_effect("active", "Sat 2026-07-04 08:00:00 UTC", "1000")):
            checks = cli_checks.check_settings_fresh(self.settings_path)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("sudo systemctl restart my-booking.service", detail)

    def test_unchanged_since_start_is_ok(self):
        # settings.toml's mtime is set well BEFORE the service's start
        # epoch -- i.e. no edit happened after the service came up.
        os.utime(self.settings_path, (1000, 1000))
        with patch("app.cli_checks.shutil.which", side_effect=self._which), \
             patch("app.cli_checks.subprocess.run",
                   side_effect=self._run_side_effect("active", "Sat 2026-07-04 08:00:00 UTC", "2000")):
            checks = cli_checks.check_settings_fresh(self.settings_path)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")


class CheckWatchdogNginxAccessTest(unittest.TestCase):
    """`_my_booking_can_read` actually shells out to `runuser` rather than
    inspecting st_mode bits -- a real incident (2026-07-05) with the old
    stat-based version: setfacl grants access via a POSIX ACL entry, which
    never shows up in st_mode at all, so the old check kept reporting
    "can't read" even right after the exact setfacl command it
    printed was run. These tests mock `_my_booking_can_read` directly (its own
    subprocess-vs-root branching is exercised separately below) so this
    class stays focused on check_watchdog_nginx_access's own branching."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._fake_pw = type("PW", (), {"pw_uid": 999999, "pw_gid": 999999})()

    def test_not_configured_is_a_noop(self):
        self.assertEqual(cli_checks.check_watchdog_nginx_access({}), [])

    def test_my_booking_user_missing_warns(self):
        with patch("pwd.getpwnam", side_effect=KeyError("no such user")):
            checks = cli_checks.check_watchdog_nginx_access(
                {"watchdog": {"nginx_access_log": str(self.dir / "access.log")}}
            )
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("doesn't exist yet", checks[0][2])

    def test_missing_file_warns(self):
        with patch("pwd.getpwnam", return_value=self._fake_pw):
            checks = cli_checks.check_watchdog_nginx_access(
                {"watchdog": {"nginx_access_log": str(self.dir / "does-not-exist.log")}}
            )
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("doesn't exist", checks[0][2])

    def test_readable_is_ok(self):
        log = self.dir / "access.log"
        log.write_text("test\n")
        with patch("pwd.getpwnam", return_value=self._fake_pw), \
             patch("app.cli_checks._my_booking_can_read", return_value=True):
            checks = cli_checks.check_watchdog_nginx_access(
                {"watchdog": {"nginx_access_log": str(log)}}
            )
        self.assertEqual(checks[0][1], "ok")

    def test_unreadable_warns_with_setfacl_command(self):
        log = self.dir / "access.log"
        log.write_text("test\n")
        with patch("pwd.getpwnam", return_value=self._fake_pw), \
             patch("app.cli_checks._my_booking_can_read", return_value=False):
            checks = cli_checks.check_watchdog_nginx_access(
                {"watchdog": {"nginx_access_log": str(log)}}
            )
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("setfacl", detail)

    def test_cannot_verify_warns_without_claiming_unreadable(self):
        # _my_booking_can_read returns None when it can't actually check
        # (not root / runuser missing) -- must NOT be reported as the
        # same "can't read, run setfacl" warning, since that would nag
        # for a fix that might already be in place (the exact bug this
        # whole rewrite fixes).
        log = self.dir / "access.log"
        log.write_text("test\n")
        with patch("pwd.getpwnam", return_value=self._fake_pw), \
             patch("app.cli_checks._my_booking_can_read", return_value=None):
            checks = cli_checks.check_watchdog_nginx_access(
                {"watchdog": {"nginx_access_log": str(log)}}
            )
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertNotIn("setfacl", detail)
        self.assertIn("root", detail)


class MyBookingCanReadTest(unittest.TestCase):
    def test_non_root_returns_none(self):
        with patch("app.cli_checks.os.geteuid", return_value=1000), \
             patch("app.cli_checks.shutil.which", return_value="/usr/sbin/runuser"):
            self.assertIsNone(cli_checks._my_booking_can_read(Path("/some/path")))

    def test_runuser_missing_returns_none(self):
        with patch("app.cli_checks.os.geteuid", return_value=0), \
             patch("app.cli_checks.shutil.which", return_value=None):
            self.assertIsNone(cli_checks._my_booking_can_read(Path("/some/path")))

    def test_root_with_runuser_reflects_exit_code(self):
        with patch("app.cli_checks.os.geteuid", return_value=0), \
             patch("app.cli_checks.shutil.which", return_value="/usr/sbin/runuser"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0})()) as run:
            self.assertTrue(cli_checks._my_booking_can_read(Path("/some/path")))
        self.assertEqual(run.call_args[0][0][:3], ["runuser", "-u", "my-booking"])

    def test_root_with_runuser_denied(self):
        with patch("app.cli_checks.os.geteuid", return_value=0), \
             patch("app.cli_checks.shutil.which", return_value="/usr/sbin/runuser"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 1})()):
            self.assertFalse(cli_checks._my_booking_can_read(Path("/some/path")))


class CheckSelinuxTest(unittest.TestCase):
    def test_not_present_is_ok(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            checks = cli_checks.check_selinux()
        self.assertEqual(checks[0][1], "ok")

    def test_permissive_is_ok(self):
        def which(name):
            return "/usr/sbin/getenforce" if name == "getenforce" else None
        with patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", return_value=type("R", (), {"stdout": "Permissive"})()):
            checks = cli_checks.check_selinux()
        self.assertEqual(checks[0][1], "ok")

    def test_enforcing_with_boolean_off_fails(self):
        def which(name):
            return f"/usr/sbin/{name}"

        def run(cmd, capture_output, text):
            if cmd[0] == "getenforce":
                return type("R", (), {"stdout": "Enforcing"})()
            return type("R", (), {"stdout": "httpd_can_network_connect --> off"})()

        with patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_selinux()
        self.assertEqual(checks[0][1], "fail")

    def test_enforcing_with_boolean_on_is_ok(self):
        def which(name):
            return f"/usr/sbin/{name}"

        def run(cmd, capture_output, text):
            if cmd[0] == "getenforce":
                return type("R", (), {"stdout": "Enforcing"})()
            return type("R", (), {"stdout": "httpd_can_network_connect --> on"})()

        with patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_selinux()
        self.assertEqual(checks[0][1], "ok")


class CheckNginxLocationsTest(unittest.TestCase):
    """`nginx -T` dumps the fully-merged live config (every `include` --
    nginx.conf, conf.d/*, sites-enabled/*, snippets -- resolved), so this
    check can find a location block regardless of which file it actually
    lives in -- unlike grepping one guessed vhost file."""

    def test_nginx_missing_is_a_single_warning(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")

    def test_nginx_dash_t_failure_is_reported(self):
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run", return_value=type(
                 "R", (), {"returncode": 1, "stdout": "", "stderr": "nginx: [emerg] bad config\n"})()):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("bad config", checks[0][2])

    def test_all_locations_present_are_ok(self):
        merged = """
        server {
            location /courses { proxy_pass http://127.0.0.1:8811; }
            location /book/ { proxy_pass http://127.0.0.1:8811; }
            location /cancel/ { proxy_pass http://127.0.0.1:8811; }
            location /reinstate/ { proxy_pass http://127.0.0.1:8811; }
            location /host-cancel/ { proxy_pass http://127.0.0.1:8811; }
            location /host-reinstate/ { proxy_pass http://127.0.0.1:8811; }
            location /host-cancel-occurrence/ { proxy_pass http://127.0.0.1:8811; }
            location /my { proxy_pass http://127.0.0.1:8811; }
            location /admin { proxy_pass http://127.0.0.1:8811; }
            location /schedule-exceptions { proxy_pass http://127.0.0.1:8811; }
            location /csp-report { proxy_pass http://127.0.0.1:8811; }
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged, "stderr": ""})()):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(len(checks), len(cli_checks._REQUIRED_NGINX_LOCATIONS))
        self.assertTrue(all(level == "ok" for _, level, _ in checks))

    def test_one_missing_location_warns_others_stay_ok(self):
        # /courses (2026-07-06) deliberately left out here -- this locks in
        # that a brand-new required location missing from an existing,
        # not-yet-updated nginx vhost is reported on its own, without
        # affecting the other, already-present locations.
        merged = """
        location /book/ { proxy_pass http://127.0.0.1:8811; }
        location /cancel/ { proxy_pass http://127.0.0.1:8811; }
        location /reinstate/ { proxy_pass http://127.0.0.1:8811; }
        location /host-cancel/ { proxy_pass http://127.0.0.1:8811; }
        location /host-reinstate/ { proxy_pass http://127.0.0.1:8811; }
        location /admin { proxy_pass http://127.0.0.1:8811; }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged, "stderr": ""})()):
            checks = cli_checks.check_nginx_locations()
        levels = _levels(checks)
        self.assertEqual(levels["nginx location /courses"], "warn")
        self.assertEqual(levels["nginx location /my"], "warn")
        self.assertEqual(levels["nginx location /book/"], "ok")
        self.assertEqual(levels["nginx location /cancel/"], "ok")
        self.assertEqual(levels["nginx location /reinstate/"], "ok")
        self.assertEqual(levels["nginx location /host-cancel/"], "ok")
        self.assertEqual(levels["nginx location /host-reinstate/"], "ok")
        self.assertEqual(levels["nginx location /admin"], "ok")

    def test_match_modifier_is_still_detected(self):
        merged = "location = /my { proxy_pass http://127.0.0.1:8811; }\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged, "stderr": ""})()):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(_levels(checks)["nginx location /my"], "ok")

    def test_similar_but_different_path_is_not_a_false_match(self):
        # "/my-other" must not satisfy the "/my" check.
        merged = "location /my-other { proxy_pass http://127.0.0.1:8811; }\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged, "stderr": ""})()):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(_levels(checks)["nginx location /my"], "warn")


class CheckNginxConfRepoFileTest(unittest.TestCase):
    """check_nginx_conf_repo_file() looks at a real, personal nginx vhost
    conf file kept at the FIXED path site/nginx-locations.conf in this
    checkout (2026-07-10: renamed from being named after the operator's own
    domain so every real-vs-.example pair in site/ follows the same
    convention) -- a different mechanism from CheckNginxLocationsTest
    above, which only ever inspects the LIVE, already-deployed `nginx -T`
    output. This is the check meant to catch the gap BEFORE that file is
    ever deployed/reloaded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "checkout"
        (self.home / "site").mkdir(parents=True)

    def _all_locations_text(self) -> str:
        return "\n".join(
            f"location {path} {{ proxy_pass http://127.0.0.1:8811; }}"
            for path in cli_checks._REQUIRED_NGINX_LOCATIONS
        )

    def test_no_conf_file_at_all_warns(self):
        checks = cli_checks.check_nginx_conf_repo_file(str(self.home))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("no real, personal nginx vhost conf file", checks[0][2])

    def test_example_file_alone_does_not_count(self):
        (self.home / "site" / "nginx-locations.conf.example").write_text(self._all_locations_text())
        checks = cli_checks.check_nginx_conf_repo_file(str(self.home))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("no real, personal nginx vhost conf file", checks[0][2])

    def test_real_file_with_every_location_is_ok(self):
        (self.home / "site" / "nginx-locations.conf").write_text(self._all_locations_text())
        checks = cli_checks.check_nginx_conf_repo_file(str(self.home))
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")
        self.assertIn("site/nginx-locations.conf", label)

    def test_missing_location_warns_with_the_path_named(self):
        text = "\n".join(
            f"location {path} {{ proxy_pass http://127.0.0.1:8811; }}"
            for path in cli_checks._REQUIRED_NGINX_LOCATIONS if path != "/reinstate/"
        )
        (self.home / "site" / "nginx-locations.conf").write_text(text)
        checks = cli_checks.check_nginx_conf_repo_file(str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("/reinstate/", detail)

    def test_leftover_replace_me_marker_warns(self):
        text = self._all_locations_text() + "\nserver_name REPLACE-ME-YOUR-DOMAIN;\n"
        (self.home / "site" / "nginx-locations.conf").write_text(text)
        checks = cli_checks.check_nginx_conf_repo_file(str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("REPLACE-ME", detail)


class TrackedNginxExampleFileTest(unittest.TestCase):
    """Real regression, 2026-07-08: /host-cancel-occurrence/ was added to
    _REQUIRED_NGINX_LOCATIONS and to the tracked site/nginx-locations.conf
    .example, but the operator's own real, gitignored site/nginx-locations.conf
    (what the operator's OWN VPS actually installs from -- see
    scripts/build-rpm.sh/packaging/my-booking-tool.spec) never got the
    matching edit, and nothing caught it until a stale file was noticed on
    the VPS after a rebuild. check_nginx_conf_repo_file() (tested above)
    already guards the real file at `my-bt admin health` time, but that can
    only ever run somewhere the real file actually exists -- it never runs
    in this test suite/CI, since the real file is deliberately gitignored
    (see the maintainer's local notes). This test instead guards the one nginx
    reference file that IS tracked and ships to every fresh install,
    directly off disk (not a synthetic fixture like the tests above) --
    so any future new required location that's added to
    _REQUIRED_NGINX_LOCATIONS but forgotten in the .example file fails
    the suite immediately, on every commit, not just whenever someone
    happens to run `my-bt admin health` against a real deployment."""

    def test_example_file_has_every_required_location(self):
        example = Path(__file__).resolve().parent.parent / "site" / "nginx-locations.conf.example"
        text = example.read_text(encoding="utf-8")
        for path in cli_checks._REQUIRED_NGINX_LOCATIONS:
            with self.subTest(path=path):
                self.assertRegex(
                    text, rf"location\s+(?:[=~^]+\*?\s+)?{re.escape(path)}\s*\{{",
                    f"{path} missing from site/nginx-locations.conf.example",
                )

    def test_example_file_has_no_leftover_replace_me_in_the_csp_hash_count(self):
        # A cheap drift check this file's own comment block warns about:
        # the prose says how many hashes are allow-listed, and that number
        # must match how many 'sha256-...' entries actually appear in
        # script-src, or the comment itself is already lying the moment
        # it's read. 2026-07-14 (repo-review): the prose used to conflate
        # "distinct scripts" with "allow-listed hashes" (and so did this
        # test, asserting the HASH count against the "distinct ... blocks"
        # sentence) -- the comment now states both numbers separately:
        # N distinct current scripts, M hashes (superseded versions' hashes
        # kept per "ADD, never replace"). This checks the HASH claim; the
        # script count has no cheap ground truth here (it's 8 app constants
        # + the real index.html's own blocks, checked content-aware by
        # test_example_file_has_every_static_script_hash_current below).
        example = Path(__file__).resolve().parent.parent / "site" / "nginx-locations.conf.example"
        text = example.read_text(encoding="utf-8")
        hash_count = len(re.findall(r"'sha256-[A-Za-z0-9+/=]+'", text))
        digit_words = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE",
                       6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE", 10: "TEN",
                       11: "ELEVEN", 12: "TWELVE", 13: "THIRTEEN", 14: "FOURTEEN",
                       15: "FIFTEEN", 16: "SIXTEEN", 17: "SEVENTEEN", 18: "EIGHTEEN"}
        expected_word = digit_words.get(hash_count)
        self.assertIsNotNone(expected_word, f"unexpected hash count {hash_count} -- extend digit_words")
        self.assertIn(f"{expected_word} hashes", text)

    def test_example_file_has_every_static_script_hash_current(self):
        """The "rpm build" half of the CSP-hash automation the operator asked for
        (app.cli_checks.expected_csp_hashes()/check_csp_hashes_deployed()
        is the "live server" half, checked via `my-bt admin health`).
        packaging/my-booking-tool.spec's %check already runs
        `python3 -m unittest discover` on every RPM build -- so this test
        failing there means the build itself refuses to package a
        forgotten hash update, with zero new build-time scripting needed.

        Only checks the 8 static, non-interpolated Python module-constant
        script hashes here -- NOT index.html's own two scripts, which are
        legitimately deployment-specific (this repo's site/index.html.example
        is a generic placeholder, not the operator's real production content, so
        its own script hashes are expected to differ from what's allow-
        listed in this real, anonymized reference conf -- see this file's
        own top-of-file comment).

        Would have caught 2 of the 4 real incidents documented in this
        file's own CSP comment above (the booking-form MAX_GUESTS
        interpolation bug and the sortable/filterable-table script going
        stale since 2026-07-08 or earlier) the moment either shipped,
        instead of only after a live browser reported the violation."""
        example = Path(__file__).resolve().parent.parent / "site" / "nginx-locations.conf.example"
        text = example.read_text(encoding="utf-8")
        deployed_hashes = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", text))
        expected = cli_checks.expected_csp_hashes({})  # no static_site_dir -- static constants only
        missing = {label: h for label, h in expected.items() if h not in deployed_hashes}
        self.assertEqual(
            missing, {},
            f"stale/missing CSP hash(es) in site/nginx-locations.conf.example: {missing} -- "
            '"ADD, never replace" -- add the new hash(es), keep the old ones for rollback safety.',
        )

    def test_real_and_example_nginx_conf_have_the_same_csp_hash_set(self):
        """Regression test for the drift the operator hit 2026-07-13: a CSP hash
        was added to site/nginx-locations.conf.example but, at first, not
        to the real, gitignored site/nginx-locations.conf sitting right
        next to it in the same checkout (see feedback_always_update_real_
        nginx_conf memory) -- caught only because `my-bt admin setup -i`
        was re-run against the live server, not by anything in this test
        suite. The two files' own CSP script-src hash SETS should always
        be identical, even though the rest of each file legitimately
        differs (real domain values like frame-ancestors 'self'
        https://ayuryoga-trier.de vs the .example's generic placeholder;
        see TrackedNginxExampleFileTest's own docstring for why the real
        file is never otherwise touched by this suite).

        The real file won't exist at all in a fresh checkout, CI, or the
        RPM build environment (it's deliberately gitignored -- see
        the maintainer's local notes and this class's own docstring) -- this test
        no-ops (skips) rather than failing when it's absent, so it only
        ever actually runs, and only ever protects against drift, on a
        machine (the operator's own) where the real file lives alongside the
        checkout -- which is exactly where this kind of drift can happen
        unnoticed."""
        base = Path(__file__).resolve().parent.parent / "site"
        real_path = base / "nginx-locations.conf"
        if not real_path.exists():
            self.skipTest("site/nginx-locations.conf (real, gitignored) not present in this checkout")
        example_path = base / "nginx-locations.conf.example"
        real_hashes = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", real_path.read_text(encoding="utf-8")))
        example_hashes = set(
            re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", example_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            real_hashes, example_hashes,
            "site/nginx-locations.conf and .example have DIFFERENT CSP script-src hash sets -- "
            f"only in real: {real_hashes - example_hashes or 'none'}; "
            f"only in .example: {example_hashes - real_hashes or 'none'}. "
            "An edit (add a hash, keep the old ones) was made to one but not the other.",
        )

    def test_real_index_html_script_hashes_are_all_in_the_example_conf(self):
        """Closes the gap that let the 2026-07-16 schedule-exceptions
        target="_top" edit ship without its new CSP hash: the existing
        test_example_file_has_every_static_script_hash_current() only
        checks the 8 static Python module-constant scripts, deliberately
        skipping index.html's own two -- because site/index.html.example
        is a generic placeholder, not the operator's real content (see that
        test's own docstring). But site/nginx-locations.conf.example is
        NOT a generic placeholder -- its top-of-file comment says so
        explicitly: it's the real, production config, anonymized. So it's
        supposed to always carry the REAL site/index.html's actual current
        script hashes too, and nothing in the test suite checked that
        until now.

        This gap meant a real script-body edit (target="_top" added to
        the schedule-exceptions script's generated "details" link) shipped
        in the RPM and was only caught by `my-bt admin health` against the
        LIVE server, after install -- one warning line instead of a failed
        `python3 -m unittest` at build time. Same "skip if the real file
        isn't in this checkout" pattern as
        test_real_and_example_nginx_conf_have_the_same_csp_hash_set right
        above -- this only ever runs, and only ever protects against this
        exact class of drift, on a machine (the operator's own) where the real,
        gitignored site/index.html lives alongside the checkout."""
        base = Path(__file__).resolve().parent.parent / "site"
        real_index_path = base / "index.html"
        if not real_index_path.exists():
            self.skipTest("site/index.html (real, gitignored) not present in this checkout")
        example_conf_path = base / "nginx-locations.conf.example"
        conf_hashes = set(
            re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", example_conf_path.read_text(encoding="utf-8"))
        )
        index_text = real_index_path.read_text(encoding="utf-8")
        bodies = site_render.extract_script_bodies(index_text)
        missing = {}
        for i, body in enumerate(bodies, start=1):
            import base64
            import hashlib
            h = "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            if h not in conf_hashes:
                missing[f"index.html script #{i}"] = h
        self.assertEqual(
            missing, {},
            f"stale/missing CSP hash(es) in site/nginx-locations.conf.example for the real, "
            f"gitignored site/index.html's own <script> block(s): {missing} -- "
            '"ADD, never replace" -- add the new hash(es), keep the old ones for rollback safety.',
        )


class CheckNginxConfDeployedTest(unittest.TestCase):
    """check_nginx_conf_deployed() reads [site].nginx_conf_path DIRECTLY off
    disk (not `nginx -T`, not a checkout glob) -- and unlike every other
    optional settings.toml-path check, reports "fail" (not "warn") for any
    problem, since configuring this path at all is a deliberate statement
    that this exact file is real and matters (2026-07-10: it should truly
    ERROR out in case there is a problem)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf_path = Path(self._tmp.name) / "booking.example.org.conf"

    def _all_locations_text(self) -> str:
        return "\n".join(
            f"location {path} {{ proxy_pass http://127.0.0.1:8811; }}"
            for path in cli_checks._REQUIRED_NGINX_LOCATIONS
        )

    def _raw(self, path=None):
        return {"site": {"nginx_conf_path": path or str(self.conf_path)}}

    def test_not_configured_is_a_noop(self):
        self.assertEqual(cli_checks.check_nginx_conf_deployed({"site": {}}), [])

    def test_configured_but_missing_file_fails(self):
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=None):
            checks = cli_checks.check_nginx_conf_deployed(self._raw())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("not found", detail)

    def test_missing_file_but_live_under_a_different_name_says_so(self):
        old = Path(self._tmp.name) / "old-name.conf"
        old.write_text("server { server_name x; }")
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            checks = cli_checks.check_nginx_conf_deployed(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn(str(old), detail)
        self.assertIn("rename", detail)

    def test_detected_live_file_that_doesnt_actually_exist_falls_back_to_plain_message(self):
        # _live_nginx_conf_file_for_host() can only name a path parsed out of
        # nginx -T's own dump -- it never checks that path still exists on
        # disk itself, so this guards against recommending a rename from a
        # file that's already gone too.
        missing = Path(self._tmp.name) / "already-gone.conf"
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=missing):
            checks = cli_checks.check_nginx_conf_deployed(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("not found", detail)
        self.assertNotIn("rename", detail)

    def test_all_locations_present_is_ok(self):
        self.conf_path.write_text(self._all_locations_text())
        checks = cli_checks.check_nginx_conf_deployed(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")

    def test_missing_location_fails_not_warns(self):
        text = "\n".join(
            f"location {path} {{ proxy_pass http://127.0.0.1:8811; }}"
            for path in cli_checks._REQUIRED_NGINX_LOCATIONS if path != "/admin"
        )
        self.conf_path.write_text(text)
        checks = cli_checks.check_nginx_conf_deployed(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("/admin", detail)

    def test_leftover_replace_me_marker_fails(self):
        text = self._all_locations_text() + "\nserver_name REPLACE-ME-YOUR-DOMAIN;\n"
        self.conf_path.write_text(text)
        checks = cli_checks.check_nginx_conf_deployed(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("REPLACE-ME", detail)


class ResolveNginxConfCheckoutSourceTest(unittest.TestCase):
    """2026-07-10: this no longer takes a nginx_conf_path argument at all --
    the checkout side always uses the fixed site/nginx-locations.conf(.example)
    name, completely independent of whatever the live deployed file is
    called on the actual server (renamed so all content in
    site/ works the same)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()

    def test_no_match_at_all_returns_none(self):
        result = cli_checks._resolve_nginx_conf_checkout_source(str(self.home))
        self.assertIsNone(result)

    def test_falls_back_to_example(self):
        (self.home / "site" / "nginx-locations.conf.example").write_text("generic")
        result = cli_checks._resolve_nginx_conf_checkout_source(str(self.home))
        self.assertEqual(result, self.home / "site" / "nginx-locations.conf.example")

    def test_real_file_takes_precedence_over_example(self):
        (self.home / "site" / "nginx-locations.conf").write_text("real")
        (self.home / "site" / "nginx-locations.conf.example").write_text("generic")
        result = cli_checks._resolve_nginx_conf_checkout_source(str(self.home))
        self.assertEqual(result, self.home / "site" / "nginx-locations.conf")


class LiveNginxConfFileForHostTest(unittest.TestCase):
    """`_live_nginx_conf_file_for_host` parses `nginx -T`'s own
    "# configuration file <path>:" markers (one precedes every file it
    dumps) to find which actual file the matching vhost currently comes
    from -- used so `setup -i` can find/rename a vhost still deployed
    under an old filename without being told what that old name is."""

    def _raw(self, base_url="https://example.org"):
        return {"site": {"base_url": base_url}}

    def test_finds_the_file_marker_preceding_the_matching_block(self):
        merged = """# configuration file /etc/nginx/nginx.conf:
http {
    include /etc/nginx/conf.d/*.conf;
}
# configuration file /etc/nginx/conf.d/other.conf:
server {
    server_name other.org;
}
# configuration file /etc/nginx/conf.d/example.org.conf:
server {
    server_name example.org;
    listen 443 ssl;
}
"""
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            result = cli_checks._live_nginx_conf_file_for_host(self._raw())
        self.assertEqual(result, Path("/etc/nginx/conf.d/example.org.conf"))

    def test_no_matching_server_block_returns_none(self):
        merged = """# configuration file /etc/nginx/conf.d/other.conf:
server {
    server_name other.org;
}
"""
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            result = cli_checks._live_nginx_conf_file_for_host(self._raw())
        self.assertIsNone(result)

    def test_no_preceding_marker_returns_none(self):
        merged = "server {\n    server_name example.org;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            result = cli_checks._live_nginx_conf_file_for_host(self._raw())
        self.assertIsNone(result)

    def test_nginx_missing_returns_none(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            result = cli_checks._live_nginx_conf_file_for_host(self._raw())
        self.assertIsNone(result)

    def test_no_base_url_returns_none(self):
        result = cli_checks._live_nginx_conf_file_for_host({"site": {}})
        self.assertIsNone(result)


class NginxRootForHostTest(unittest.TestCase):
    """`_nginx_root_for_host` isolates one `server { ... }` block's `root`
    out of a full `nginx -T` dump via brace-depth tracking -- these tests
    exercise that parsing directly, separately from check_static_pages_reachable()."""

    def _raw(self, base_url="https://example.org"):
        return {"site": {"static_site_dir": "/var/www/x", "base_url": base_url}}

    def test_finds_root_for_matching_server_name(self):
        merged = """
        server {
            listen 80;
            server_name example.org www.example.org;
            root /var/www/example.org/public_html;
            location / { try_files $uri $uri/ =404; }
        }
        server {
            server_name other.org;
            root /var/www/other.org;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            root = cli_checks._nginx_root_for_host(self._raw())
        self.assertEqual(root, "/var/www/example.org/public_html")

    def test_no_matching_server_name_returns_none(self):
        merged = "server {\n  server_name other.org;\n  root /var/www/other.org;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            root = cli_checks._nginx_root_for_host(self._raw())
        self.assertIsNone(root)

    def test_nginx_missing_returns_none(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            root = cli_checks._nginx_root_for_host(self._raw())
        self.assertIsNone(root)

    def test_no_base_url_returns_none(self):
        root = cli_checks._nginx_root_for_host({"site": {}})
        self.assertIsNone(root)


class NginxAccessLogForHostTest(unittest.TestCase):
    def _raw(self, base_url="https://example.org"):
        return {"site": {"base_url": base_url}}

    def test_finds_access_log_in_matching_server_block(self):
        merged = """
        server {
            server_name example.org;
            access_log /var/log/nginx/example.access.log main;
        }
        server {
            server_name other.org;
            access_log /var/log/nginx/other.access.log;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            log = cli_checks._nginx_access_log_for_host(self._raw())
        self.assertEqual(log, "/var/log/nginx/example.access.log")

    def test_no_log_format_name_does_not_swallow_the_semicolon(self):
        """Real production bug, 2026-07-10: with no log-format name between
        the path and the `;` (the operator's real nginx-locations.conf --
        `access_log /var/log/nginx/booking.example.org.access.log;`), the old regex's
        greedy `\\S+` path capture swallowed the `;` itself, then matched on
        into the NEXT line's `error_log ...;` to find a semicolon to close
        the pattern with -- so the "detected" path came out as
        '.../access.log;' (semicolon included) instead of backtracking.
        Silent for years until #78 (this same day) added the first code
        path that actually WRITES this detected value into settings.toml on
        accept, which is what turned it into a real corrupted
        nginx_access_log setting in production."""
        merged = """
        server {
            server_name booking.example.org;
            access_log /var/log/nginx/booking.example.org.access.log;
            error_log  /var/log/nginx/booking.example.org.error.log;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            log = cli_checks._nginx_access_log_for_host(
                {"site": {"base_url": "https://booking.example.org"}})
        self.assertEqual(log, "/var/log/nginx/booking.example.org.access.log")

    def test_falls_back_to_http_level_directive(self):
        merged = """
        access_log /var/log/nginx/access.log combined;
        server {
            server_name example.org;
            root /var/www/example.org;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            log = cli_checks._nginx_access_log_for_host(self._raw())
        self.assertEqual(log, "/var/log/nginx/access.log")

    def test_access_log_off_is_ignored(self):
        merged = """
        server {
            server_name example.org;
            access_log off;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            log = cli_checks._nginx_access_log_for_host(self._raw())
        self.assertIsNone(log)

    def test_syslog_target_is_ignored(self):
        merged = """
        server {
            server_name example.org;
            access_log syslog:server=unix:/dev/log combined;
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            log = cli_checks._nginx_access_log_for_host(self._raw())
        self.assertIsNone(log)

    def test_nginx_missing_returns_none(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            log = cli_checks._nginx_access_log_for_host(self._raw())
        self.assertIsNone(log)


class CheckWatchdogNginxAccessLogConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _raw(self, nginx_access_log=None):
        raw = {"site": {"base_url": "https://example.org"}}
        if nginx_access_log is not None:
            raw["watchdog"] = {"nginx_access_log": nginx_access_log}
        return raw

    def test_nothing_detectable_is_a_noop(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            self.assertEqual(cli_checks.check_watchdog_nginx_access_log_config(self._raw()), [])

    def test_not_configured_but_detected_suggests_enabling(self):
        merged = "server {\n  server_name example.org;\n  access_log /var/log/nginx/access.log;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            checks = cli_checks.check_watchdog_nginx_access_log_config(self._raw())
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("not enabled yet", detail)
        self.assertIn("/var/log/nginx/access.log", detail)

    def test_configured_and_matches_detected_is_ok(self):
        log = self.dir / "access.log"
        log.write_text('1.2.3.4 - - [05/Jul/2026:14:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "-"\n')
        merged = f"server {{\n  server_name example.org;\n  access_log {log};\n}}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            checks = cli_checks.check_watchdog_nginx_access_log_config(self._raw(str(log)))
        self.assertEqual(checks[0][1], "ok")

    def test_configured_but_differs_from_detected_warns(self):
        merged = "server {\n  server_name example.org;\n  access_log /var/log/nginx/access.log;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            checks = cli_checks.check_watchdog_nginx_access_log_config(self._raw("/some/stale/path.log"))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("stale", detail)
        self.assertIn("nginx's live config", detail)


class CheckStaticPagesReachableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.static_dir = Path(self._tmp.name) / "static"
        self.nginx_root = Path(self._tmp.name) / "public_html"
        self.static_dir.mkdir()
        self.nginx_root.mkdir()

    def _raw(self):
        return {"site": {"static_site_dir": str(self.static_dir), "base_url": "https://example.org"}}

    def test_no_static_site_dir_is_noop(self):
        self.assertEqual(cli_checks.check_static_pages_reachable({"site": {}}), [])

    def test_no_nginx_root_found_is_noop(self):
        with patch("app.cli_checks._nginx_root_for_host", return_value=None):
            self.assertEqual(cli_checks.check_static_pages_reachable(self._raw()), [])

    def test_same_directory_as_nginx_root_is_noop(self):
        # static_site_dir IS nginx's root -- every file is trivially
        # reachable, nothing worth reporting.
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(self.static_dir)):
            self.assertEqual(cli_checks.check_static_pages_reachable(self._raw()), [])

    def test_page_not_deployed_at_all_is_skipped(self):
        # Nothing in static_site_dir yet -- check_static_pages_deployed()
        # already covers "not deployed", this check has nothing to add.
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(self.nginx_root)):
            self.assertEqual(cli_checks.check_static_pages_reachable(self._raw()), [])

    def test_page_reachable_via_symlink_is_ok(self):
        (self.static_dir / "privacy.html").write_text("hi")
        (self.nginx_root / "privacy.html").symlink_to(self.static_dir / "privacy.html")
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(self.nginx_root)):
            checks = cli_checks.check_static_pages_reachable(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["nginx-reachable: privacy.html"], "ok")

    def test_page_deployed_but_not_symlinked_warns(self):
        (self.static_dir / "privacy.html").write_text("hi")
        # nginx_root has nothing pointing at it.
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(self.nginx_root)):
            checks = cli_checks.check_static_pages_reachable(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["nginx-reachable: privacy.html"], "warn")
        detail = {label: detail for label, _, detail in checks}["nginx-reachable: privacy.html"]
        self.assertIn("ln -s", detail)


class CheckStaticPagesDeployedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "checkout"
        self.static_dir = Path(self._tmp.name) / "static"
        (self.home / "site").mkdir(parents=True)
        self.static_dir.mkdir()
        # Point the RPM-install fallback (normally the hardcoded
        # /usr/share/doc/my-booking-tool/site) at a guaranteed-empty tmp
        # dir, so these tests are deterministic regardless of whether
        # my-booking-tool actually happens to be installed on the machine
        # running the suite.
        self.doc_site_dir = Path(self._tmp.name) / "doc-site"
        self.doc_site_dir.mkdir()
        patcher = patch("app.cli_checks._DOC_SITE_DIR", self.doc_site_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _raw(self):
        return {"site": {"static_site_dir": str(self.static_dir)}}

    def test_no_static_site_dir_is_noop(self):
        self.assertEqual(cli_checks.check_static_pages_deployed({"site": {}}, str(self.home)), [])

    def test_no_checkout_source_is_skipped(self):
        # Neither a real site/index.html, an .example placeholder, nor a
        # %doc reference copy exists -- nothing to compare against, so no
        # entry for it at all.
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        self.assertEqual(checks, [])

    def test_falls_back_to_doc_dir_on_an_installed_system(self):
        # Regression coverage for 2026-07-05: HOME (/opt/my-booking) never
        # carries index.html/impressum.html/terms.html at all -- only the
        # %doc copy under _DOC_SITE_DIR does (see packaging/*.spec). Before
        # this fallback existed, check_static_pages_deployed() silently
        # found nothing on a real installed server, even right after a
        # rebuild with genuinely new content.
        (self.doc_site_dir / "index.html").write_text("from the doc-dir copy")
        (self.static_dir / "index.html").write_text("stale deployed copy")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("differs", detail)

    def test_home_site_takes_precedence_over_doc_dir(self):
        # Running straight from a git checkout (dev/test) -- home/site/
        # wins over the installed-system fallback.
        (self.home / "site" / "index.html").write_text("checkout copy")
        (self.doc_site_dir / "index.html").write_text("doc-dir copy")
        (self.static_dir / "index.html").write_text("checkout copy")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")

    def test_matches_checkout_is_ok(self):
        (self.home / "site" / "index.html").write_text("hello world")
        (self.static_dir / "index.html").write_text("hello world")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        levels = _levels(checks)
        self.assertEqual(levels["static site content (" + str(self.static_dir / "index.html") + ")"], "ok")

    def test_differs_from_checkout_warns(self):
        (self.home / "site" / "index.html").write_text("new content")
        (self.static_dir / "index.html").write_text("old content")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("differs", detail)

    def test_not_deployed_yet_warns(self):
        (self.home / "site" / "terms.html").write_text("terms")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("not deployed yet", detail)

    def test_falls_back_to_example_when_no_real_file(self):
        (self.home / "site" / "impressum.html.example").write_text("generic placeholder")
        (self.static_dir / "impressum.html").write_text("generic placeholder")
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")

    def test_maintenance_banner_alone_does_not_count_as_a_difference(self):
        # 2026-07-10: a vimdiff offered by setup -i where
        # the ONLY difference was the maintenance banner led to `my-bt
        # setup -i` learning about maintenance mode and ignoring any
        # change linked to it, so it no longer proposes a vimdiff when
        # this is the only difference -- `my-bt admin site-maintenance on`
        # inserts the
        # banner directly into the LIVE deployed index.html (by design, so
        # it shows up immediately), so the deployed copy legitimately
        # differs from the checkout for as long as maintenance stays on.
        content = "<html><body>hello world</body></html>"
        (self.home / "site" / "index.html").write_text(content)
        banner = maintenance.banner_html("admin@example.org", "back soon")
        (self.static_dir / "index.html").write_text(maintenance.insert_banner(content, banner))
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        levels = _levels(checks)
        self.assertEqual(levels["static site content (" + str(self.static_dir / "index.html") + ")"], "ok")

    def test_a_real_difference_alongside_the_banner_still_warns(self):
        # The banner-stripping normalization must not mask a GENUINE
        # content difference just because maintenance mode also happens to
        # be on.
        (self.home / "site" / "index.html").write_text("<html><body>new content</body></html>")
        banner = maintenance.banner_html("admin@example.org")
        deployed = maintenance.insert_banner("<html><body>old content</body></html>", banner)
        (self.static_dir / "index.html").write_text(deployed)
        checks = cli_checks.check_static_pages_deployed(self._raw(), str(self.home))
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("differs", detail)


class CheckRpmVerifyTest(unittest.TestCase):
    def test_rpm_not_present_is_ok(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(checks[0][1], "ok")
        self.assertIn("not present", checks[0][2])

    def test_not_installed_via_rpm_is_ok(self):
        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", return_value=type("R", (), {"returncode": 1, "stdout": ""})()):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(checks[0][1], "ok")

    def test_clean_verify_is_ok(self):
        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": ""})()  # rpm -V: no output = clean

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(checks[0][1], "ok")

    def test_non_config_file_modification_warns(self):
        verify_output = "S.5....T.  /usr/lib/systemd/system/my-booking.service\n"

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("my-booking.service", label)

    def test_config_file_modification_is_excluded(self):
        # The "c" marker means settings.toml -- already tracked separately
        # via check_rpmnew(), so check_rpm_verify() shouldn't also flag it.
        verify_output = "S.5....T.  c /etc/my-booking/settings.toml\n"

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")

    def test_ownership_only_change_is_not_flagged(self):
        # e.g. settings.toml.example after %post's `chown -R
        # my-booking:my-booking /etc/my-booking` (packaging/my-booking-tool.spec)
        # -- U/G differ from what the RPM recorded at build time, but
        # that's the package's OWN intended behavior, not tampering (hit
        # in practice 2026-07-05 -- see the maintainer's local notes).
        verify_output = ".....UG..  /etc/my-booking/settings.toml.example\n"

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")

    def test_content_change_still_flagged_alongside_ownership_only_noise(self):
        # Ownership-only noise on one file must not hide a real content
        # change (S/5) on another.
        verify_output = (
            ".....UG..  /etc/my-booking/settings.toml.example\n"
            "S.5....T.  /opt/my-booking/app/webapp.py\n"
        )

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("webapp.py", checks[0][0])

    def test_missing_file_is_reported(self):
        verify_output = "missing     /opt/my-booking/app/webapp.py\n"

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("webapp.py", checks[0][0])


class CheckStaticSiteDriftTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.tmpl_path = self.dir / "privacy.html.tmpl"
        self.tmpl_path.write_text("kept for ${retention_months} months", encoding="utf-8")
        self.static_dir = self.dir / "live"
        self.static_dir.mkdir()

    def _raw(self, static_site_dir=None, retention_months=24, canceled_retention_months=6) -> dict:
        raw = {"privacy": {"retention_months": retention_months,
                            "canceled_retention_months": canceled_retention_months}}
        if static_site_dir:
            raw["site"] = {"static_site_dir": static_site_dir}
        return raw

    def test_not_configured_is_a_noop(self):
        checks = cli_checks.check_static_site_drift(self._raw(), self.tmpl_path)
        self.assertEqual(checks, [])

    def test_not_deployed_yet_warns(self):
        checks = cli_checks.check_static_site_drift(self._raw(str(self.static_dir)), self.tmpl_path)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("not deployed yet", checks[0][2])

    def test_matching_deployed_page_is_ok(self):
        site_render.write_privacy_html(self.tmpl_path, 24, 6, self.static_dir / "privacy.html")
        checks = cli_checks.check_static_site_drift(self._raw(str(self.static_dir)), self.tmpl_path)
        self.assertEqual(checks[0][1], "ok")

    def test_stale_deployed_page_warns(self):
        # Deployed with the OLD retention value; settings.toml has since changed.
        site_render.write_privacy_html(self.tmpl_path, 24, 6, self.static_dir / "privacy.html")
        checks = cli_checks.check_static_site_drift(
            self._raw(str(self.static_dir), retention_months=36), self.tmpl_path
        )
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("doesn't match", checks[0][2])


_SAMPLE_INDEX_HTML = """<html><body>
<div class="top-bar" id="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
<div id="schedule-exceptions"></div>
<ul><li><a href="/book/sat-trier">Book your place here.</a></li></ul>
<script>(function () { fetch('/my/session', { credentials: 'same-origin' }); })();</script>
<script>(function () { fetch('/schedule-exceptions', { credentials: 'same-origin' }); })();</script>
</body></html>
"""


class CheckIndexEmbeddedDriftTest(unittest.TestCase):
    """check_index_embedded_drift (reworked 2026-07-13): no more separate
    .tmpl file -- DERIVES what index_embedded.html should look like
    straight from the LIVE deployed index.html + current settings.toml,
    gated entirely on [site].index_embedded_enabled (default off)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.static_dir = self.dir / "live"
        self.static_dir.mkdir()

    def _raw(self, static_site_dir=None, enabled=True, timezone="UTC", course=None) -> dict:
        site = {"timezone": timezone, "index_embedded_enabled": enabled}
        if static_site_dir:
            site["static_site_dir"] = static_site_dir
        raw = {"site": site}
        if course:
            raw["course"] = [course]
        return raw

    def test_disabled_is_a_silent_noop_not_a_warning(self):
        # Off by default -- most deployments don't embed their site via
        # <iframe> elsewhere, so being off is never itself a problem.
        (self.static_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir), enabled=False))
        self.assertEqual(checks, [])

    def test_static_site_dir_not_configured_is_a_noop(self):
        checks = cli_checks.check_index_embedded_drift(self._raw())
        self.assertEqual(checks, [])

    def test_index_html_not_deployed_warns(self):
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir)))
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("isn't deployed yet", checks[0][2])

    def test_not_deployed_yet_warns(self):
        (self.static_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir)))
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("not deployed yet", checks[0][2])

    def test_matching_deployed_page_is_ok(self):
        (self.static_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        derived = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        (self.static_dir / "index_embedded.html").write_text(derived, encoding="utf-8")
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir)))
        self.assertEqual(checks[0][1], "ok")

    def test_new_course_date_override_makes_deployed_page_stale(self):
        (self.static_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        # Deployed with NO overrides; settings.toml has since gained one --
        # far-future date so this is "upcoming" regardless of real today().
        derived = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        (self.static_dir / "index_embedded.html").write_text(derived, encoding="utf-8")
        course = {"shortname": "sat-trier", "title": "Yoga", "location": "Trier", "weekday": "sat",
                  "start_time": "10:45", "duration_minutes": 120, "capacity": 10,
                  "date_override": [{"date": "2099-01-01", "start_time": "09:45"}]}
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir), course=course))
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("doesn't match", checks[0][2])

    def test_active_maintenance_banner_is_not_reported_as_drift(self):
        (self.static_dir / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        derived = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        out_path = self.static_dir / "index_embedded.html"
        out_path.write_text(derived, encoding="utf-8")
        maintenance.apply_banner_to_file(out_path, True, "admin@example.org", "back Monday")
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir)))
        self.assertEqual(checks[0][1], "ok")

    def test_derivation_error_on_live_index_html_is_reported_as_fail(self):
        broken_html = _SAMPLE_INDEX_HTML.replace('href="/my"', 'href="/my-account"')
        (self.static_dir / "index.html").write_text(broken_html, encoding="utf-8")
        checks = cli_checks.check_index_embedded_drift(self._raw(str(self.static_dir)))
        self.assertEqual(checks[0][1], "fail")


class CheckStaticSiteComplianceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.static_dir = Path(self._tmp.name) / "live"
        self.static_dir.mkdir()

    def _raw(self, static_site_dir=None) -> dict:
        return {"site": {"static_site_dir": static_site_dir}} if static_site_dir else {}

    def test_not_configured_is_a_noop(self):
        self.assertEqual(cli_checks.check_static_site_compliance(self._raw()), [])

    def test_missing_pages_are_skipped_not_flagged(self):
        # No pages copied to static_site_dir at all yet -- nothing to warn
        # about here (check_static_site_drift already covers "not deployed
        # yet" for privacy.html specifically).
        checks = cli_checks.check_static_site_compliance(self._raw(str(self.static_dir)))
        self.assertEqual(checks, [])

    def test_leftover_replace_me_marker_warns(self):
        (self.static_dir / "impressum.html").write_text(
            "<p>REPLACE-ME-YOUR-NAME-OR-ORGANIZATION</p>", encoding="utf-8"
        )
        checks = cli_checks.check_static_site_compliance(self._raw(str(self.static_dir)))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("REPLACE-ME", checks[0][2])

    def test_leftover_unsubstituted_template_placeholder_warns(self):
        (self.static_dir / "privacy.html").write_text(
            "kept for ${retention_months} months", encoding="utf-8"
        )
        checks = cli_checks.check_static_site_compliance(self._raw(str(self.static_dir)))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "warn")
        self.assertIn("unsubstituted", checks[0][2])

    def test_customized_page_is_ok(self):
        (self.static_dir / "terms.html").write_text(
            "<p>Participation is voluntary and at your own risk.</p>", encoding="utf-8"
        )
        checks = cli_checks.check_static_site_compliance(self._raw(str(self.static_dir)))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")

    def test_checks_all_four_pages_independently(self):
        (self.static_dir / "index.html").write_text("fine", encoding="utf-8")
        (self.static_dir / "impressum.html").write_text("REPLACE-ME", encoding="utf-8")
        checks = cli_checks.check_static_site_compliance(self._raw(str(self.static_dir)))
        self.assertEqual(len(checks), 2)
        levels = {label: level for label, level, _ in checks}
        self.assertTrue(any(level == "ok" for level in levels.values()))
        self.assertTrue(any(level == "warn" for level in levels.values()))


class CheckDataDirGitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_no_git_dir_warns(self):
        checks = cli_checks.check_data_dir_git(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("setup -i", detail)

    def test_git_dir_present_is_ok(self):
        (self.data_dir / ".git").mkdir()
        checks = cli_checks.check_data_dir_git(self.data_dir)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")


class CheckDirectoryFsyncSupportTest(unittest.TestCase):
    """2026-07-15: fsync_dir()'s best-effort/never-raises
    design is worth a one-time capability probe, rather than relying
    on someone noticing a warning line in a log nobody tails. This is
    the re-checkable-any-time half of that (see app.serve's startup
    check for the other half) -- surfaced through `my-bt admin setup`/
    `admin health` so a stale/unsupported mount keeps showing up, with
    the standing "any warning -> exit 1" policy applying to it
    same as every other check here."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_missing_data_dir_is_a_warn_not_a_crash(self):
        missing = self.data_dir / "not-created-yet"
        checks = cli_checks.check_directory_fsync_support(missing)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("doesn't exist yet", detail)

    def test_supported_is_ok(self):
        checks = cli_checks.check_directory_fsync_support(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")
        self.assertIn("fsync works", detail)

    def test_unsupported_is_a_warn(self):
        with patch("app.atomic_io.probe_dir_fsync_support", return_value=False):
            checks = cli_checks.check_directory_fsync_support(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("NOT supported", detail)


class CheckDataDirOwnershipTest(unittest.TestCase):
    """2026-07-08 incident: scripts/migrate-simplymeet-history.py --commit
    run from a root shell left users.csv/registrations.csv root-owned +
    mode 0600, unreadable by the my-booking service -> live 500. This
    check exists to catch that as a `fail` before it becomes a live
    incident again -- see cli_checks.check_data_dir_ownership's own
    docstring for the full story."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_no_csvs_yet_is_empty(self):
        # Nothing written yet -- nothing to own, not a failure.
        checks = cli_checks.check_data_dir_ownership(self.data_dir)
        self.assertEqual(checks, [])

    def test_my_booking_user_missing_warns(self):
        (self.data_dir / "users.csv").write_text("id,name\n")
        with patch("pwd.getpwnam", side_effect=KeyError("no such user")):
            checks = cli_checks.check_data_dir_ownership(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("doesn't exist yet", detail)

    def test_correct_owner_is_ok(self):
        p = self.data_dir / "users.csv"
        p.write_text("id,name\n")
        my_uid = os.stat(p).st_uid  # the test process's own uid, as a stand-in
        with patch("pwd.getpwnam", return_value=type("P", (), {"pw_uid": my_uid})()):
            checks = cli_checks.check_data_dir_ownership(self.data_dir)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")

    def test_wrong_owner_fails_with_chown_command(self):
        p = self.data_dir / "users.csv"
        p.write_text("id,name\n")
        real_uid = os.stat(p).st_uid
        wrong_uid = real_uid + 1
        with patch("pwd.getpwnam", return_value=type("P", (), {"pw_uid": wrong_uid})()), \
             patch("pwd.getpwuid", return_value=type("P", (), {"pw_name": "root"})()):
            checks = cli_checks.check_data_dir_ownership(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("owned by root", detail)
        self.assertIn(f"sudo chown my-booking:my-booking {p}", detail)

    def test_multiple_mismatched_files_all_named_in_fix_command(self):
        p1 = self.data_dir / "users.csv"
        p2 = self.data_dir / "registrations.csv"
        p1.write_text("id,name\n")
        p2.write_text("id,course\n")
        real_uid = os.stat(p1).st_uid
        wrong_uid = real_uid + 1
        with patch("pwd.getpwnam", return_value=type("P", (), {"pw_uid": wrong_uid})()), \
             patch("pwd.getpwuid", return_value=type("P", (), {"pw_name": "root"})()):
            checks = cli_checks.check_data_dir_ownership(self.data_dir)
        self.assertEqual(len(checks), 1)
        _, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn(str(p1), detail)
        self.assertIn(str(p2), detail)

    def test_owner_uid_with_no_pwd_entry_shown_as_uid(self):
        p = self.data_dir / "users.csv"
        p.write_text("id,name\n")
        real_uid = os.stat(p).st_uid
        wrong_uid = real_uid + 1
        with patch("pwd.getpwnam", return_value=type("P", (), {"pw_uid": wrong_uid})()), \
             patch("pwd.getpwuid", side_effect=KeyError("no such uid")):
            checks = cli_checks.check_data_dir_ownership(self.data_dir)
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn(f"owned by uid {real_uid}", detail)


class CheckPathGroupAndSelinuxTest(unittest.TestCase):
    """2026-07-16: group+permissions+SELinux are audited for ALL
    data paths, INCLUDING any user-configurable ones (e.g. an
    email-templates directory) -- the ONE shared function every data
    path (data_dir itself, [logging].log_file, [site].static_site_dir,
    and any future configurable directory) goes through instead of
    growing its own bespoke ownership check the way check_data_dir_
    ownership above (uid-only, *.csv-only) and scripts/my-bt's old
    os.access()-based data_dir/log_file checks did."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "somedir"
        self.path.mkdir()

    def _no_selinux(self):
        return patch("app.cli_checks.shutil.which", return_value=None)

    def test_missing_path_warns_and_skips_everything_else(self):
        checks = cli_checks.check_path_group_and_selinux("data dir", self.path / "nope")
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("does not exist yet", detail)

    def test_expected_group_missing_warns(self):
        with patch("grp.getgrnam", side_effect=KeyError("no such group")):
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "data dir group")
        self.assertEqual(level, "warn")
        self.assertIn("doesn't exist yet", detail)

    def test_matching_group_is_ok(self):
        my_gid = os.stat(self.path).st_gid  # test process's own gid as a stand-in
        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), self._no_selinux():
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        group_check = _levels(checks)["data dir group"]
        self.assertEqual(group_check, "ok")

    def test_mismatched_group_warns_with_chgrp_command(self):
        real_gid = os.stat(self.path).st_gid
        wrong_gid = real_gid + 1
        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": wrong_gid})()), \
             patch("grp.getgrgid", return_value=type("G", (), {"gr_name": "wheel"})()), self._no_selinux():
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        label, level, detail = _both(checks)["data dir group"]
        self.assertEqual(level, "warn")
        self.assertIn("group 'wheel'", detail)
        self.assertIn(f"sudo chgrp -R my-booking {self.path}", detail)

    def test_mismatched_group_with_no_grp_entry_shown_as_gid(self):
        # The path's own ACTUAL gid (real_gid) is what has no /etc/group
        # entry here -- the mismatch is against `expected_group`
        # ("my-booking", mocked to a different gid), not the other way
        # around, so the rendered "gid N" must be the real, current gid.
        real_gid = os.stat(self.path).st_gid
        wrong_gid = real_gid + 1
        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": wrong_gid})()), \
             patch("grp.getgrgid", side_effect=KeyError("no such gid")), self._no_selinux():
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        _, level, detail = _both(checks)["data dir group"]
        self.assertEqual(level, "warn")
        self.assertIn(f"gid {real_gid}", detail)

    def test_selinux_not_present_skips_selinux_check_entirely(self):
        my_gid = os.stat(self.path).st_gid
        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), self._no_selinux():
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        self.assertNotIn("data dir SELinux context", _levels(checks))

    def test_selinux_permissive_skips_selinux_check(self):
        my_gid = os.stat(self.path).st_gid

        def which(name):
            return "/usr/sbin/getenforce" if name == "getenforce" else None

        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), \
             patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", return_value=type("R", (), {"stdout": "Permissive"})()):
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        self.assertNotIn("data dir SELinux context", _levels(checks))

    def test_selinux_enforcing_matchpathcon_missing_warns(self):
        my_gid = os.stat(self.path).st_gid

        def which(name):
            return "/usr/sbin/getenforce" if name == "getenforce" else None

        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), \
             patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", return_value=type("R", (), {"stdout": "Enforcing"})()):
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        label, level, detail = _both(checks)["data dir SELinux context"]
        self.assertEqual(level, "warn")
        self.assertIn("matchpathcon isn't available", detail)

    def test_selinux_enforcing_matching_context_is_ok(self):
        my_gid = os.stat(self.path).st_gid

        def which(name):
            return f"/usr/sbin/{name}"

        def run(cmd, capture_output, text):
            if cmd[0] == "getenforce":
                return type("R", (), {"stdout": "Enforcing"})()
            if cmd[0] == "matchpathcon":
                return type("R", (), {"stdout": "system_u:object_r:var_lib_t:s0"})()
            return type("R", (), {"stdout": "system_u:object_r:var_lib_t:s0"})()  # stat -c %C

        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), \
             patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        label, level, detail = _both(checks)["data dir SELinux context"]
        self.assertEqual(level, "ok")

    def test_selinux_enforcing_mismatched_context_warns_with_restorecon_command(self):
        my_gid = os.stat(self.path).st_gid

        def which(name):
            return f"/usr/sbin/{name}"

        def run(cmd, capture_output, text):
            if cmd[0] == "getenforce":
                return type("R", (), {"stdout": "Enforcing"})()
            if cmd[0] == "matchpathcon":
                return type("R", (), {"stdout": "system_u:object_r:var_lib_t:s0"})()
            return type("R", (), {"stdout": "unconfined_u:object_r:user_home_t:s0"})()  # stat -c %C

        with patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": my_gid})()), \
             patch("app.cli_checks.shutil.which", side_effect=which), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_path_group_and_selinux("data dir", self.path)
        label, level, detail = _both(checks)["data dir SELinux context"]
        self.assertEqual(level, "warn")
        self.assertIn("policy expects 'system_u:object_r:var_lib_t:s0'", detail)
        self.assertIn(f"sudo restorecon -Rv {self.path}", detail)


class CheckMaintenanceModeTest(unittest.TestCase):
    """Reported as "warn" (not silently "ok"/nothing) whenever maintenance
    mode is ON -- deliberate, not a misconfiguration, but still something
    that should be visible in `status`/`setup` so it can't stay on by
    accident, unnoticed, after a real maintenance window ends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_off_is_ok(self):
        checks = cli_checks.check_maintenance_mode(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")

    def test_on_warns_with_the_message_included(self):
        maintenance.enable(self.data_dir, message="back Monday")
        checks = cli_checks.check_maintenance_mode(self.data_dir)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("back Monday", detail)
        self.assertIn("maintenance off", detail)

    def test_on_without_a_message_still_warns(self):
        maintenance.enable(self.data_dir)
        checks = cli_checks.check_maintenance_mode(self.data_dir)
        self.assertEqual(checks[0][1], "warn")


class CheckCaldavCalendarsTest(unittest.TestCase):
    """CalDAVClient itself is exercised in tests/test_caldav.py -- these
    tests are about check_caldav_calendars()'s own logic over the
    2026-07-18 [booking_calendar] + [[conflict_calendar]] shape (config
    gating, error handling, structural blocks-coverage warn), so
    CalDAVClient is mocked wholesale rather than driven through a fake
    transport. ICS conflict sources are fetch-level and covered in
    tests/test_conflict.py; here only the reachability check's shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.password_file = Path(self._tmp.name) / "caldav_password"
        self.password_file.write_text("hunter2", encoding="utf-8")

    def _raw(self, entries=None, **booking_overrides) -> dict:
        booking = {
            "caldav_url": "https://dav.mailbox.org/caldav/",
            "username": "calendar@example.org",
            "password_file": str(self.password_file),
            "calendar": "Yoga-Bookings",
        }
        booking.update(booking_overrides)
        if entries is None:
            entries = [{"name": "own-calendar", "source": "booking_calendar", "mode": "blocks"}]
        return {"booking_calendar": booking, "conflict_calendar": entries}

    def test_not_configured_is_a_noop_except_coverage_warn(self):
        checks = cli_checks.check_caldav_calendars({"booking_calendar": {}, "conflict_calendar": []})
        # Nothing reachable to check, but the structural blocks-coverage
        # warn still applies (no entry covers the booking calendar).
        self.assertEqual([c[1] for c in checks], ["warn"])
        self.assertIn("blocks-mode", checks[0][2])

    def test_no_blocks_entry_covering_booking_calendar_warns(self):
        # 2026-07-14, kept through the 2026-07-18 redesign: the
        # cancel-entire-session blocker lands on the booking calendar,
        # but only [[conflict_calendar]] entries are conflict-checked --
        # without a blocks-mode entry covering it, a canceled session's
        # date would silently stay bookable.
        raw = self._raw(entries=[{"name": "other", "ics_url": "https://x/feed.ics"}])
        with patch.object(cli_checks, "_check_ics_conflict_source",
                          return_value=("conflict calendar 'other'", "ok", "fetched")):
            with patch.object(cli_checks, "CalDAVClient") as klass:
                klass.return_value.list_calendars.return_value = {"Yoga-Bookings": "/y/"}
                checks = cli_checks.check_caldav_calendars(raw)
        warns = [c for c in checks if c[1] == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn("blocker", warns[0][2])

    def test_blocks_coverage_via_source_booking_calendar_does_not_warn(self):
        with patch.object(cli_checks, "CalDAVClient") as klass:
            klass.return_value.list_calendars.return_value = {"Yoga-Bookings": "/y/"}
            checks = cli_checks.check_caldav_calendars(self._raw())
        self.assertEqual([c for c in checks if c[1] != "ok"], [])

    def test_partially_configured_booking_is_quiet_besides_coverage(self):
        raw = self._raw(entries=[], username=None)
        self.assertEqual([c[1] for c in cli_checks.check_caldav_calendars(raw)], ["warn"])

    def test_missing_password_file_skips_the_live_check(self):
        # check_secrets() already reports a missing secret file -- this
        # check shouldn't also complain about it a second way.
        raw = self._raw(password_file=str(self.password_file) + ".missing")
        self.assertEqual([c for c in cli_checks.check_caldav_calendars(raw) if c[1] == "fail"], [])

    @patch("app.cli_checks.CalDAVClient")
    def test_connection_or_auth_failure_is_a_warn(self, mock_cls):
        mock_cls.return_value.list_calendars.side_effect = RuntimeError(
            "PROPFIND https://dav.mailbox.org/caldav/ -> HTTP 401"
        )
        checks = cli_checks.check_caldav_calendars(self._raw())
        warns = [c for c in checks if c[1] == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn("401", warns[0][2])

    @patch("app.cli_checks.CalDAVClient")
    def test_booking_calendar_found_is_ok(self, mock_cls):
        mock_cls.return_value.list_calendars.return_value = {"Yoga-Bookings": "/y/"}
        checks = cli_checks.check_caldav_calendars(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["booking calendar 'Yoga-Bookings'"], "ok")

    @patch("app.cli_checks.CalDAVClient")
    def test_booking_calendar_not_found_fails_with_the_real_list(self, mock_cls):
        # The exact failure mode hit in production 2026-07-05: the
        # configured base URL only ever resolved "WebDAV Root" -- the
        # detail message should surface what was actually found so this
        # is diagnosable without a separate curl/journalctl trip.
        mock_cls.return_value.list_calendars.return_value = {"WebDAV Root": "/"}
        checks = cli_checks.check_caldav_calendars(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["booking calendar 'Yoga-Bookings'"], "fail")
        details = {label: detail for label, _, detail in checks}
        self.assertIn("WebDAV Root", details["booking calendar 'Yoga-Bookings'"])

    @patch("app.cli_checks.CalDAVClient")
    def test_caldav_conflict_entry_checked_with_its_own_calendar_name(self, mock_cls):
        mock_cls.return_value.list_calendars.return_value = {"Yoga-Bookings": "/y/", "Work": "/w/"}
        raw = self._raw(entries=[
            {"name": "own-calendar", "source": "booking_calendar", "mode": "blocks"},
            {"name": "work", "caldav_url": "https://dav.other/", "username": "u",
             "password_file": str(self.password_file), "calendar": "Work"},
        ])
        checks = cli_checks.check_caldav_calendars(raw)
        levels = _levels(checks)
        self.assertEqual(levels["conflict calendar 'work'"], "ok")

    def test_ics_conflict_entry_is_checked_via_fetch(self):
        raw = self._raw(entries=[
            {"name": "own-calendar", "source": "booking_calendar", "mode": "blocks"},
            {"name": "feed", "ics_url": "https://x/feed.ics"},
        ])
        with patch.object(cli_checks, "_check_ics_conflict_source",
                          return_value=("conflict calendar 'feed'", "ok", "fetched (3 events)")) as m:
            with patch.object(cli_checks, "CalDAVClient") as klass:
                klass.return_value.list_calendars.return_value = {"Yoga-Bookings": "/y/"}
                checks = cli_checks.check_caldav_calendars(raw)
        m.assert_called_once_with("feed", "https://x/feed.ics")
        self.assertEqual(_levels(checks)["conflict calendar 'feed'"], "ok")


class CheckCalendarInviteFormatTest(unittest.TestCase):
    """2026-07-15: the read-only half of the calendar-invite-format story
    (does the marker on disk match app.calendar_sync.
    CALENDAR_INVITE_FORMAT_VERSION?) as a proper structured Check -- see
    this function's own docstring in app/cli_checks.py for why (a raw
    print_fn() line during `setup -i` didn't count towards fails/warns or
    get repeated at the end, so a real stale-marker warning went
    unnoticed under a "Done -- all checks pass now" line)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.password_file = self.data_dir / "caldav_password"
        self.password_file.write_text("hunter2", encoding="utf-8")

    def _raw(self, **overrides) -> dict:
        cal = {
            "caldav_url": "https://dav.mailbox.org/caldav/",
            "username": "calendar@example.org",
            "password_file": str(self.password_file),
        }
        cal.update(overrides)
        return {"booking_calendar": cal}

    def test_not_configured_is_a_noop(self):
        self.assertEqual(cli_checks.check_calendar_invite_format({"booking_calendar": {}}, self.data_dir), [])

    def test_partially_configured_is_a_noop(self):
        raw = self._raw(username=None)
        self.assertEqual(cli_checks.check_calendar_invite_format(raw, self.data_dir), [])

    def test_no_marker_yet_is_a_warn(self):
        checks = cli_checks.check_calendar_invite_format(self._raw(), self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "calendar invite format")
        self.assertEqual(level, "warn")
        self.assertIn("resync-calendar", detail)

    def test_stale_marker_is_a_warn_naming_the_recorded_version(self):
        (self.data_dir / ".calendar_invite_format_version").write_text("0\n", encoding="utf-8")
        checks = cli_checks.check_calendar_invite_format(self._raw(), self.data_dir)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("'0'", detail)

    def test_matching_marker_is_ok(self):
        from app import calendar_sync as app_calendar_sync

        (self.data_dir / ".calendar_invite_format_version").write_text(
            f"{app_calendar_sync.CALENDAR_INVITE_FORMAT_VERSION}\n", encoding="utf-8",
        )
        checks = cli_checks.check_calendar_invite_format(self._raw(), self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "ok")


class CheckCalendarInviteResyncSkipsTest(unittest.TestCase):
    """2026-07-15/16: from a real production run, 3 occurrences hit
    persistent CalDAV conflicts during a resync, got skipped, and the run
    still printed "[ok] ... resynced 6 upcoming occurrence(s)" then "Done
    -- all checks pass now" -- because check_calendar_invite_format()
    above only ever asks "did an attempt happen", never "did every
    occurrence in that attempt actually succeed". The "OK" summary line
    didn't match what the detailed output actually showed. This is that
    second question, no network call, always safe
    to check -- no gating on CalDAV being configured at all, unlike
    check_calendar_invite_format above (the marker only ever exists after
    a real resync ran, so there's nothing to gate)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_no_marker_at_all_is_ok(self):
        checks = cli_checks.check_calendar_invite_resync_skips(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "calendar invite resync")
        self.assertEqual(level, "ok")

    def test_empty_marker_is_ok(self):
        (self.data_dir / ".calendar_invite_resync_skipped").write_text("", encoding="utf-8")
        checks = cli_checks.check_calendar_invite_resync_skips(self.data_dir)
        self.assertEqual(checks[0][1], "ok")

    def test_marker_with_entries_is_a_warn_naming_the_count_and_occurrences(self):
        (self.data_dir / ".calendar_invite_resync_skipped").write_text(
            "yoga-class-1 on 2026-07-10: HTTP 412\nyoga-class-2 on 2026-07-11: HTTP 412\n",
            encoding="utf-8",
        )
        checks = cli_checks.check_calendar_invite_resync_skips(self.data_dir)
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("2 occurrence(s)", detail)
        self.assertIn("yoga-class-1 on 2026-07-10", detail)
        self.assertIn("resync-calendar", detail)


def _app_log_line(ts: str, msg: str) -> str:
    """One line of [logging].log_file, matching
    app/logutil.py::configure_logging's own formatter --
    "%(asctime)s %(levelname)s %(message)s"."""
    return f"{ts},000 WARNING {msg}\n"


def _csp_line(ts: str, ip: str = "1.2.3.4", blocked: str = "eval",
              directive: str = "script-src", doc: str = "https://booking.example.org/book/trier-sat-yoga") -> str:
    return _app_log_line(
        ts,
        "CSP violation report from %s: blocked-uri=%r violated-directive=%r document-uri=%r"
        % (ip, blocked, directive, doc),
    )


def _csp_unparseable_line(ts: str, ip: str = "5.6.7.8") -> str:
    return _app_log_line(ts, "CSP violation report from %s: unparseable body: %r" % (ip, "garbage"))


class FindCspViolationsTest(unittest.TestCase):
    """app.cli_checks.find_csp_violations -- the one place that parses CSP
    violation reports out of [logging].log_file, shared by
    check_csp_violations() below (my-bt health/setup), `my-bt admin
    csp-violations`, and app.watchdog.check_csp_violations (threshold-
    gated alert)."""

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_no_matching_lines_is_empty(self):
        lines = [_app_log_line("2026-07-13 11:55:00", "some unrelated line")]
        self.assertEqual(cli_checks.find_csp_violations(lines, 15, now=self._now()), [])

    def test_single_violation_is_counted_once(self):
        lines = [_csp_line("2026-07-13 11:55:00")]
        violations = cli_checks.find_csp_violations(lines, 15, now=self._now())
        self.assertEqual(len(violations), 1)
        count, detail = violations[0]
        self.assertEqual(count, 1)
        self.assertIn("blocked-uri='eval'", detail)
        self.assertIn("violated-directive='script-src'", detail)
        self.assertIn("document-uri='https://booking.example.org/book/trier-sat-yoga'", detail)

    def test_identical_violations_are_deduped_and_counted(self):
        lines = [_csp_line("2026-07-13 11:55:00"), _csp_line("2026-07-13 11:56:00"),
                  _csp_line("2026-07-13 11:57:00")]
        violations = cli_checks.find_csp_violations(lines, 15, now=self._now())
        self.assertEqual(violations, [(3, violations[0][1])])

    def test_different_violations_are_kept_separate_sorted_by_count(self):
        lines = (
            [_csp_line("2026-07-13 11:55:00", doc="https://booking.example.org/book/trier-sat-yoga")] * 1
            + [_csp_line("2026-07-13 11:56:00", doc="https://booking.example.org/")] * 3
        )
        # (the list * 1/* 3 above just repeats the SAME string object, which
        # find_csp_violations still has to count individually since it
        # iterates lines, not identity-checks them)
        violations = cli_checks.find_csp_violations(lines, 15, now=self._now())
        self.assertEqual(len(violations), 2)
        self.assertEqual(violations[0][0], 3)  # most frequent first
        self.assertEqual(violations[1][0], 1)

    def test_outside_window_is_excluded(self):
        lines = [_csp_line("2026-07-13 11:00:00")]  # 60 min before now
        self.assertEqual(cli_checks.find_csp_violations(lines, 15, now=self._now()), [])

    def test_unparseable_line_timestamp_excludes_it(self):
        lines = ["garbled line with no timestamp: " + _csp_line("2026-07-13 11:55:00")]
        self.assertEqual(cli_checks.find_csp_violations(lines, 15, now=self._now()), [])

    def test_malformed_report_body_gets_its_own_bucket(self):
        lines = [_csp_unparseable_line("2026-07-13 11:55:00")]
        violations = cli_checks.find_csp_violations(lines, 15, now=self._now())
        self.assertEqual(len(violations), 1)
        count, detail = violations[0]
        self.assertEqual(count, 1)
        self.assertIn("unparseable report body", detail)


class CheckCspViolationsTest(unittest.TestCase):
    """cli_checks.check_csp_violations() -- the real-file wrapper `my-bt
    health`/`admin setup` call. Always surfaces ANY violation found
    (never threshold-gated -- that's app.watchdog's own job)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "my-booking.log"

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_no_log_file_configured_is_empty(self):
        self.assertEqual(cli_checks.check_csp_violations({}), [])

    def test_log_file_does_not_exist_is_empty(self):
        raw = {"logging": {"log_file": str(self.log_path)}}
        self.assertEqual(cli_checks.check_csp_violations(raw), [])

    def test_no_violations_in_window_is_empty(self):
        self.log_path.write_text(_app_log_line("2026-07-13 11:55:00", "nothing relevant"), encoding="utf-8")
        raw = {"logging": {"log_file": str(self.log_path)}}
        self.assertEqual(cli_checks.check_csp_violations(raw, now=self._now()), [])

    def test_violations_found_is_a_single_warn_check_with_summary(self):
        self.log_path.write_text(
            _csp_line("2026-07-13 11:55:00") + _csp_line("2026-07-13 11:56:00"), encoding="utf-8",
        )
        raw = {"logging": {"log_file": str(self.log_path)}}
        checks = cli_checks.check_csp_violations(raw, now=self._now())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "CSP violations")
        self.assertEqual(level, "warn")
        self.assertIn("2 CSP violation report(s)", detail)
        self.assertIn("2x blocked-uri=", detail)
        self.assertIn("my-bt admin csp-violations", detail)

    def test_more_than_five_distinct_groups_are_capped_with_a_plus_more_note(self):
        lines = "".join(
            _csp_line("2026-07-13 11:55:00", doc=f"https://booking.example.org/book/course-{i}") for i in range(7)
        )
        self.log_path.write_text(lines, encoding="utf-8")
        raw = {"logging": {"log_file": str(self.log_path)}}
        checks = cli_checks.check_csp_violations(raw, now=self._now())
        self.assertIn("+2 more distinct", checks[0][2])

    def test_custom_window_minutes_is_respected(self):
        self.log_path.write_text(_csp_line("2026-07-13 11:30:00"), encoding="utf-8")  # 30 min before now
        raw = {"logging": {"log_file": str(self.log_path)}, "watchdog": {"window_minutes": 45}}
        checks = cli_checks.check_csp_violations(raw, now=self._now())
        self.assertEqual(len(checks), 1)
        raw["watchdog"]["window_minutes"] = 15
        self.assertEqual(cli_checks.check_csp_violations(raw, now=self._now()), [])


class ExpectedCspHashesTest(unittest.TestCase):
    """expected_csp_hashes() -- the proactive half of the CSP-hash
    automation (contrast check_csp_violations() above, which is purely
    reactive). Computes every inline <script> hash this app can currently
    produce straight from source, without ever rendering a real page --
    safe only because every one of these constants is deliberately
    non-interpolated (see each one's own history in
    site/nginx-locations.conf.example's CSP comment)."""

    _STATIC_LABELS = {
        "templates._SUBMIT_FEEDBACK_SCRIPT",
        "webapp._RESEND_COOLDOWN_SCRIPT",
        "webapp._RESEND_INLINE_COOLDOWN_SCRIPT",
        "webapp._LOCKOUT_COUNTDOWN_SCRIPT",
        "webapp._DIALOG_WIRING_SCRIPT",
        "webapp._CANCEL_ENTIRE_SESSION_SCRIPT",
        "webapp._SORTABLE_FILTERABLE_TABLE_SCRIPT",
        "webapp._BOOKING_FORM_SCRIPT",
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_no_static_site_dir_returns_only_the_eight_static_constants(self):
        hashes = cli_checks.expected_csp_hashes({})
        self.assertEqual(set(hashes), self._STATIC_LABELS)
        for h in hashes.values():
            self.assertTrue(h.startswith("sha256-"))

    def test_every_static_hash_matches_hashing_the_constant_directly(self):
        from app import templates, webapp
        hashes = cli_checks.expected_csp_hashes({})
        pairs = {
            "templates._SUBMIT_FEEDBACK_SCRIPT": templates._SUBMIT_FEEDBACK_SCRIPT,
            "webapp._BOOKING_FORM_SCRIPT": webapp._BOOKING_FORM_SCRIPT,
        }
        for label, constant in pairs.items():
            body = site_render.extract_script_bodies(constant)[0]
            expected = "sha256-" + __import__("base64").b64encode(
                __import__("hashlib").sha256(body.encode("utf-8")).digest()
            ).decode()
            self.assertEqual(hashes[label], expected)

    def test_static_site_dir_configured_but_no_index_html_still_just_eight(self):
        raw = {"site": {"static_site_dir": str(self.dir)}}
        hashes = cli_checks.expected_csp_hashes(raw)
        self.assertEqual(set(hashes), self._STATIC_LABELS)

    def test_static_site_dir_with_index_html_adds_its_script_hashes(self):
        index_html = (
            "<html><body>"
            "<script>console.log('one');</script>"
            "<div id=\"schedule-exceptions\"></div>"
            "<script>console.log('two');</script>"
            "</body></html>"
        )
        (self.dir / "index.html").write_text(index_html, encoding="utf-8")
        raw = {"site": {"static_site_dir": str(self.dir)}}
        hashes = cli_checks.expected_csp_hashes(raw)
        self.assertEqual(set(hashes), self._STATIC_LABELS | {"index.html script #1", "index.html script #2"})
        import base64
        import hashlib
        expected_one = "sha256-" + base64.b64encode(hashlib.sha256(b"console.log('one');").digest()).decode()
        self.assertEqual(hashes["index.html script #1"], expected_one)

    def test_html_comment_mentioning_script_does_not_create_a_phantom_entry(self):
        # Real incident this guards against -- see extract_script_bodies()'s
        # own docstring: a stray "<script>" in developer prose inside an
        # HTML comment must never be mistaken for a real opening tag.
        index_html = (
            "<!-- the real <script> further down does the thing -->"
            "<script>console.log('real');</script>"
        )
        (self.dir / "index.html").write_text(index_html, encoding="utf-8")
        raw = {"site": {"static_site_dir": str(self.dir)}}
        hashes = cli_checks.expected_csp_hashes(raw)
        self.assertEqual(set(hashes) - self._STATIC_LABELS, {"index.html script #1"})


class NginxCspScriptHashesTest(unittest.TestCase):
    """_nginx_csp_script_hashes() -- parses the live, deployed
    Content-Security-Policy header's script-src 'sha256-...' entries out of
    the ONE server block matching [site].base_url's hostname."""

    def _raw(self, base_url="https://booking.example.org"):
        return {"site": {"base_url": base_url}}

    def test_nginx_unavailable_returns_none(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            self.assertIsNone(cli_checks._nginx_csp_script_hashes(self._raw()))

    def test_no_matching_server_block_returns_none(self):
        merged = "server {\n    server_name other.org;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            self.assertIsNone(cli_checks._nginx_csp_script_hashes(self._raw()))

    def test_no_csp_header_returns_none(self):
        merged = "server {\n    server_name booking.example.org;\n}\n"
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            self.assertIsNone(cli_checks._nginx_csp_script_hashes(self._raw()))

    def test_extracts_every_hash_in_the_matching_blocks_header(self):
        merged = (
            "server {\n"
            "    server_name booking.example.org;\n"
            '    add_header Content-Security-Policy "default-src \'self\'; '
            "script-src 'self' 'sha256-AAA=' 'sha256-BBB='; "
            'style-src \'self\';" always;\n'
            "}\n"
        )
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            result = cli_checks._nginx_csp_script_hashes(self._raw())
        self.assertEqual(result, {"sha256-AAA=", "sha256-BBB="})


class CheckCspHashesDeployedTest(unittest.TestCase):
    """check_csp_hashes_deployed() -- compares expected_csp_hashes()
    (computed from source) against the live nginx CSP header's actual
    hash set, proactively catching a forgotten hash update."""

    def _raw(self, base_url="https://booking.example.org"):
        return {"site": {"base_url": base_url}}

    def _merged_with_hashes(self, hashes):
        hash_tokens = " ".join(f"'{h}'" for h in hashes)
        return (
            "server {\n"
            "    server_name booking.example.org;\n"
            '    add_header Content-Security-Policy "default-src \'self\'; '
            f"script-src 'self' {hash_tokens}; "
            'style-src \'self\';" always;\n'
            "}\n"
        )

    def test_nginx_unavailable_is_a_no_op(self):
        with patch("app.cli_checks.shutil.which", return_value=None):
            self.assertEqual(cli_checks.check_csp_hashes_deployed(self._raw()), [])

    def test_all_expected_hashes_present_reports_ok(self):
        expected = cli_checks.expected_csp_hashes(self._raw())
        merged = self._merged_with_hashes(expected.values())
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            checks = cli_checks.check_csp_hashes_deployed(self._raw())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(label, "CSP script hashes deployed")
        self.assertEqual(level, "ok")
        self.assertIn(f"all {len(expected)}", detail)

    def test_one_missing_hash_reports_warn_with_label_and_hash(self):
        expected = cli_checks.expected_csp_hashes(self._raw())
        labels = list(expected)
        dropped_label = labels[0]
        remaining = {label: h for label, h in expected.items() if label != dropped_label}
        merged = self._merged_with_hashes(remaining.values())
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged})()):
            checks = cli_checks.check_csp_hashes_deployed(self._raw())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn(dropped_label, detail)
        self.assertIn(expected[dropped_label], detail)


class CspScriptSrcPatchTest(unittest.TestCase):
    """csp_script_src_patch() -- the self-heal half (2026-07-16, the operator:
    "can we automate and fix this ... maybe can self-heal?"). Pure string
    transform: no file I/O, no subprocess -- app/cli_setup.py::
    interactive_setup() is the only place that reads/writes the real file
    and runs (and checks the actual pass/fail of) `nginx -t`."""

    _CONF = (
        'add_header Content-Security-Policy "default-src \'self\'; '
        "script-src 'self' 'sha256-AAAA=' 'sha256-BBBB='; "
        'style-src \'self\' \'unsafe-inline\'; frame-ancestors \'self\';" always;'
    )

    def test_appends_new_hash_after_script_src(self):
        out = cli_checks.csp_script_src_patch(self._CONF, ["sha256-NEW="])
        self.assertIn("script-src 'sha256-NEW=' 'self' 'sha256-AAAA=' 'sha256-BBBB='", out)

    def test_keeps_every_existing_hash_and_directive(self):
        out = cli_checks.csp_script_src_patch(self._CONF, ["sha256-NEW="])
        self.assertIn("'sha256-AAAA='", out)
        self.assertIn("'sha256-BBBB='", out)
        self.assertIn("style-src 'self' 'unsafe-inline'", out)
        self.assertIn("frame-ancestors 'self'", out)

    def test_appends_multiple_hashes(self):
        out = cli_checks.csp_script_src_patch(self._CONF, ["sha256-NEW1=", "sha256-NEW2="])
        self.assertIn("script-src 'sha256-NEW1=' 'sha256-NEW2=' 'self'", out)

    def test_empty_list_is_a_no_op(self):
        self.assertEqual(cli_checks.csp_script_src_patch(self._CONF, []), self._CONF)

    def test_no_csp_header_raises_value_error(self):
        with self.assertRaises(ValueError):
            cli_checks.csp_script_src_patch("server { listen 443; }", ["sha256-NEW="])

    def test_no_script_src_directive_raises_value_error(self):
        conf = 'add_header Content-Security-Policy "default-src \'self\';" always;'
        with self.assertRaises(ValueError):
            cli_checks.csp_script_src_patch(conf, ["sha256-NEW="])

    def test_only_touches_the_one_csp_line_in_a_larger_file(self):
        conf = f"server {{\n    server_name booking.example.org;\n    {self._CONF}\n    other_directive on;\n}}\n"
        out = cli_checks.csp_script_src_patch(conf, ["sha256-NEW="])
        self.assertIn("server_name booking.example.org;", out)
        self.assertIn("other_directive on;", out)
        self.assertIn("'sha256-NEW='", out)


class ParseLastDurationTest(unittest.TestCase):
    def test_hours_only(self):
        from datetime import timedelta
        self.assertEqual(cli_checks.parse_last_duration("2h"), timedelta(hours=2))

    def test_minutes_only(self):
        from datetime import timedelta
        self.assertEqual(cli_checks.parse_last_duration("90m"), timedelta(minutes=90))

    def test_combined_hours_and_minutes(self):
        from datetime import timedelta
        self.assertEqual(cli_checks.parse_last_duration("1h30m"), timedelta(hours=1, minutes=30))

    def test_days_hours_minutes_seconds(self):
        from datetime import timedelta
        self.assertEqual(
            cli_checks.parse_last_duration("1d2h3m4s"),
            timedelta(days=1, hours=2, minutes=3, seconds=4),
        )

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            cli_checks.parse_last_duration("")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            cli_checks.parse_last_duration("banana")

    def test_wrong_order_raises(self):
        # must be d/h/m/s in that order, not e.g. "30m1h"
        with self.assertRaises(ValueError):
            cli_checks.parse_last_duration("30m1h")


class ResolveReportWindowTest(unittest.TestCase):
    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_last_takes_precedence(self):
        from datetime import timedelta
        start, end, description = cli_checks.resolve_report_window(last="2h", now=self._now())
        self.assertEqual(end, self._now())
        self.assertEqual(start, self._now() - timedelta(hours=2))
        self.assertIn("last 2h", description)

    def test_since_and_till_both_given(self):
        start, end, description = cli_checks.resolve_report_window(
            since="2026-07-13T09:00:00", till="2026-07-13T10:00:00", now=self._now(),
        )
        from datetime import datetime, timezone
        self.assertEqual(start, datetime(2026, 7, 13, 9, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc))

    def test_since_only_defaults_till_to_now(self):
        start, end, _ = cli_checks.resolve_report_window(since="2026-07-13T09:00:00", now=self._now())
        self.assertEqual(end, self._now())

    def test_till_only_defaults_since_to_24h_before(self):
        from datetime import timedelta
        start, end, _ = cli_checks.resolve_report_window(till="2026-07-13T10:00:00", now=self._now())
        self.assertEqual(end - start, timedelta(hours=24))

    def test_none_given_falls_back_to_nginx_last_restart(self):
        from datetime import datetime, timezone
        restart = datetime(2026, 7, 13, 8, 0, 0, tzinfo=timezone.utc)
        with patch("app.cli_checks.nginx_last_restart_at", return_value=restart):
            start, end, description = cli_checks.resolve_report_window(now=self._now())
        self.assertEqual(start, restart)
        self.assertEqual(end, self._now())
        self.assertIn("nginx's last restart", description)

    def test_none_given_and_restart_unknown_falls_back_to_24h(self):
        from datetime import timedelta
        with patch("app.cli_checks.nginx_last_restart_at", return_value=None):
            start, end, description = cli_checks.resolve_report_window(now=self._now())
        self.assertEqual(end - start, timedelta(hours=24))
        self.assertIn("could not be determined", description)


class NginxGlobalAndErrorLogDerivationTest(unittest.TestCase):
    """_nginx_error_log_for_host/_nginx_global_access_log/
    _nginx_global_error_log -- mirror NginxAccessLogForHostTest's own
    patterns exactly, for the three siblings added alongside `my-bt admin
    health report`/`errors`."""

    def _raw(self, base_url="https://example.org"):
        return {"site": {"base_url": base_url}}

    def _patched(self, merged):
        return (
            patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"),
            patch("app.cli_checks.subprocess.run",
                  return_value=type("R", (), {"returncode": 0, "stdout": merged})()),
        )

    def test_error_log_for_host_finds_vhost_specific(self):
        merged = """
        server {
            server_name example.org;
            access_log /var/log/nginx/example.access.log;
            error_log  /var/log/nginx/example.error.log;
        }
        """
        p1, p2 = self._patched(merged)
        with p1, p2:
            log = cli_checks._nginx_error_log_for_host(self._raw())
        self.assertEqual(log, "/var/log/nginx/example.error.log")

    def test_error_log_for_host_falls_back_to_http_level(self):
        merged = """
        error_log /var/log/nginx/error.log;
        server {
            server_name example.org;
        }
        """
        p1, p2 = self._patched(merged)
        with p1, p2:
            log = cli_checks._nginx_error_log_for_host(self._raw())
        self.assertEqual(log, "/var/log/nginx/error.log")

    def test_global_access_log_ignores_vhost_specific_override(self):
        merged = """
        access_log /var/log/nginx/access.log;
        server {
            server_name example.org;
            access_log /var/log/nginx/example.access.log;
        }
        """
        p1, p2 = self._patched(merged)
        with p1, p2:
            log = cli_checks._nginx_global_access_log(self._raw())
        self.assertEqual(log, "/var/log/nginx/access.log")

    def test_global_access_log_none_when_only_vhost_level_set(self):
        merged = """
        server {
            server_name example.org;
            access_log /var/log/nginx/example.access.log;
        }
        """
        p1, p2 = self._patched(merged)
        with p1, p2:
            log = cli_checks._nginx_global_access_log(self._raw())
        self.assertIsNone(log)

    def test_global_error_log_found_at_http_level(self):
        merged = """
        error_log /var/log/nginx/error.log;
        server {
            server_name example.org;
        }
        """
        p1, p2 = self._patched(merged)
        with p1, p2:
            log = cli_checks._nginx_global_error_log(self._raw())
        self.assertEqual(log, "/var/log/nginx/error.log")


class HealthReportLogSourcesTest(unittest.TestCase):
    def test_returns_five_labeled_sources_in_order(self):
        raw = {"logging": {"log_file": "/var/lib/my-booking/my-booking.log"}}
        with patch("app.cli_checks._nginx_global_access_log", return_value="/var/log/nginx/access.log"), \
             patch("app.cli_checks._nginx_global_error_log", return_value="/var/log/nginx/error.log"), \
             patch("app.cli_checks._nginx_access_log_for_host", return_value="/var/log/nginx/yoga.access.log"), \
             patch("app.cli_checks._nginx_error_log_for_host", return_value="/var/log/nginx/yoga.error.log"):
            sources = cli_checks.health_report_log_sources(raw)
        self.assertEqual([label for label, _ in sources], [
            "nginx global access log", "nginx global error log",
            "nginx vhost access log", "nginx vhost error log", "app log",
        ])
        self.assertEqual(dict(sources)["app log"], "/var/lib/my-booking/my-booking.log")


class NginxLastRestartAtTest(unittest.TestCase):
    def test_returns_none_when_undetectable(self):
        with patch("app.cli_checks._service_active_since", return_value=None):
            self.assertIsNone(cli_checks.nginx_last_restart_at())

    def test_converts_epoch_to_utc_datetime(self):
        with patch("app.cli_checks._service_active_since", return_value=1752400000.0):
            result = cli_checks.nginx_last_restart_at()
        from datetime import datetime, timezone
        self.assertEqual(result, datetime.fromtimestamp(1752400000.0, tz=timezone.utc))


class LogLineTimestampParsersTest(unittest.TestCase):
    def test_nginx_error_log_timestamp_parses_local_time(self):
        line = "2026/07/13 12:00:00 [error] 123#0: something broke\n"
        ts = cli_checks._nginx_error_log_timestamp(line)
        self.assertIsNotNone(ts)

    def test_nginx_error_log_timestamp_none_for_unrelated_line(self):
        self.assertIsNone(cli_checks._nginx_error_log_timestamp("not a log line\n"))

    def test_nginx_access_log_timestamp_parses_combined_format(self):
        line = '1.2.3.4 - - [13/Jul/2026:12:00:00 +0000] "GET / HTTP/1.1" 200 100\n'
        ts = cli_checks._nginx_access_log_timestamp(line)
        from datetime import datetime, timezone
        self.assertEqual(ts, datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc))


class FilterLinesByWindowTest(unittest.TestCase):
    def test_keeps_only_lines_in_window(self):
        from datetime import datetime, timezone
        lines = [
            _app_log_line("2026-07-13 11:00:00", "too early"),
            _app_log_line("2026-07-13 11:30:00", "in window"),
            _app_log_line("2026-07-13 12:30:00", "too late"),
        ]
        start = datetime(2026, 7, 13, 11, 15, tzinfo=timezone.utc)
        end = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        kept = cli_checks._filter_lines_by_window(lines, start, end, cli_checks.parse_app_log_timestamp)
        self.assertEqual(len(kept), 1)
        self.assertIn("in window", kept[0])

    def test_unparseable_timestamp_excluded(self):
        from datetime import datetime, timezone
        lines = ["not a timestamped line\n"]
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        end = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            cli_checks._filter_lines_by_window(lines, start, end, cli_checks.parse_app_log_timestamp), [],
        )


class GroupCspViolationLinesTest(unittest.TestCase):
    def test_groups_and_counts_with_no_time_filtering(self):
        lines = [_csp_line("2026-07-13 11:55:00"), _csp_line("2026-07-13 11:56:00")]
        violations = cli_checks.group_csp_violation_lines(lines)
        self.assertEqual(violations, [(2, violations[0][1])])
        self.assertIn("blocked-uri='eval'", violations[0][1])


class BuildHealthReportTest(unittest.TestCase):
    """cli_checks.build_health_report -- the pure assembly function behind
    `my-bt admin health report`/`errors`; real file/journalctl I/O is the
    CALLER's job (scripts/my-bt), so this only ever gets already-decided
    paths and already-gathered sshd/service lines."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

    def _window(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 13, 11, 0, 0, tzinfo=timezone.utc), self._now()

    def test_missing_sources_are_labeled_not_configured(self):
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources", return_value=[("app log", None)]):
            text = cli_checks.build_health_report({}, start, end, "test window")
        self.assertIn("app log (not configured / not detected)", text)

    def test_report_mode_includes_every_matching_line(self):
        app_log = self.dir / "app.log"
        app_log.write_text(
            _app_log_line("2026-07-13 11:30:00", "just some info line") +
            _app_log_line("2026-07-13 10:00:00", "outside the window"),
            encoding="utf-8",
        )
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources", return_value=[("app log", str(app_log))]):
            text = cli_checks.build_health_report({}, start, end, "test window", errors_only=False)
        self.assertIn("just some info line", text)
        self.assertNotIn("outside the window", text)

    def test_errors_mode_filters_app_log_to_warning_and_above(self):
        app_log = self.dir / "app.log"
        app_log.write_text(
            _app_log_line("2026-07-13 11:30:00", "harmless info, not shown in errors mode")
            .replace("WARNING", "INFO")
            + _app_log_line("2026-07-13 11:31:00", "a real problem"),
            encoding="utf-8",
        )
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources", return_value=[("app log", str(app_log))]):
            text = cli_checks.build_health_report({}, start, end, "test window", errors_only=True)
        self.assertNotIn("harmless info", text)
        self.assertIn("a real problem", text)

    def test_errors_mode_filters_access_log_to_4xx_5xx(self):
        access_log = self.dir / "access.log"
        access_log.write_text(
            '1.2.3.4 - - [13/Jul/2026:11:30:00 +0000] "GET / HTTP/1.1" 200 100\n'
            '5.6.7.8 - - [13/Jul/2026:11:31:00 +0000] "GET /nope HTTP/1.1" 404 0\n',
            encoding="utf-8",
        )
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources",
                   return_value=[("nginx vhost access log", str(access_log))]):
            text = cli_checks.build_health_report({}, start, end, "test window", errors_only=True)
        self.assertNotIn("GET / HTTP", text)
        self.assertIn("GET /nope HTTP", text)

    def test_errors_mode_appends_grouped_csp_summary(self):
        app_log = self.dir / "app.log"
        app_log.write_text(_csp_line("2026-07-13 11:30:00") + _csp_line("2026-07-13 11:31:00"),
                            encoding="utf-8")
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources", return_value=[("app log", str(app_log))]):
            text = cli_checks.build_health_report({}, start, end, "test window", errors_only=True)
        self.assertIn("CSP violations, grouped", text)
        self.assertIn("2x blocked-uri=", text)

    def test_errors_mode_groups_csp_violations_from_the_journal_too(self):
        # 2026-07-13, real gap hit live: without [logging].log_file
        # configured, health_report_log_sources()'s "app log" entry is
        # None, so app_log_window is always empty -- but the app's own
        # WARNING lines (including CSP violation reports) still reach
        # `journalctl -u my-booking.service` via the service's own stdout.
        # The grouped CSP summary must still appear when the violation is
        # ONLY visible there, not just when [logging].log_file happens to
        # be configured too.
        start, end = self._window()
        service_lines = [
            _csp_line("2026-07-13 11:30:00"), _csp_line("2026-07-13 11:31:00"),
        ]
        with patch("app.cli_checks.health_report_log_sources", return_value=[("app log", None)]):
            text = cli_checks.build_health_report(
                {}, start, end, "test window", app_service_lines=service_lines, errors_only=True,
            )
        self.assertIn("CSP violations, grouped", text)
        self.assertIn("2x blocked-uri=", text)

    def test_sshd_and_service_lines_are_included_when_given(self):
        start, end = self._window()
        with patch("app.cli_checks.health_report_log_sources", return_value=[]):
            text = cli_checks.build_health_report(
                {}, start, end, "test window",
                sshd_lines=["sshd[1]: Failed password for root\n"],
                app_service_lines=["my-booking.service: started\n"],
            )
        self.assertIn("Failed password for root", text)
        self.assertIn("my-booking.service: started", text)


if __name__ == "__main__":
    unittest.main()
