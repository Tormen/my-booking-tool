import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli_checks, maintenance, site_render


def _levels(checks):
    return {label: level for label, level, _ in checks}


class CheckSecretsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _raw(self, **paths) -> dict:
        return {
            "calendar": {"caldav_password_file": paths.get("caldav_password")},
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


class CheckGroupMembershipTest(unittest.TestCase):
    def test_root_is_always_ok(self):
        with patch.dict("os.environ", {"SUDO_USER": "root"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": []})()
            checks = cli_checks.check_group_membership()
        self.assertEqual(checks[0][1], "ok")

    def test_member_is_ok(self):
        with patch.dict("os.environ", {"SUDO_USER": "operator"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": ["operator"]})()
            checks = cli_checks.check_group_membership()
        self.assertEqual(checks[0][1], "ok")

    def test_non_member_warns_with_usermod_command(self):
        with patch.dict("os.environ", {"SUDO_USER": "operator"}, clear=False), \
             patch("grp.getgrnam") as getgrnam:
            getgrnam.return_value = type("G", (), {"gr_mem": ["someoneelse"]})()
            checks = cli_checks.check_group_membership()
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("usermod -aG my-booking operator", detail)

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
    "can't read" even right after the operator ran the exact setfacl command it
    printed. These tests mock `_my_booking_can_read` directly (its own
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
            location /my { proxy_pass http://127.0.0.1:8811; }
            location /admin { proxy_pass http://127.0.0.1:8811; }
        }
        """
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": merged, "stderr": ""})()):
            checks = cli_checks.check_nginx_locations()
        self.assertEqual(len(checks), 8)
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


class CheckNginxConfDeployedTest(unittest.TestCase):
    """check_nginx_conf_deployed() reads [site].nginx_conf_path DIRECTLY off
    disk (not `nginx -T`, not a checkout glob) -- and unlike every other
    optional settings.toml-path check, reports "fail" (not "warn") for any
    problem, since configuring this path at all is a deliberate statement
    that this exact file is real and matters (2026-07-10, the operator: "truly
    ERROR out in case there is a problem")."""

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
        checks = cli_checks.check_nginx_conf_deployed(self._raw())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "fail")
        self.assertIn("not found", detail)

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
    called on the actual server (the operator's rename request: "all content in
    site/ works the same")."""

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
    tests are about check_caldav_calendars()'s own logic (config
    gating, error handling, want/found diffing), so CalDAVClient is
    mocked wholesale rather than driven through a fake transport."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.password_file = Path(self._tmp.name) / "caldav_password"
        self.password_file.write_text("hunter2", encoding="utf-8")

    def _raw(self, **overrides) -> dict:
        cal = {
            "caldav_url": "https://dav.mailbox.org/caldav/",
            "caldav_username": "calendar@example.org",
            "caldav_password_file": str(self.password_file),
            "booking_calendar": "Yoga-Bookings",
            "conflict_calendars": ["Calendar", "Yoga-Bookings"],
        }
        cal.update(overrides)
        return {"calendar": cal}

    def test_not_configured_is_a_noop(self):
        self.assertEqual(cli_checks.check_caldav_calendars({"calendar": {}}), [])

    def test_partially_configured_is_a_noop(self):
        raw = self._raw(caldav_username=None)
        self.assertEqual(cli_checks.check_caldav_calendars(raw), [])

    def test_missing_password_file_is_a_noop(self):
        # check_secrets() already reports a missing secret file -- this
        # check shouldn't also complain about it a second way.
        raw = self._raw(caldav_password_file=str(self.password_file) + ".missing")
        self.assertEqual(cli_checks.check_caldav_calendars(raw), [])

    @patch("app.cli_checks.CalDAVClient")
    def test_connection_or_auth_failure_is_one_warn(self, mock_cls):
        mock_cls.return_value.list_calendars.side_effect = RuntimeError(
            "PROPFIND https://dav.mailbox.org/caldav/ -> HTTP 401"
        )
        checks = cli_checks.check_caldav_calendars(self._raw())
        self.assertEqual(len(checks), 1)
        label, level, detail = checks[0]
        self.assertEqual(level, "warn")
        self.assertIn("401", detail)

    @patch("app.cli_checks.CalDAVClient")
    def test_calendars_found_are_ok(self, mock_cls):
        mock_cls.return_value.list_calendars.return_value = {
            "Calendar": "/caldav/Y2FsOi8vMC8zMQ/",
            "Yoga-Bookings": "/caldav/somewhere/",
        }
        checks = cli_checks.check_caldav_calendars(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["CalDAV calendar 'Calendar'"], "ok")
        self.assertEqual(levels["CalDAV calendar 'Yoga-Bookings'"], "ok")

    @patch("app.cli_checks.CalDAVClient")
    def test_calendar_not_found_fails_with_the_real_list(self, mock_cls):
        # The exact failure mode hit in production 2026-07-05: the
        # configured base URL only ever resolved "WebDAV Root" -- the
        # detail message should surface what was actually found so this
        # is diagnosable without a separate curl/journalctl trip.
        mock_cls.return_value.list_calendars.return_value = {"WebDAV Root": "/"}
        checks = cli_checks.check_caldav_calendars(self._raw())
        levels = _levels(checks)
        self.assertEqual(levels["CalDAV calendar 'Calendar'"], "fail")
        self.assertEqual(levels["CalDAV calendar 'Yoga-Bookings'"], "fail")
        details = {label: detail for label, _, detail in checks}
        self.assertIn("WebDAV Root", details["CalDAV calendar 'Calendar'"])

    @patch("app.cli_checks.CalDAVClient")
    def test_booking_and_conflict_overlap_is_deduped(self, mock_cls):
        mock_cls.return_value.list_calendars.return_value = {"Calendar": "/x"}
        raw = self._raw(booking_calendar="Calendar", conflict_calendars=["Calendar"])
        checks = cli_checks.check_caldav_calendars(raw)
        self.assertEqual(len(checks), 1)


if __name__ == "__main__":
    unittest.main()
