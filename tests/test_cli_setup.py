"""Tests the `my-bt setup` / `my-bt setup --interactive` logic
(app/cli_setup.py) via dependency injection -- prompt/read_secret/run/
is_root are all fake callables here instead of a real tty/subprocess/
root, so the whole branching logic (what gets asked, what gets skipped,
what gets written) is exercised deterministically, the same way this was
manually smoke-tested via piped stdin during development."""
import stat
import subprocess
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

    def test_prints_all_twelve_numbered_steps(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        for n in range(1, 13):
            self.assertIn(f"{n}.", text)

    def test_caldav_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "caldav_url" in ln for ln in lines))

    def test_reports_missing_secret(self):
        lines: list[str] = []
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("caldav_password" in ln and "FAIL" in ln for ln in lines))

    def test_static_site_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "static_site_dir" in ln for ln in lines))

    def test_returned_counts_match_the_printed_report(self):
        # 2026-07-10, the operator: wants plain `my-bt setup` scriptable the same
        # way `status` already is -- print_report() now returns (fails,
        # warns) so scripts/my-bt's cmd_setup can decide the exit code.
        # Regardless of what this sandboxed test host's own systemd/
        # SELinux/rpm state happens to be, the returned counts must always
        # match what was actually printed.
        lines: list[str] = []
        fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        printed_fails = sum(1 for ln in lines if "[FAIL]" in ln)
        printed_warns = sum(1 for ln in lines if "[WARN]" in ln)
        self.assertEqual(fails, printed_fails)
        self.assertEqual(warns, printed_warns)

    def test_a_real_failure_is_reflected_in_the_returned_count_and_summary_line(self):
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        lines: list[str] = []
        fails, warns = cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertGreaterEqual(fails, 1)
        self.assertTrue(any(f"{fails} problem(s)" in ln for ln in lines))

    def test_all_checks_passed_summary_when_nothing_to_report(self):
        # Force a fully clean report by taking the REAL report's shape
        # (every group key print_report() expects) and flattening every
        # check to "ok" -- avoids depending on this sandboxed test host's
        # own systemd/SELinux/group state actually being clean, which
        # isn't something this test can control or should assume.
        real_report = cli_setup.build_report(_raw(), self.settings_path, str(self.home))
        all_ok_report = {
            key: [(label, "ok", detail) for label, _, detail in checks]
            for key, checks in real_report.items()
        }
        with patch.object(cli_setup, "build_report", return_value=all_ok_report):
            lines: list[str] = []
            fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertEqual((fails, warns), (0, 0))
        self.assertTrue(any("all checks passed" in ln for ln in lines))


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
            patch("app.cli_checks.check_settings_fresh",
                  return_value=[("my-booking.service freshness", "warn",
                                  "settings.toml was edited after my-booking.service last (re)started -- "
                                  "those edits aren't live yet: sudo systemctl restart my-booking.service")]),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_non_root_is_never_asked_about_privileged_steps(self):
        # As non-root, none of the four warn/fail checks above should
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
        self.assertEqual(prompt.asked_matching("Restart my-booking.service"), [])

    def test_root_is_asked_once_per_actionable_item(self):
        # group (warn) + my-booking.service enable (warn) + SELinux (fail)
        # + settings.toml freshness (warn) = 4 actionable items; the
        # retention timer is already "ok" and isn't asked about at all.
        # nginx (step 4) is a 5th, unrelated prompt.
        prompt = FakePrompts({
            "usermod": True,
            "Enable+start": True,
            "setsebool": True,
            "Restart my-booking.service": True,
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
        self.assertEqual(len(prompt.asked_matching("Restart my-booking.service")), 1)
        self.assertTrue(any(c[0] == "usermod" for c in calls))
        self.assertTrue(any(c == ["systemctl", "enable", "--now", "my-booking.service"] for c in calls))
        self.assertTrue(any(c[0] == "setsebool" for c in calls))
        self.assertTrue(any(c == ["systemctl", "restart", "my-booking.service"] for c in calls))

    def test_declining_root_prompts_runs_nothing(self):
        prompt = FakePrompts({
            "usermod": False,
            "Enable+start": False,
            "setsebool": False,
            "Restart my-booking.service": False,
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


class InteractiveSetupNginxConfDeployedTest(unittest.TestCase):
    """[site].nginx_conf_path: read directly off disk (never via `nginx -T`
    or a checkout glob), and -- unlike static_site_dir's own vimdiff offer
    above -- never auto-writable, just an offer to reconcile against this
    checkout's own FIXED site/nginx-locations.conf(.example) if the two
    differ. The deployed file below is deliberately kept named
    "booking.example.org.conf" (not "nginx-locations.conf") throughout this class,
    to prove the checkout-side match no longer depends on nginx_conf_path's
    own basename at all (2026-07-10 rename)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.deployed = self.home / "deployed" / "booking.example.org.conf"
        self.deployed.parent.mkdir()

    def test_not_configured_is_never_prompted(self):
        prompt = FakePrompts({})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("vimdiff"), [])

    def test_no_checkout_source_is_never_prompted(self):
        self.deployed.write_text("location /admin { }")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("vimdiff"), [])

    def test_matching_checkout_copy_is_never_prompted(self):
        text = "location /admin { }"
        self.deployed.write_text(text)
        (self.home / "site" / "nginx-locations.conf").write_text(text)
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("vimdiff"), [])

    def test_accepting_opens_vimdiff_for_a_differing_copy(self):
        self.deployed.write_text("location /admin { } # deployed")
        (self.home / "site" / "nginx-locations.conf").write_text("location /admin { } # checkout")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertTrue(any(
            c[0] == "vimdiff" and str(self.deployed) in c and str(self.home / "site" / "nginx-locations.conf") in c
            for c in calls
        ))
        # Never auto-written -- vimdiff is what would actually reconcile it.
        self.assertEqual(self.deployed.read_text(), "location /admin { } # deployed")

    def test_declining_vimdiff_leaves_both_files_alone(self):
        self.deployed.write_text("deployed version")
        (self.home / "site" / "nginx-locations.conf").write_text("checkout version")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": False})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(calls, [])

    def test_falls_back_to_example_when_no_real_checkout_copy(self):
        self.deployed.write_text("deployed version")
        (self.home / "site" / "nginx-locations.conf.example").write_text("generic template")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertTrue(any(
            c[0] == "vimdiff" and str(self.home / "site" / "nginx-locations.conf.example") in c
            for c in calls
        ))


class InteractiveSetupCaldavTest(unittest.TestCase):
    """Step 9 is informational only -- there's no safe auto-fix for a
    calendar-name mismatch, so unlike every other step here it never
    calls `prompt` at all, just surfaces check_caldav_calendars()'s
    findings (mocked wholesale -- its own logic is covered by
    tests/test_cli_checks.py::CheckCaldavCalendarsTest)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def _run(self, raw):
        lines: list[str] = []
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lines.append,
        )
        return lines, prompt

    def test_not_configured_shows_skip_and_never_prompts(self):
        lines, prompt = self._run(_raw())
        self.assertTrue(any("skip" in ln and "caldav_url" in ln for ln in lines))
        self.assertEqual(prompt.asked_matching("CalDAV"), [])

    @patch("app.cli_checks.CalDAVClient")
    def test_calendar_not_found_reports_fail_and_fix_hint(self, mock_cls):
        mock_cls.return_value.list_calendars.return_value = {"WebDAV Root": "/"}
        raw = _raw(calendar={
            "caldav_url": "https://dav.mailbox.org/caldav/",
            "caldav_username": "calendar@example.org",
            "caldav_password_file": None,
            "booking_calendar": "Yoga-Bookings",
            "conflict_calendars": ["Calendar"],
        })
        # password file check happens first in check_caldav_calendars --
        # give it a real (empty-ok) file so the CalDAVClient mock is reached.
        pw = self.home / "caldav_password"
        pw.write_text("hunter2")
        raw["calendar"]["caldav_password_file"] = str(pw)
        lines, prompt = self._run(raw)
        text = "\n".join(lines)
        self.assertIn("fail", text)
        self.assertIn("WebDAV Root", text)
        self.assertIn("settings.toml", text)
        self.assertEqual(prompt.asked_matching("CalDAV"), [])


class InteractiveSetupWatchdogTest(unittest.TestCase):
    """Step 10 -- unlike step 9 (CalDAV), there IS a safe auto-fix here
    (setfacl), so this DOES prompt/act, mirroring the SELinux/group-
    membership pattern. check_watchdog_nginx_access's own permission-bit
    logic is covered by tests/test_cli_checks.py::CheckWatchdogNginxAccessTest
    -- mocked wholesale here, same as the CalDAV test above."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.log_path = self.home / "access.log"
        self.log_path.write_text("test\n")

    def _raw_with_log(self):
        return _raw(watchdog={"nginx_access_log": str(self.log_path)})

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    def test_not_configured_shows_skip_and_never_prompts(self, _mock):
        lines: list[str] = []
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lines.append,
        )
        self.assertTrue(any("skip" in ln and "nginx_access_log" in ln for ln in lines))
        self.assertEqual(prompt.asked_matching("setfacl"), [])

    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log (x)", "ok", "my-booking can read it")])
    def test_already_ok_never_prompts(self, _mock):
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            self._raw_with_log(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("setfacl"), [])

    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log (x)", "warn", "my-booking can't read this yet -- sudo setfacl ...")])
    def test_non_root_shows_needs_root_and_never_prompts(self, _mock):
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            self._raw_with_log(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("setfacl"), [])

    # shutil.which is patched selectively (not a blanket return_value) --
    # `shutil` is one shared module object, so an unconditional patch here
    # would also make cli_checks.check_nginx_locations()'s own
    # shutil.which("nginx") call (step 4, always run) return truthy and
    # then actually try to subprocess.run(["nginx", "-T"]) for real.
    @patch("app.cli_setup.shutil.which", side_effect=lambda name: "/usr/sbin/setfacl" if name == "setfacl" else None)
    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log (x)", "warn", "my-booking can't read this yet -- sudo setfacl ...")])
    def test_root_accepting_prompt_runs_both_setfacl_commands(self, _mock, _which):
        prompt = FakePrompts({"setfacl": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            self._raw_with_log(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(len(prompt.asked_matching("setfacl")), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[0] == "setfacl" for c in calls))
        self.assertTrue(any("-d" in c for c in calls))

    @patch("app.cli_setup.shutil.which", return_value=None)
    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log (x)", "warn", "my-booking can't read this yet -- sudo setfacl ...")])
    def test_setfacl_missing_shows_install_hint_and_never_prompts(self, _mock, _which):
        prompt = FakePrompts({"setfacl": True})
        lines: list[str] = []
        cli_setup.interactive_setup(
            self._raw_with_log(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
            print_fn=lines.append,
        )
        self.assertEqual(prompt.asked_matching("setfacl"), [])
        self.assertTrue(any("dnf install acl" in ln for ln in lines))

    @patch("app.cli_setup.shutil.which", side_effect=lambda name: "/usr/sbin/setfacl" if name == "setfacl" else None)
    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log (x)", "warn", "my-booking can't read this yet -- sudo setfacl ...")])
    def test_declining_prompt_runs_nothing(self, _mock, _which):
        prompt = FakePrompts({"setfacl": False})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            self._raw_with_log(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(calls, [])

    @patch("app.cli_checks.check_watchdog_nginx_access",
           return_value=[("watchdog: nginx_access_log access", "warn", "user 'my-booking' doesn't exist yet -- install the package first")])
    def test_missing_my_booking_user_never_prompts_even_as_root(self, _mock):
        prompt = FakePrompts({"setfacl": True})
        cli_setup.interactive_setup(
            _raw(watchdog={"nginx_access_log": str(self.log_path)}), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("setfacl"), [])

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    @patch("app.cli_checks._nginx_access_log_for_host", return_value=None)
    def test_nothing_detected_never_offers_to_write(self, _detect, _access):
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("Add nginx_access_log"), [])

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    def test_detected_but_unconfigured_offers_to_write_and_accepting_writes_it(self, _access):
        with patch("app.cli_checks._nginx_access_log_for_host", return_value=str(self.log_path)):
            prompt = FakePrompts({"Add nginx_access_log": True})
            raw = _raw()
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertEqual(len(prompt.asked_matching("Add nginx_access_log")), 1)
        written = Path(self.settings_path).read_text()
        self.assertIn(f'nginx_access_log = "{self.log_path}"', written)
        # in-memory raw is updated too, so the readability check in the
        # same run sees it immediately rather than requiring a re-run.
        self.assertEqual(raw["watchdog"]["nginx_access_log"], str(self.log_path))

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    def test_detected_but_unconfigured_declining_writes_nothing(self, _access):
        with patch("app.cli_checks._nginx_access_log_for_host", return_value=str(self.log_path)):
            prompt = FakePrompts({"Add nginx_access_log": False})
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertNotIn("nginx_access_log", Path(self.settings_path).read_text())

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    def test_configured_value_differing_from_detected_warns_without_prompting(self, _access):
        with patch("app.cli_checks._nginx_access_log_for_host", return_value=str(self.log_path)):
            prompt = FakePrompts()
            lines: list[str] = []
            cli_setup.interactive_setup(
                _raw(watchdog={"nginx_access_log": "/some/stale/path.log"}),
                self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lines.append,
            )
        self.assertEqual(prompt.asked_matching("Add nginx_access_log"), [])
        self.assertTrue(any("stale" in ln for ln in lines))


class InteractiveSetupDataDirGitTest(unittest.TestCase):
    """Step 11 -- unlike the systemd/SELinux/group-membership offers, this
    one is never gated behind is_root() (see interactive_setup's own
    comment on why: it only needs filesystem write access to the data
    dir). `git init`/`config` are run directly via real subprocess calls
    (not through the injected `run` fake) so the repo genuinely exists
    afterward for app.git_snapshot.snapshot() to find -- these tests use a
    real temp directory and real `git`, the most convincing way to verify
    this end-to-end (per the project's own testing conventions)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "checkout"
        (self.home / "site").mkdir(parents=True)
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")

    def test_declining_leaves_no_git_repo(self):
        prompt = FakePrompts({"Initialize a git repo": False})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None, data_dir=str(self.data_dir),
        )
        self.assertFalse((self.data_dir / ".git").exists())

    def test_accepting_creates_repo_with_initial_commit(self):
        prompt = FakePrompts({"Initialize a git repo": True})
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None, data_dir=str(self.data_dir),
        )
        self.assertTrue((self.data_dir / ".git").is_dir())
        self.assertTrue((self.data_dir / ".gitignore").exists())
        self.assertIn("*.tmp", (self.data_dir / ".gitignore").read_text())
        log = subprocess.run(
            ["git", "-C", str(self.data_dir), "log", "--oneline"],
            capture_output=True, text=True,
        )
        self.assertEqual(len(log.stdout.strip().splitlines()), 1)

    def test_not_gated_behind_root(self):
        # Even as non-root, accepting the prompt actually initializes the
        # repo -- unlike the systemd/SELinux/group steps above, this
        # doesn't require root and must never print a "needs root" note
        # or silently skip.
        prompt = FakePrompts({"Initialize a git repo": True})
        lines: list[str] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lines.append, data_dir=str(self.data_dir),
        )
        self.assertTrue((self.data_dir / ".git").exists())
        self.assertFalse(any("needs root" in ln and "git repo" in ln for ln in lines))

    def test_already_a_repo_is_never_prompted(self):
        subprocess.run(["git", "init", str(self.data_dir)], capture_output=True, text=True)
        prompt = FakePrompts({}, default=True)  # would init again if (wrongly) asked+accepted
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None, data_dir=str(self.data_dir),
        )
        self.assertEqual(prompt.asked_matching("Initialize a git repo"), [])


class InteractiveSetupFinalSummaryTest(unittest.TestCase):
    """The closing "Done." line (2026-07-08, the operator: "would be better if
    'Done' would reflect if there were any problems.") -- previously a flat
    string printed unconditionally, identical whether the walkthrough above
    just fixed everything or three warnings are still sitting there.
    build_report() is patched directly here rather than relying on this
    sandbox's real systemd/nginx/SELinux state (which varies by environment
    and would make an "all clear" assertion flaky) -- all that needs
    verifying is that interactive_setup() tallies whatever build_report()
    returns (re-run fresh, so it reflects fixes just applied above, not
    stale pre-walkthrough state) into the final line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def _run(self, report: dict) -> str:
        lines: list[str] = []
        with patch.object(cli_setup, "build_report", return_value=report):
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=FakePrompts(), run=lambda cmd: None, is_root=lambda: False,
                print_fn=lines.append,
            )
        return "\n".join(lines)

    def test_all_clear_says_all_checks_pass(self):
        text = self._run({"group": [("g", "ok", "fine")]})
        self.assertIn("Done -- all checks pass now", text)

    def test_remaining_problems_are_counted_in_the_final_line(self):
        text = self._run({
            "secrets": [("secret: x", "fail", "missing")],
            "group": [("g", "warn", "not in group")],
        })
        self.assertIn("Done -- 1 problem(s), 1 warning(s) still need attention", text)

    def test_warnings_only_no_fails_still_flagged(self):
        text = self._run({"group": [("g", "warn", "not in group")]})
        self.assertIn("Done -- 0 problem(s), 1 warning(s) still need attention", text)


class AddNginxAccessLogSettingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings_path = str(Path(self._tmp.name) / "settings.toml")

    def test_creates_watchdog_section_when_absent(self):
        Path(self.settings_path).write_text("[site]\nadmin_email = \"a@b.com\"\n")
        cli_setup._add_nginx_access_log_setting(self.settings_path, "/var/log/nginx/access.log")
        text = Path(self.settings_path).read_text()
        self.assertIn("[watchdog]", text)
        self.assertIn('nginx_access_log = "/var/log/nginx/access.log"', text)
        # the original section is untouched
        self.assertIn('admin_email = "a@b.com"', text)

    def test_inserts_into_existing_watchdog_section_preserving_other_lines(self):
        Path(self.settings_path).write_text(
            "[watchdog]\nenabled = true\nwindow_minutes = 15\n\n[[course]]\nshortname = \"x\"\n"
        )
        cli_setup._add_nginx_access_log_setting(self.settings_path, "/var/log/nginx/access.log")
        text = Path(self.settings_path).read_text()
        self.assertIn('nginx_access_log = "/var/log/nginx/access.log"', text)
        self.assertIn("enabled = true", text)
        self.assertIn("window_minutes = 15", text)
        self.assertIn('shortname = "x"', text)
        # inserted right after the [watchdog] header, before its other keys
        watchdog_idx = text.index("[watchdog]")
        new_line_idx = text.index("nginx_access_log")
        enabled_idx = text.index("enabled = true")
        self.assertTrue(watchdog_idx < new_line_idx < enabled_idx)


if __name__ == "__main__":
    unittest.main()
