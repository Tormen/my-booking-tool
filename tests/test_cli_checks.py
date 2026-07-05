import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli_checks, site_render


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
        verify_output = "S.5....T.  c /opt/my-booking/settings.toml\n"

        def run(cmd, capture_output, text):
            if cmd[1] == "-q":
                return type("R", (), {"returncode": 0, "stdout": ""})()
            return type("R", (), {"returncode": 0, "stdout": verify_output})()

        with patch("app.cli_checks.shutil.which", return_value="/usr/bin/rpm"), \
             patch("app.cli_checks.subprocess.run", side_effect=run):
            checks = cli_checks.check_rpm_verify()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], "ok")

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


if __name__ == "__main__":
    unittest.main()
