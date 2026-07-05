"""Tests the `my-bt setup` / `my-bt setup --interactive` logic
(app/cli_setup.py) via dependency injection -- prompt/read_secret/run/
is_root are all fake callables here instead of a real tty/subprocess/
root, so the whole branching logic (what gets asked, what gets skipped,
what gets written) is exercised deterministically, the same way this was
manually smoke-tested via piped stdin during development."""
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli_setup, site_render


def _raw(**overrides) -> dict:
    base = {
        "calendar": {"caldav_password_file": None},
        "smtp": {"password_file": None},
        "admin": {"password_hash_file": None},
        "privacy": {"erasure_pepper_file": None, "retention_months": 24, "canceled_retention_months": 6},
        "course": [{"shortname": "yoga-class-1"}],
    }
    base.update(overrides)
    return base


class FakePrompts:
    """Answers each yes/no question by matching `message` against a set
    of substrings (checked in insertion order, first match wins) rather
    than by position -- `interactive_setup` always asks some questions
    unconditionally (e.g. the step-4 nginx prompt) regardless of what a
    given test cares about, so a positional queue is brittle to steps a
    test isn't exercising. Anything unmatched gets `default` (False --
    "decline everything you're not explicitly testing"). Every question
    asked is recorded in `asked` for assertions."""

    def __init__(self, answers: dict[str, bool] | None = None, default: bool = False):
        self._answers = list((answers or {}).items())
        self._default = default
        self.asked: list[str] = []

    def __call__(self, message: str) -> bool:
        self.asked.append(message)
        for substr, value in self._answers:
            if substr in message:
                return value
        return self._default

    def asked_matching(self, substr: str) -> list[str]:
        return [m for m in self.asked if substr in m]


class PrintReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def test_prints_all_eight_numbered_steps(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        for n in range(1, 9):
            self.assertIn(f"{n}.", text)

    def test_reports_missing_secret(self):
        lines: list[str] = []
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("caldav_password" in ln and "FAIL" in ln for ln in lines))

    def test_static_site_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "static_site_dir" in ln for ln in lines))


class InteractiveSetupSecretsTest(unittest.TestCase):
    """Step 4 (nginx) is always asked unconditionally regardless of what
    these tests care about -- FakePrompts defaults it (and anything else
    unmatched) to False, so these tests only need to supply an answer for
    the specific secret prompt they're exercising."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.secrets_dir = self.home / "secrets"

    def _run(self, raw, answers, read_secret=lambda label: "s3cr3t", is_root=lambda: False):
        prompt = FakePrompts(answers)
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, read_secret=read_secret, run=calls.append,
            is_root=is_root, print_fn=lambda *_: None,
        )
        return prompt, calls

    def test_declining_a_missing_secret_leaves_it_unwritten(self):
        path = self.secrets_dir / "caldav_password"
        raw = _raw(calendar={"caldav_password_file": str(path)})
        self._run(raw, answers={"caldav_password": False})
        self.assertFalse(path.exists())

    def test_accepting_a_missing_plain_secret_writes_it_mode_600(self):
        path = self.secrets_dir / "caldav_password"
        raw = _raw(calendar={"caldav_password_file": str(path)})
        self._run(raw, answers={"caldav_password": True}, read_secret=lambda label: "hunter2")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text().strip(), "hunter2")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_erasure_pepper_auto_generates_valid_hex(self):
        path = self.secrets_dir / "erasure_pepper"
        raw = _raw(privacy={"erasure_pepper_file": str(path), "retention_months": 24, "canceled_retention_months": 6})
        self._run(raw, answers={"erasure_pepper": True})
        content = path.read_text().strip()
        self.assertEqual(len(bytes.fromhex(content)), 32)

    def test_admin_password_hash_uses_real_hashing(self):
        path = self.secrets_dir / "admin_password_hash"
        raw = _raw(admin={"password_hash_file": str(path)})
        answers_iter = iter(["s3cret!", "s3cret!"])
        self._run(raw, answers={"admin password": True}, read_secret=lambda label: next(answers_iter))
        content = path.read_text().strip()
        self.assertIn("$", content)  # looks like a hash, not the plain password
        self.assertNotIn("s3cret!", content)

    def test_admin_password_mismatch_is_not_written(self):
        path = self.secrets_dir / "admin_password_hash"
        raw = _raw(admin={"password_hash_file": str(path)})
        answers_iter = iter(["one", "two"])
        self._run(raw, answers={"admin password": True}, read_secret=lambda label: next(answers_iter))
        self.assertFalse(path.exists())

    def test_already_present_secret_is_never_prompted(self):
        path = self.secrets_dir / "caldav_password"
        self.secrets_dir.mkdir()
        path.write_text("already-here")
        raw = _raw(calendar={"caldav_password_file": str(path)})
        prompt, _ = self._run(raw, answers={})
        self.assertEqual(prompt.asked_matching("caldav_password"), [])


class InteractiveSetupRpmnewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def test_declines_merge_leaves_rpmnew_in_place(self):
        rpmnew = Path(self.settings_path + ".rpmnew")
        rpmnew.write_text("new version")
        prompt = FakePrompts({"vimdiff": False})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertTrue(rpmnew.exists())

    def test_accepting_merge_runs_vimdiff_and_can_remove_rpmnew(self):
        rpmnew = Path(self.settings_path + ".rpmnew")
        rpmnew.write_text("new version")
        prompt = FakePrompts({"vimdiff": True, "Merged?": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertFalse(rpmnew.exists())
        self.assertTrue(any("vimdiff" in c[0] for c in calls))


class InteractiveSetupPrivilegedStepsTest(unittest.TestCase):
    """Steps 5-7 (group membership, systemd, SELinux) call app.cli_checks
    directly rather than through an injected dependency -- mock those
    functions here so these tests are deterministic regardless of
    whether the machine running the suite happens to have systemd/
    SELinux/a real my-booking group, instead of depending on real system
    state (fragile, and not what these tests are meant to exercise)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

        self._patches = [
            patch("app.cli_checks.check_group_membership",
                  return_value=[("my-booking group membership (operator)", "warn", "not a member")]),
            patch("app.cli_checks.check_systemd",
                  return_value=[("my-booking.service", "warn", "not enabled"),
                                ("my-booking-retention.timer", "ok", "enabled, active")]),
            patch("app.cli_checks.check_selinux",
                  return_value=[("SELinux httpd_can_network_connect", "fail", "off")]),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_non_root_is_never_asked_about_privileged_steps(self):
        # As non-root, none of the three warn/fail checks above should
        # trigger a prompt -- only an informational "needs root" note.
        # (The step-4 nginx prompt still fires -- it's unconditional --
        # so it needs an answer here, but that's not what this test is
        # about.)
        prompt = FakePrompts({})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("usermod"), [])
        self.assertEqual(prompt.asked_matching("Enable+start"), [])
        self.assertEqual(prompt.asked_matching("setsebool"), [])

    def test_root_is_asked_once_per_actionable_item(self):
        # group (warn) + my-booking.service (warn) + SELinux (fail) = 3
        # actionable items; the retention timer is already "ok" and isn't
        # asked about at all. nginx (step 4) is a 4th, unrelated prompt.
        prompt = FakePrompts({
            "usermod": True,
            "Enable+start": True,
            "setsebool": True,
        })
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(len(prompt.asked_matching("usermod")), 1)
        self.assertEqual(len(prompt.asked_matching("Enable+start")), 1)
        self.assertEqual(len(prompt.asked_matching("setsebool")), 1)
        self.assertTrue(any(c[0] == "usermod" for c in calls))
        self.assertTrue(any(c == ["systemctl", "enable", "--now", "my-booking.service"] for c in calls))
        self.assertTrue(any(c[0] == "setsebool" for c in calls))

    def test_declining_root_prompts_runs_nothing(self):
        prompt = FakePrompts({
            "usermod": False,
            "Enable+start": False,
            "setsebool": False,
        })
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(calls, [])


class InteractiveSetupStaticSiteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.static_dir = self.home / "live"
        self.static_dir.mkdir()

    def test_not_configured_is_never_prompted(self):
        prompt = FakePrompts({})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("Re)generate"), [])

    def test_accepting_regenerates_the_live_page(self):
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({"Re)generate": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        out = self.static_dir / site_render.OUTPUT_NAME
        self.assertTrue(out.exists())
        self.assertIn("MANAGED BY my-bt", out.read_text())

    def test_declining_leaves_it_unwritten(self):
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({"Re)generate": False})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertFalse((self.static_dir / site_render.OUTPUT_NAME).exists())

    def test_not_prompted_when_already_matching(self):
        # Regression coverage: unlike nginx's reload prompt (which stays
        # unconditional -- see feedback), regenerating privacy.html is a
        # pure re-render of the CURRENT settings.toml/template, so if it
        # already matches, asking again is pure noise -- must not prompt.
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        privacy = raw["privacy"]
        out = self.static_dir / site_render.OUTPUT_NAME
        site_render.write_privacy_html(
            self.home / "site" / "privacy.html.tmpl",
            privacy["retention_months"], privacy["canceled_retention_months"], out,
        )
        prompt = FakePrompts({}, default=True)  # would write again if (wrongly) asked+accepted
        before = out.read_text()
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("Re)generate"), [])
        self.assertEqual(out.read_text(), before)

    def test_accepting_opens_vimdiff_for_a_stale_hand_authored_page(self):
        # Regression coverage for 2026-07-05: an index.html footer edit sat
        # in the checkout for weeks, never deployed, and nothing offered to
        # fix it. Now `setup -i` actively offers to reconcile it -- via
        # vimdiff (not a blind copy) since BOTH sides have real content
        # here, and either could be the one worth keeping.
        (self.home / "site" / "index.html").write_text("new content with footer")
        (self.static_dir / "index.html").write_text("old stale content")
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({"vimdiff": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertTrue(any(
            c[0] == "vimdiff" and str(self.static_dir / "index.html") in c and str(self.home / "site" / "index.html") in c
            for c in calls
        ))
        # my-bt never writes the file itself here -- vimdiff (a real
        # editor session) is what would actually reconcile/save it.
        self.assertEqual((self.static_dir / "index.html").read_text(), "old stale content")

    def test_declining_vimdiff_leaves_the_stale_page_alone(self):
        (self.home / "site" / "index.html").write_text("new content")
        (self.static_dir / "index.html").write_text("old content")
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({"vimdiff": False})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(calls, [])
        self.assertEqual((self.static_dir / "index.html").read_text(), "old content")

    def test_accepting_copy_deploys_a_never_deployed_page(self):
        (self.home / "site" / "terms.html").write_text("terms content")
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({"Copy": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual((self.static_dir / "terms.html").read_text(), "terms content")

    def test_no_checkout_source_is_never_prompted_to_copy(self):
        # Nothing under site/ for impressum.html (no real file, no
        # .example) -- there's nothing to offer copying from.
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        prompt = FakePrompts({})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("impressum.html"), [])

    def test_symlink_offered_and_created_when_root_and_confirmed(self):
        nginx_root = self.home / "public_html"
        nginx_root.mkdir()
        (self.static_dir / "privacy.html").write_text("privacy content")
        raw = _raw(site={"static_site_dir": str(self.static_dir), "base_url": "https://example.org"})
        prompt = FakePrompts({"Symlink": True})
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(nginx_root)):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        link = nginx_root / "privacy.html"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.static_dir / "privacy.html").resolve())

    def test_symlink_not_offered_without_root(self):
        nginx_root = self.home / "public_html"
        nginx_root.mkdir()
        (self.static_dir / "privacy.html").write_text("privacy content")
        raw = _raw(site={"static_site_dir": str(self.static_dir), "base_url": "https://example.org"})
        prompt = FakePrompts({"Symlink": True})
        with patch("app.cli_checks._nginx_root_for_host", return_value=str(nginx_root)):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
            )
        self.assertEqual(prompt.asked_matching("Symlink"), [])
        self.assertFalse((nginx_root / "privacy.html").exists())


if __name__ == "__main__":
    unittest.main()
