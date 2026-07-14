"""scripts/install.sh (the manual/dev install path) must keep parity with
the RPM for everything the app actually needs at runtime -- 2026-07-14,
repo-review finding G1: email_templates/ was never installed (so EVERY
email send raised FileNotFoundError on an install.sh-installed system,
since app/email_templates.py's built-in fallback dir didn't exist), and
the watchdog/git-snapshot systemd unit pairs were missing too. Both
postdate the script; nothing cross-checked it since.

Plain text checks over the script's own source -- the same cheap
consistency-test pattern tests/test_render_site_script.py already uses
for build-time scripts -- rather than actually executing a root-only
installer in the suite."""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")
SYSTEMD_DIR = os.path.join(REPO_ROOT, "systemd")


class InstallScriptParityTest(unittest.TestCase):
    def setUp(self):
        with open(INSTALL_SH, encoding="utf-8") as f:
            self.text = f.read()

    def test_installs_every_systemd_unit_the_repo_ships(self):
        # Whatever unit files exist under systemd/ (the same set the RPM's
        # %install enumerates), install.sh must install too -- a NEW unit
        # added later fails this immediately instead of silently only
        # shipping via the RPM path.
        units = sorted(n for n in os.listdir(SYSTEMD_DIR) if n.endswith((".service", ".timer")))
        self.assertTrue(units, "no unit files found -- test looking in the wrong place?")
        for name in units:
            self.assertIn(
                f"systemd/{name}", self.text,
                f"scripts/install.sh does not install systemd/{name} (the RPM does)",
            )

    def test_installs_the_email_templates_fallback_dir(self):
        # app/email_templates.py::load_email_template falls back to
        # /opt/my-booking/email_templates -- without this, every email
        # send crashes on an install.sh-installed system.
        self.assertIn("/opt/my-booking/email_templates", self.text)
        self.assertIn('email_templates/*.txt', self.text)
        self.assertIn('email_templates/*.html', self.text)

    def test_never_overwrites_an_existing_customized_template(self):
        # Mirrors the RPM's %config(noreplace) treatment: copy-if-missing
        # only -- look for the guard on the destination path.
        self.assertIn('[ -f "$dest" ] ||', self.text)


if __name__ == "__main__":
    unittest.main()
