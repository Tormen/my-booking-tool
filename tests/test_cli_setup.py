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

from app import cli_checks, cli_setup, maintenance, site_render

# 2026-07-16, real failure on the RPM build host (vps-b59d01b3's own
# %check, i.e. the actual production machine): interactive_setup()'s
# check_active_sessions defaults to a REAL HTTP GET against
# http://127.0.0.1:8811/internal/status (see _default_check_active_
# sessions) -- fine in a throwaway CI sandbox where nothing's listening
# on that port, but on THIS host the real my-booking.service IS actually
# up, so the ~50 tests below that call interactive_setup() without
# mocking check_settings_fresh were unknowingly hitting the real,
# running production service on every single run (confirmed via
# journalctl: a burst of real "GET /internal/status" hits exactly when
# `rpmbuild %check` ran). Only one test's assertion happened to be
# sensitive enough to actually fail from this (it requires zero active
# sessions to see the restart prompt fire) -- but ANY test here was one
# `sudo my-bt admin logout` away from silently changing behavior based on
# real, live production session state, which is a much bigger problem
# than the one visible failure. check_active_sessions() is only ever
# reached when check_settings_fresh() reports "aren't live yet" (see
# interactive_setup's own settings-freshness step), so patching THAT one
# function to always report "ok" by default -- for the whole module --
# is enough to make every test hermetic without touching each of the ~50
# individual call sites. The few tests that deliberately want the "aren't
# live yet" scenario (InteractiveSetupRestartSessionGuardTest) already
# apply their OWN nested patch of check_settings_fresh in their own
# setUp(), which correctly overrides this module-level default for the
# duration of those tests (mock.patch stacks: an inner patch's stop()
# restores whatever was active before it started, i.e. this default) and
# reverts back to it afterward.
# 2026-07-13: same real-live-service problem as above, twice more --
# interactive_setup() now ALSO does an unconditional check_active_sessions
# call right at its very top (the new upfront hard-refusal gate, mirroring
# the RPM's own %pre gate -- see interactive_setup's own docstring), and
# build_report() (which both print_report() and interactive_setup()'s own
# closing tally call) now includes cli_checks.check_active_sessions() as
# one more check group. Both are REAL HTTP GETs against
# http://127.0.0.1:8811/internal/status by default, unconditionally, on
# every single call -- not just the one settings-freshness-gated call site
# the comment above was originally about. Patched hermetically here too,
# for the same reason: on a host where my-booking.service is actually
# running, every one of the ~50+ tests below that don't care about
# sessions would otherwise depend on real, live production session state.
# The handful of tests that DO want an interesting session scenario
# (InteractiveSetupActiveSessionGateTest, InteractiveSetupRestartSessionGuardTest)
# apply their own nested patch, same stacking behavior as above.
_module_patches: list = []


def setUpModule():
    patches = [
        patch(
            "app.cli_checks.check_settings_fresh",
            return_value=[("my-booking.service freshness", "ok", "settings.toml unchanged since last (re)start")],
        ),
        patch("app.cli_checks.fetch_active_sessions", return_value=(None, None)),
        patch("app.cli_checks.check_active_sessions", return_value=[]),
    ]
    for p in patches:
        p.start()
        _module_patches.append(p)


def tearDownModule():
    for p in _module_patches:
        p.stop()
    _module_patches.clear()


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


class DefaultCheckActiveSessionsTest(unittest.TestCase):
    """cli_setup._default_check_active_sessions -- the real GET-/internal/
    status-and-count-sessions implementation interactive_setup's restart
    guard defaults to. Mocks urllib directly (no real HTTP call) rather
    than spinning up a live server for this small a check."""

    def test_returns_the_session_count_from_the_payload(self):
        import io
        import json as _json

        fake_resp = io.BytesIO(_json.dumps({"sessions": [{}, {}]}).encode("utf-8"))
        with patch("app.cli_setup.urllib.request.urlopen") as m_urlopen:
            m_urlopen.return_value.__enter__.return_value = fake_resp
            count, error = cli_setup._default_check_active_sessions("http://127.0.0.1:8811")
        self.assertEqual(count, 2)
        self.assertIsNone(error)

    def test_connection_failure_returns_error_not_a_raise(self):
        with patch("app.cli_setup.urllib.request.urlopen", side_effect=OSError("Connection refused")):
            count, error = cli_setup._default_check_active_sessions("http://127.0.0.1:8811")
        self.assertIsNone(count)
        self.assertIn("Connection refused", error)


class PrintReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def test_prints_all_fifteen_numbered_steps(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        for n in range(1, 16):
            self.assertIn(f"{n}.", text)

    def test_caldav_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "caldav_url" in ln for ln in lines))

    def test_stale_calendar_invite_format_marker_is_a_warn_that_fails_the_report(self):
        # 2026-07-15: from a real `setup -i` run, a stale marker was
        # printed as a raw "[warn] ..." line that never became a structured
        # Check, so it didn't count towards fails/warns and didn't stop the
        # closing line from claiming "all checks pass now" -- see
        # app/cli_checks.check_calendar_invite_format()'s own docstring. A
        # stale/mismatched marker must now show up as a [WARN] line here AND
        # bump the returned warns count, same as any other check.
        data_dir = Path(self._tmp.name) / "data"
        data_dir.mkdir()
        (data_dir / ".calendar_invite_format_version").write_text("0\n")
        password_file = self.home / "caldav_password"
        password_file.write_text("secret")
        raw = _raw(calendar={
            "caldav_url": "https://caldav.example.org/",
            "caldav_username": "bot",
            "caldav_password_file": str(password_file),
        })
        lines: list[str] = []
        fails, warns = cli_setup.print_report(
            raw, self.settings_path, str(self.home), print_fn=lines.append, data_dir=str(data_dir),
        )
        self.assertTrue(any("WARN" in ln and "calendar invite format" in ln for ln in lines))
        self.assertGreaterEqual(warns, 1)

    def test_reports_missing_secret(self):
        lines: list[str] = []
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("caldav_password" in ln and "FAIL" in ln for ln in lines))

    def test_static_site_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "static_site_dir" in ln for ln in lines))

    def test_log_file_not_configured_shows_skip(self):
        # 2026-07-16: [logging].log_file's group/SELinux check (new)
        # -- unlike the other data paths, print_report had no section
        # for log_file at all before this; must degrade to a plain SKIP
        # line, not silently vanish, when it isn't configured.
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("SKIP" in ln and "log_file" in ln for ln in lines))

    def test_csp_violations_not_configured_shows_skip(self):
        lines: list[str] = []
        cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        self.assertIn("CSP violations reported by browsers", text)
        self.assertIn("[SKIP] [logging].log_file not configured -- not checked", text)

    def test_csp_violations_configured_and_clean_shows_ok(self):
        raw = _raw(logging={"log_file": str(self.home / "my-booking.log")})
        with patch("app.cli_checks.check_csp_violations", return_value=[]):
            lines: list[str] = []
            cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("OK" in ln and "none in the last" in ln for ln in lines))

    def test_csp_violations_found_are_shown_and_counted(self):
        raw = _raw(logging={"log_file": str(self.home / "my-booking.log")})
        with patch("app.cli_checks.check_csp_violations", return_value=[(
            "CSP violations", "warn", "3 CSP violation report(s) in the last 15 min: 3x blocked-uri='eval' ...",
        )]):
            lines: list[str] = []
            fails, warns = cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("WARN" in ln and "CSP violations" in ln for ln in lines))
        self.assertGreaterEqual(warns, 1)

    def test_csp_hashes_not_detected_shows_skip(self):
        with patch("app.cli_checks.check_csp_hashes_deployed", return_value=[]):
            lines: list[str] = []
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        self.assertIn("CSP script hashes deployed", text)
        self.assertIn("[SKIP] nginx/the matching vhost/its CSP header not detected -- not checked", text)

    def test_csp_hashes_all_present_shows_ok(self):
        with patch("app.cli_checks.check_csp_hashes_deployed", return_value=[(
            "CSP script hashes deployed", "ok", "all 10 expected inline <script> hash(es) present",
        )]):
            lines: list[str] = []
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("OK" in ln and "CSP script hashes deployed" in ln for ln in lines))

    def test_csp_hashes_missing_is_shown_and_counted(self):
        with patch("app.cli_checks.check_csp_hashes_deployed", return_value=[(
            "CSP script hashes deployed", "warn", "1 of 10 inline <script> hash(es) missing -- "
            "webapp._SORTABLE_FILTERABLE_TABLE_SCRIPT needs 'sha256-AAA=' added",
        )]):
            lines: list[str] = []
            fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("WARN" in ln and "CSP script hashes deployed" in ln for ln in lines))
        self.assertGreaterEqual(warns, 1)

    def test_static_site_dir_group_selinux_findings_are_shown(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("static_site_dir group", "warn", "wrong group -- sudo chgrp -R my-booking X"),
        ]):
            lines: list[str] = []
            raw = _raw(site={"static_site_dir": str(self.home / "public_html")})
            cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("WARN" in ln and "static_site_dir group" in ln for ln in lines))

    def test_data_dir_group_selinux_findings_are_shown_and_counted(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir SELinux context", "warn", "mismatch -- sudo restorecon -Rv X"),
        ]):
            lines: list[str] = []
            fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("WARN" in ln and "data dir SELinux context" in ln for ln in lines))
        self.assertGreaterEqual(warns, 1)

    def test_no_active_sessions_shows_nothing_extra(self):
        with patch("app.cli_checks.fetch_active_sessions", return_value=({"sessions": []}, None)):
            lines: list[str] = []
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertFalse(any("active session" in ln for ln in lines))

    def test_active_sessions_warn_and_full_overview_are_shown_and_counted(self):
        # 2026-07-13: plain `my-bt setup` (no -i, never touches the live
        # process for anything else) now also surfaces this -- a WARN
        # line for the tally/exit-code, plus the full name/email/session
        # start/last activity/timeout breakdown right underneath it.
        payload = {
            "sessions": [{
                "kind": "guest", "who": "ines@example.org", "name": "Guest One",
                "connected_since": "2026-07-13T06:49:00+00:00", "last_seen": "2026-07-13T07:37:00+00:00",
            }],
            "session_timeout_seconds": 14400,
        }
        # setUpModule's own hermetic default patches check_active_sessions
        # wholesale (to keep the ~50 unrelated tests in this module from
        # hitting a real live service) -- override BOTH it and the lower-
        # level fetch_active_sessions it and print_report() each call, so
        # this one test sees the interesting scenario end to end.
        with patch("app.cli_checks.fetch_active_sessions", return_value=(payload, None)), \
             patch("app.cli_checks.check_active_sessions", return_value=[(
                 "active sessions", "warn", "1 active session(s) right now (ines@example.org)",
             )]):
            lines: list[str] = []
            fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        self.assertTrue(any("WARN" in ln and "active sessions" in ln for ln in lines))
        self.assertIn("Guest One", text)
        self.assertIn("ines@example.org", text)
        self.assertIn("my-bt admin logout", text)
        self.assertGreaterEqual(warns, 1)

    def test_returned_counts_match_the_printed_report(self):
        # 2026-07-10: plain `my-bt setup` should be scriptable the same
        # way `status` already is -- print_report() now returns (fails,
        # warns) so scripts/my-bt's cmd_setup can decide the exit code.
        # Regardless of what this sandboxed test host's own systemd/
        # SELinux/rpm state happens to be, the returned counts must always
        # match what was actually printed.
        #
        # 2026-07-08: only counts the ORIGINAL per-section printout, not
        # the "Warnings/failures, repeated from above" block added the
        # same day -- that block deliberately reprints each [WARN]/[FAIL]
        # line a second time, so counting the whole output would double
        # every count and always fail regardless of correctness.
        lines: list[str] = []
        fails, warns = cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        original_lines = "\n".join(lines).split("Warnings/failures, repeated from above:")[0].splitlines()
        printed_fails = sum(1 for ln in original_lines if "[FAIL]" in ln)
        printed_warns = sum(1 for ln in original_lines if "[WARN]" in ln)
        self.assertEqual(fails, printed_fails)
        self.assertEqual(warns, printed_warns)

    def test_a_real_failure_is_reflected_in_the_returned_count_and_summary_line(self):
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        lines: list[str] = []
        fails, warns = cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        self.assertGreaterEqual(fails, 1)
        self.assertTrue(any(f"{fails} problem(s)" in ln for ln in lines))

    def test_repeats_every_warning_and_failure_at_the_end(self):
        # 2026-07-08: all warnings are repeated at the end of
        # setup and status explicitly -- a real FAIL (missing secret)
        # must reappear, verbatim, in a repeated block after all twelve
        # numbered steps, not just once wherever it first printed.
        raw = _raw(calendar={"caldav_password_file": str(self.home / "nope")})
        lines: list[str] = []
        cli_setup.print_report(raw, self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        repeated_section = text.split("Warnings/failures, repeated from above:")[1]
        self.assertIn("caldav_password", repeated_section)
        self.assertIn("FAIL", repeated_section)

    def test_no_repeated_section_when_report_is_clean(self):
        real_report = cli_setup.build_report(_raw(), self.settings_path, str(self.home))
        all_ok_report = {
            key: [(label, "ok", detail) for label, _, detail in checks]
            for key, checks in real_report.items()
        }
        with patch.object(cli_setup, "build_report", return_value=all_ok_report):
            lines: list[str] = []
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertFalse(any("repeated from above" in ln for ln in lines))

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


class NginxLocationsHintTest(unittest.TestCase):
    """When `nginx -T` is missing a required location block, the hint
    for where to copy it from should point at THIS checkout's own real,
    already-complete site/nginx-locations.conf when one exists, instead
    of the bare generic packaged example -- 2026-07-10, after
    the generic hint was seen firing while a complete file sat unused
    in site/, print_report() was updated to prepare the correct
    nginx-locations.conf directly instead. Both print_report() and
    interactive_setup() share this logic, so both are covered here."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def _missing_location_checks(self):
        return [("nginx location /admin", "warn", "missing from the live config")]

    def test_print_report_points_at_repo_file_when_complete(self):
        (self.home / "site" / "nginx-locations.conf").write_text("complete, hardened")
        lines: list[str] = []
        with patch("app.cli_checks.check_nginx_locations", return_value=self._missing_location_checks()), \
             patch("app.cli_checks.check_nginx_conf_repo_file",
                   return_value=[("nginx vhost conf (site/nginx-locations.conf)", "ok", "complete")]):
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        text = "\n".join(lines)
        self.assertIn("this checkout's own", text)
        self.assertNotIn("/opt/my-booking/site/my-booking.conf.example", text)

    def test_print_report_falls_back_to_generic_example_when_no_repo_file(self):
        lines: list[str] = []
        with patch("app.cli_checks.check_nginx_locations", return_value=self._missing_location_checks()), \
             patch("app.cli_checks.check_nginx_conf_repo_file",
                   return_value=[("nginx vhost conf (site/nginx-locations.conf)", "warn", "none found yet")]):
            cli_setup.print_report(_raw(), self.settings_path, str(self.home), print_fn=lines.append)
        self.assertTrue(any("/opt/my-booking/site/my-booking.conf.example" in ln for ln in lines))

    def test_interactive_setup_points_at_repo_file_when_complete(self):
        lines: list[str] = []
        prompt = FakePrompts({}, default=False)
        with patch("app.cli_checks.check_nginx_locations", return_value=self._missing_location_checks()), \
             patch("app.cli_checks.check_nginx_conf_repo_file",
                   return_value=[("nginx vhost conf (site/nginx-locations.conf)", "ok", "complete")]):
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lines.append,
            )
        text = "\n".join(lines)
        self.assertIn("this checkout's own", text)
        self.assertNotIn("/opt/my-booking/site/my-booking.conf.example", text)

    def test_interactive_setup_falls_back_to_generic_example_when_no_repo_file(self):
        lines: list[str] = []
        prompt = FakePrompts({}, default=False)
        with patch("app.cli_checks.check_nginx_locations", return_value=self._missing_location_checks()), \
             patch("app.cli_checks.check_nginx_conf_repo_file",
                   return_value=[("nginx vhost conf (site/nginx-locations.conf)", "warn", "none found yet")]):
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lines.append,
            )
        self.assertTrue(any("/opt/my-booking/site/my-booking.conf.example" in ln for ln in lines))


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

    def test_accepting_a_missing_secret_leaves_no_temp_file_behind(self):
        # 2026-07-15: secret files are written via atomic_io.
        # atomic_write_text (temp file + fsync + rename), not a bare
        # write_text() -- confirm no temp file lingers next to it.
        path = self.secrets_dir / "caldav_password"
        raw = _raw(calendar={"caldav_password_file": str(path)})
        self._run(raw, answers={"caldav_password": True}, read_secret=lambda label: "hunter2")
        leftover_tmps = [p.name for p in self.secrets_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftover_tmps, [])

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
                  return_value=[("my-booking group membership (alice)", "warn", "not a member")]),
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
            # This class's setUp() deliberately makes check_settings_fresh
            # report "aren't live yet" (the restart-prompt scenario), which
            # means interactive_setup() actually reaches the
            # check_active_sessions() call below it -- left at its real
            # default, that's a live HTTP GET to
            # 127.0.0.1:8811/internal/status, so on a host where
            # my-booking.service is genuinely running (e.g. the RPM
            # build/install host's own %check) this test's outcome depended
            # on real, live production session state instead of the fixed
            # scenario it's meant to test. Inject a fake with zero sessions
            # so the restart prompt is reached deterministically.
            check_active_sessions=lambda: (0, None),
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
            check_active_sessions=lambda: (0, None),  # see note above
        )
        self.assertEqual(calls, [])


class InteractiveSetupPathGroupSelinuxTest(unittest.TestCase):
    """Step 11d -- cli_checks.check_path_group_and_selinux's group/
    SELinux findings for data_dir (always audited) and
    [logging].log_file / [site].static_site_dir (only when configured --
    the base `_raw()` fixture has neither, so only the data_dir call
    fires in these tests). Mocked here, not real grp/subprocess calls,
    same determinism reasoning as InteractiveSetupPrivilegedStepsTest
    above. 2026-07-16: group+permissions+SELinux are audited for
    ALL data paths -- this is the auto-heal half (chgrp/restorecon),
    gated on is_root() the same way the pre-existing data-dir-ownership
    chown step is."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()

    def _run(self, prompt, calls, is_root):
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=is_root,
            print_fn=lambda *_: None, data_dir=str(self.data_dir),
        )

    def test_group_mismatch_offers_chgrp_when_root(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir group", "warn", "wrong group -- sudo chgrp -R my-booking X"),
        ]):
            prompt = FakePrompts({"chgrp": True})
            calls: list[list[str]] = []
            self._run(prompt, calls, is_root=lambda: True)
        self.assertEqual(len(prompt.asked_matching("chgrp")), 1)
        self.assertTrue(any(c[0] == "chgrp" for c in calls))

    def test_group_mismatch_as_non_root_is_not_prompted_or_run(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir group", "warn", "wrong group -- sudo chgrp -R my-booking X"),
        ]):
            prompt = FakePrompts({})
            calls: list[list[str]] = []
            self._run(prompt, calls, is_root=lambda: False)
        self.assertEqual(prompt.asked_matching("chgrp"), [])
        self.assertEqual(calls, [])

    def test_selinux_mismatch_offers_restorecon_when_root(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir SELinux context", "warn", "mismatch -- sudo restorecon -Rv X"),
        ]):
            prompt = FakePrompts({"restorecon": True})
            calls: list[list[str]] = []
            self._run(prompt, calls, is_root=lambda: True)
        self.assertEqual(len(prompt.asked_matching("restorecon")), 1)
        self.assertTrue(any(c[0] == "restorecon" for c in calls))

    def test_declining_the_prompt_runs_nothing(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir group", "warn", "wrong group -- sudo chgrp -R my-booking X"),
        ]):
            prompt = FakePrompts({"chgrp": False})
            calls: list[list[str]] = []
            self._run(prompt, calls, is_root=lambda: True)
        self.assertEqual(calls, [])

    def test_ok_result_is_never_prompted(self):
        with patch("app.cli_checks.check_path_group_and_selinux", return_value=[
            ("data dir group", "ok", "group is 'my-booking'"),
        ]):
            prompt = FakePrompts({})
            calls: list[list[str]] = []
            self._run(prompt, calls, is_root=lambda: True)
        self.assertEqual(prompt.asked_matching("chgrp"), [])
        self.assertEqual(calls, [])


def _sessions_payload(*sessions, timeout=14400):
    return {"sessions": list(sessions), "session_timeout_seconds": timeout}


class InteractiveSetupActiveSessionGateTest(unittest.TestCase):
    """2026-07-13: interactive_setup() now refuses the WHOLE walkthrough
    outright while anyone's actively logged in, mirroring the RPM's own
    %pre gate before an upgrade (packaging/my-booking-tool.spec) -- unlike
    the older, narrower step-6 "Restart my-booking.service now?" guard
    (InteractiveSetupRestartSessionGuardTest below, unaffected by this),
    this fires unconditionally, before step 1 even prints, regardless of
    whether settings.toml is stale."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

    def test_active_sessions_refuses_before_any_step_and_asks_nothing(self):
        payload = _sessions_payload({
            "kind": "guest", "who": "ines@example.org", "name": "Guest One",
            "connected_since": "2026-07-13T06:49:00+00:00", "last_seen": "2026-07-13T07:37:00+00:00",
        })
        prompt = FakePrompts({}, default=True)
        printed: list[str] = []
        with patch("app.cli_checks.fetch_active_sessions", return_value=(payload, None)):
            fails, warns = cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=printed.append,
            )
        self.assertEqual(prompt.asked, [])
        self.assertEqual((fails, warns), (1, 0))
        text = "\n".join(printed)
        self.assertIn("1 active session(s)", text)
        self.assertIn("Guest One", text)
        self.assertIn("ines@example.org", text)
        self.assertIn("my-bt admin logout", text)
        self.assertNotIn("-- 1. Secrets --", text)

    def test_no_sessions_proceeds_normally(self):
        with patch("app.cli_checks.fetch_active_sessions", return_value=(_sessions_payload(), None)):
            prompt = FakePrompts({})
            printed: list[str] = []
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=printed.append,
            )
        self.assertIn("-- 1. Secrets --", "\n".join(printed))

    def test_unreachable_service_fails_open_same_as_the_rpm_gate(self):
        with patch("app.cli_checks.fetch_active_sessions", return_value=(None, "Connection refused")):
            prompt = FakePrompts({})
            printed: list[str] = []
            cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=printed.append,
            )
        self.assertIn("-- 1. Secrets --", "\n".join(printed))


class InteractiveSetupRestartSessionGuardTest(unittest.TestCase):
    """2026-07-10: the "Restart my-booking.service now?" prompt
    (fired by check_settings_fresh's "aren't live yet" warning) used to
    have no session-awareness at all, unlike the RPM's own %pre gate
    before an upgrade -- SESSIONS is in-memory (app/webapp.py's module
    docstring), so this restart silently drops every session. A hard
    refuse (not just a warning) was chosen, with the fix pointed at
    directly: `my-bt admin logout`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")

        self._patches = [
            patch("app.cli_checks.check_group_membership", return_value=[("my-booking group membership (alice)", "ok", "")]),
            patch("app.cli_checks.check_systemd", return_value=[("my-booking.service", "ok", "enabled, active")]),
            patch("app.cli_checks.check_selinux", return_value=[("SELinux httpd_can_network_connect", "ok", "on")]),
            patch("app.cli_checks.check_settings_fresh",
                  return_value=[("my-booking.service freshness", "warn",
                                  "settings.toml was edited after my-booking.service last (re)started -- "
                                  "those edits aren't live yet: sudo systemctl restart my-booking.service")]),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_active_sessions_refuses_the_restart_and_never_prompts(self):
        prompt = FakePrompts({"Restart my-booking.service": True})
        calls: list[list[str]] = []
        printed: list[str] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=printed.append,
            check_active_sessions=lambda: (2, None),
        )
        self.assertEqual(prompt.asked_matching("Restart my-booking.service"), [])
        self.assertFalse(any(c == ["systemctl", "restart", "my-booking.service"] for c in calls))
        self.assertTrue(any("2 active session(s)" in line for line in printed))
        self.assertTrue(any("my-bt admin logout" in line for line in printed))

    def test_zero_sessions_proceeds_exactly_as_before(self):
        prompt = FakePrompts({"Restart my-booking.service": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
            check_active_sessions=lambda: (0, None),
        )
        self.assertEqual(len(prompt.asked_matching("Restart my-booking.service")), 1)
        self.assertTrue(any(c == ["systemctl", "restart", "my-booking.service"] for c in calls))

    def test_session_check_error_fails_open_same_as_service_not_running(self):
        # The service being unreachable is treated as "nothing running,
        # nothing to protect" -- same fail-open reasoning as the RPM's own
        # %pre gate -- not a reason to refuse.
        prompt = FakePrompts({"Restart my-booking.service": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: True,
            print_fn=lambda *_: None,
            check_active_sessions=lambda: (None, "Connection refused"),
        )
        self.assertEqual(len(prompt.asked_matching("Restart my-booking.service")), 1)
        self.assertTrue(any(c == ["systemctl", "restart", "my-booking.service"] for c in calls))


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
        # 2026-07-08 fix: app.cli_checks._resolve_static_source() falls
        # back to the REAL, hardcoded /usr/share/doc/my-booking-tool/site
        # (the RPM's %doc copy) once this fake `home` has nothing under
        # site/ -- on a machine with an older build of this same RPM
        # already installed (e.g. the VPS that runs `rpmbuild`'s own
        # %check), that path genuinely exists, so
        # test_no_checkout_source_is_never_prompted_to_copy started
        # failing there while passing everywhere else. test_cli_checks.py
        # already patches this same constant per-test for the identical
        # reason (see its own `_DOC_SITE_DIR` patches) -- this class was
        # just missing it. Pointed at a path that's guaranteed to never
        # exist, class-wide, so every test here stays isolated from
        # whatever happens to be installed on the machine running them.
        doc_site_patcher = patch("app.cli_checks._DOC_SITE_DIR", self.home / "no-such-doc-site")
        doc_site_patcher.start()
        self.addCleanup(doc_site_patcher.stop)

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

    def test_maintenance_banner_alone_does_not_trigger_a_vimdiff_offer(self):
        # 2026-07-10: a vimdiff offered by setup -i where
        # the ONLY difference was the maintenance banner `my-bt admin site-maintenance
        # on` had inserted into the live index.html led to `my-bt setup -i`
        # learning about maintenance mode and ignoring any change linked to
        # this, so it no longer proposes a vimdiff when this is the only
        # difference.
        content = "<html><body>hello world</body></html>"
        (self.home / "site" / "index.html").write_text(content)
        banner = maintenance.banner_html("admin@example.org", "back soon")
        (self.static_dir / "index.html").write_text(maintenance.insert_banner(content, banner))
        raw = _raw(site={"static_site_dir": str(self.static_dir)})
        # default=True: would open vimdiff if (wrongly) asked+accepted --
        # also makes unrelated prompts elsewhere in interactive_setup()
        # (e.g. an nginx reload offer) accept too, so this test asserts
        # specifically on "no vimdiff for index.html", not on the full
        # (unrelated) call list.
        prompt = FakePrompts({}, default=True)
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("vimdiff"), [])
        self.assertFalse(any(c[0] == "vimdiff" for c in calls))

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


_SAMPLE_INDEX_HTML = """<html><body>
<div class="top-bar" id="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
<div id="schedule-exceptions"></div>
<ul><li><a href="/book/sat-trier">Book your place here.</a></li></ul>
<script>(function () { fetch('/my/session', { credentials: 'same-origin' }); })();</script>
<script>(function () { fetch('/schedule-exceptions', { credentials: 'same-origin' }); })();</script>
</body></html>
"""


class InteractiveSetupIndexEmbeddedTest(unittest.TestCase):
    """index_embedded.html (reworked 2026-07-13): no more separate .tmpl
    file -- DERIVED fresh from the LIVE, just-reconciled index.html at
    static_site_dir, gated entirely on [site].index_embedded_enabled.
    Same copy-if-missing/vimdiff-if-different UX as index.html itself."""

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
        doc_site_patcher = patch("app.cli_checks._DOC_SITE_DIR", self.home / "no-such-doc-site")
        doc_site_patcher.start()
        self.addCleanup(doc_site_patcher.stop)

    def _deploy_index_html(self, text=_SAMPLE_INDEX_HTML):
        (self.static_dir / "index.html").write_text(text, encoding="utf-8")

    def test_disabled_is_never_prompted(self):
        self._deploy_index_html()
        raw = _raw(site={"static_site_dir": str(self.static_dir)}, course=[])
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("index_embedded.html"), [])
        self.assertFalse((self.static_dir / site_render.EMBEDDED_OUTPUT_NAME).exists())

    def test_no_live_index_html_yet_is_skipped_not_an_error(self):
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("index_embedded.html"), [])
        self.assertFalse((self.static_dir / site_render.EMBEDDED_OUTPUT_NAME).exists())

    def test_accepting_copies_the_derived_page(self):
        self._deploy_index_html()
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        prompt = FakePrompts({"Derive and copy": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        out = self.static_dir / site_render.EMBEDDED_OUTPUT_NAME
        self.assertTrue(out.exists())
        self.assertIn("MANAGED BY my-bt", out.read_text())
        self.assertNotIn("<script>", out.read_text())

    def test_custom_attention_message_from_raw_settings_is_derived_in(self):
        # 2026-07-13: [site].custom_attention_message must be read from raw
        # settings and threaded into the derivation, same as
        # index_embedded_new_tab_links already is.
        self._deploy_index_html()
        raw = _raw(
            site={
                "static_site_dir": str(self.static_dir), "index_embedded_enabled": True,
                "custom_attention_message": "On vacation from 2026-08-01.",
            },
            course=[],
        )
        prompt = FakePrompts({"Derive and copy": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        out = self.static_dir / site_render.EMBEDDED_OUTPUT_NAME
        self.assertIn("On vacation from 2026-08-01.", out.read_text())

    def test_declining_leaves_it_unwritten(self):
        self._deploy_index_html()
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        prompt = FakePrompts({"Derive and copy": False})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertFalse((self.static_dir / site_render.EMBEDDED_OUTPUT_NAME).exists())

    def test_not_prompted_when_already_matching(self):
        self._deploy_index_html()
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        out = self.static_dir / site_render.EMBEDDED_OUTPUT_NAME
        derived = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        out.write_text(derived, encoding="utf-8")
        # default=True: would write/vimdiff again if (wrongly) asked+accepted
        # -- also accepts the unrelated privacy.html regen prompt this
        # fixture's own site/privacy.html.tmpl triggers, so this asserts
        # specifically on "no index_embedded.html prompt", not the full
        # (unrelated) call list.
        prompt = FakePrompts({}, default=True)
        before = out.read_text()
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("index_embedded.html"), [])
        self.assertEqual(out.read_text(), before)

    def test_stale_deployed_page_offers_vimdiff_against_a_derived_tempfile(self):
        self._deploy_index_html()
        course = {
            "shortname": "sat-trier", "title": "Yoga", "location": "Trier", "weekday": "sat",
            "start_time": "10:45", "duration_minutes": 120, "capacity": 10,
            "date_override": [{"date": "2099-01-01", "start_time": "09:45"}],
        }
        out = self.static_dir / site_render.EMBEDDED_OUTPUT_NAME
        # Deployed with NO overrides baked in yet -- stale relative to what
        # would currently derive.
        stale = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        out.write_text(stale, encoding="utf-8")
        raw = _raw(
            site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True},
            course=[course],
        )
        # Read the tmp file's content from INSIDE the fake `run` call --
        # interactive_setup deletes it right after run() returns (same
        # cleanup a real, blocking `vimdiff` invocation would want once the
        # user closes the editor), so it's gone by the time this method
        # itself returns.
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd
            captured["tmp_content"] = Path(cmd[2]).read_text(encoding="utf-8")

        prompt = FakePrompts({"vimdiff": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=fake_run, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "vimdiff")
        self.assertEqual(cmd[1], str(out))
        self.assertIn("ATTENTION", captured["tmp_content"])
        # And the temp file is cleaned up afterward, not left behind.
        self.assertFalse(Path(cmd[2]).exists())

    def test_active_maintenance_banner_alone_never_triggers_a_vimdiff_offer(self):
        # Deployed page matches what would derive EXCEPT for an active
        # maintenance banner -- must never look like drift needing a merge
        # (same reasoning as index.html's own maintenance-aware comparison).
        self._deploy_index_html()
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        out = self.static_dir / site_render.EMBEDDED_OUTPUT_NAME
        derived = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        banner = maintenance.banner_html("admin@example.org", "back Monday")
        out.write_text(maintenance.insert_banner(derived, banner), encoding="utf-8")
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(prompt.asked_matching("index_embedded.html"), [])
        self.assertIn("MAINTENANCE-BANNER:START", out.read_text())

    def test_derivation_error_prints_fail_and_never_prompts(self):
        broken_html = _SAMPLE_INDEX_HTML.replace('href="/my"', 'href="/my-account"')
        self._deploy_index_html(broken_html)
        raw = _raw(site={"static_site_dir": str(self.static_dir), "index_embedded_enabled": True}, course=[])
        printed = []
        prompt = FakePrompts({}, default=True)
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=printed.append,
        )
        self.assertEqual(prompt.asked_matching("index_embedded.html"), [])
        self.assertTrue(any("[fail] index_embedded.html" in line for line in printed))


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

    def test_accepting_vimdiff_then_reload_reloads_nginx(self):
        # 2026-07-13, reordered: this used to be TWO separate reload
        # prompts in one run (step 4's own, firing BEFORE this vimdiff
        # ever ran, then a second one right after it) -- confusing, and
        # still let a real run reload nginx with stale content before the
        # merge below ever happened. Now there's exactly ONE reload
        # prompt for the whole nginx section, asked at the very end,
        # after this vimdiff (and any rename) has already had its chance
        # to change what's on disk -- see nginx_content_changed's own
        # comment in app/cli_setup.py.
        self.deployed.write_text("location /admin { } # deployed")
        (self.home / "site" / "nginx-locations.conf").write_text("location /admin { } # checkout")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True, "pick up these changes": True})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertTrue(any(c[:2] == ["nginx", "-t"] for c in calls))
        self.assertTrue(any(c[:2] == ["systemctl", "reload"] for c in calls))

    def test_declining_reload_after_vimdiff_runs_nothing_further(self):
        self.deployed.write_text("location /admin { } # deployed")
        (self.home / "site" / "nginx-locations.conf").write_text("location /admin { } # checkout")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True, "pick up these changes": False})
        calls: list[list[str]] = []
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=calls.append, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertFalse(any(c[:2] == ["systemctl", "reload"] for c in calls))

    def test_only_one_reload_prompt_is_ever_asked_after_a_vimdiff_merge(self):
        # The real bug this reordering fixes: there must be exactly ONE
        # "reload nginx" question in a run that included a vimdiff merge,
        # not step 4's own plus a second one right after the merge.
        self.deployed.write_text("location /admin { } # deployed")
        (self.home / "site" / "nginx-locations.conf").write_text("location /admin { } # checkout")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True, "pick up these changes": True})
        cli_setup.interactive_setup(
            raw, self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lambda *_: None,
        )
        self.assertEqual(len(prompt.asked_matching("reload nginx")), 1)

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

    def test_not_deployed_but_live_under_old_name_offers_vimdiff_then_rename(self):
        """2026-07-10: after the booking.example.org.conf -> nginx-locations.conf
        rename, a real server still had the file under the OLD name --
        nothing was at nginx_conf_path yet, but nginx -T said the vhost was
        still live from elsewhere. The package installer (or
        my-bt setup -i) needed to fix this -- vimdiff to reconcile content
        first, then a root-gated rename into place, then offer to reload --
        2026-07-13, reordered: that final reload is now the ONE shared
        end-of-section prompt, asked once after both the vimdiff merge and
        the rename have already happened, not a separate prompt per step."""
        old = self.home / "old-etc" / "booking.example.org.conf"
        old.parent.mkdir()
        old.write_text("location /admin { } # old, missing /reinstate")
        (self.home / "site" / "nginx-locations.conf").write_text("location /admin { } # checkout, complete")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"vimdiff": True, "Rename": True, "pick up these changes": True})
        calls: list[list[str]] = []
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=calls.append, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertTrue(any(
            c[0] == "vimdiff" and str(old) in c and
            str(self.home / "site" / "nginx-locations.conf") in c
            for c in calls
        ))
        self.assertFalse(old.exists())
        self.assertTrue(self.deployed.exists())
        self.assertEqual(self.deployed.read_text(), "location /admin { } # old, missing /reinstate")
        self.assertTrue(any(c[:2] == ["nginx", "-t"] for c in calls))
        self.assertTrue(any(c[:2] == ["systemctl", "reload"] for c in calls))

    def test_not_deployed_but_live_under_old_name_non_root_never_renames(self):
        old = self.home / "old-etc" / "booking.example.org.conf"
        old.parent.mkdir()
        old.write_text("location /admin { }")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        # Decline the (root-independent) "point the setting at reality"
        # offer explicitly, so this test actually exercises the root gate
        # on the rename fallback instead of resolving via that other path.
        prompt = FakePrompts({"instead of renaming": False}, default=True)
        calls: list[list[str]] = []
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=calls.append, is_root=lambda: False,
                print_fn=lambda *_: None,
            )
        self.assertEqual(prompt.asked_matching("Rename"), [])
        self.assertTrue(old.exists())
        self.assertFalse(self.deployed.exists())

    def test_not_deployed_but_live_under_old_name_declining_leaves_both_alone(self):
        old = self.home / "old-etc" / "booking.example.org.conf"
        old.parent.mkdir()
        old.write_text("location /admin { }")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"instead of renaming": False, "Rename": False})
        calls: list[list[str]] = []
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=calls.append, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertTrue(old.exists())
        self.assertFalse(self.deployed.exists())
        self.assertEqual(calls, [])

    def test_offers_to_point_nginx_conf_path_at_the_live_file_instead(self):
        """2026-07-10: an earlier version of this step only ever offered
        to rename the live file, but settings.toml should instead be able
        to say booking.example.org.conf is what's actually in use and have my-bt
        respect that. This is the low-risk alternative: just fix
        the setting, never touch the actual nginx file. Doesn't need root
        (writing settings.toml isn't a privileged operation)."""
        old = self.home / "old-etc" / "booking.example.org.conf"
        old.parent.mkdir()
        old.write_text("location /admin { }")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"instead of renaming": True})
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
            )
        self.assertEqual(prompt.asked_matching("Rename"), [])  # resolved, nothing left to rename
        self.assertTrue(old.exists())  # the actual file is untouched
        self.assertFalse(self.deployed.exists())
        written = Path(self.settings_path).read_text()
        self.assertIn(f'nginx_conf_path = "{old}"', written)
        self.assertEqual(raw["site"]["nginx_conf_path"], str(old))

    def test_declining_to_point_nginx_conf_path_falls_through_to_rename_offer(self):
        old = self.home / "old-etc" / "booking.example.org.conf"
        old.parent.mkdir()
        old.write_text("location /admin { }")
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({"instead of renaming": False, "Rename": True})
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=old):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertEqual(len(prompt.asked_matching("Rename")), 1)
        self.assertFalse(old.exists())
        self.assertTrue(self.deployed.exists())
        self.assertNotIn("nginx_conf_path", Path(self.settings_path).read_text())

    def test_no_live_file_detected_never_offers_to_rename(self):
        raw = _raw(site={"nginx_conf_path": str(self.deployed)})
        prompt = FakePrompts({}, default=True)
        with patch("app.cli_checks._live_nginx_conf_file_for_host", return_value=None):
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertEqual(prompt.asked_matching("Rename"), [])


class InteractiveSetupCspSelfHealTest(unittest.TestCase):
    """CSP hash self-heal (2026-07-16, the operator: "can we automate and fix
    this within the build or setup ... maybe can self-heal?" -- after
    `my-bt admin setup -i` had only ever warned about a missing hash,
    never fixed it). The deployed file on disk and the mocked `nginx -T`
    dump are kept byte-identical in every test here (same convention as
    tests/test_cli_checks.py::CheckCspHashesDeployedTest) -- the
    interactive walkthrough reads/patches/writes the real file directly,
    while check_csp_hashes_deployed()/expected_csp_hashes() go through the
    mocked `nginx -T`/no static_site_dir, so both need to agree on what's
    "currently deployed" for these tests to mean anything. Always uses an
    identical checkout-side site/nginx-locations.conf too, so the
    unrelated vimdiff-reconciliation step (covered by
    InteractiveSetupNginxConfDeployedTest above) never fires and adds a
    confusing extra prompt to these tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text("x")
        self.deployed = self.home / "deployed" / "nginx-locations.conf"
        self.deployed.parent.mkdir()
        self.expected = cli_checks.expected_csp_hashes({})

    def _conf_text(self, hashes):
        hash_tokens = " ".join(f"'{h}'" for h in hashes)
        return (
            "server {\n"
            "    server_name booking.example.org;\n"
            '    add_header Content-Security-Policy "default-src \'self\'; '
            f"script-src 'self' {hash_tokens}; "
            'style-src \'self\';" always;\n'
            "}\n"
        )

    def _write(self, text):
        self.deployed.write_text(text)
        (self.home / "site" / "nginx-locations.conf").write_text(text)

    def _raw(self):
        return _raw(site={"nginx_conf_path": str(self.deployed), "base_url": "https://booking.example.org"})

    def test_all_hashes_already_present_never_prompts(self):
        text = self._conf_text(self.expected.values())
        self._write(text)
        prompt = FakePrompts({}, default=True)
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": text})()):
            cli_setup.interactive_setup(
                self._raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
            )
        self.assertEqual(prompt.asked_matching("add the missing hash"), [])

    def test_declining_leaves_the_file_untouched(self):
        text = self._conf_text([])
        self._write(text)
        prompt = FakePrompts({"add the missing hash": False}, default=True)
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": text})()):
            cli_setup.interactive_setup(
                self._raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
            )
        self.assertEqual(self.deployed.read_text(), text)

    def test_accepting_adds_missing_hashes_when_nginx_test_passes(self):
        text = self._conf_text([])
        self._write(text)
        prompt = FakePrompts({"add the missing hash": True}, default=False)
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": text})()):
            cli_setup.interactive_setup(
                self._raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
                run_nginx_test=lambda: True,
            )
        patched = self.deployed.read_text()
        for h in self.expected.values():
            self.assertIn(f"'{h}'", patched)

    def test_failing_nginx_test_reverts_the_file_and_never_marks_content_changed(self):
        text = self._conf_text([])
        self._write(text)
        prompt = FakePrompts({"add the missing hash": True}, default=False)
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": text})()):
            cli_setup.interactive_setup(
                self._raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
                run_nginx_test=lambda: False,
            )
        # Reverted -- never left the file in a half-patched/broken state.
        self.assertEqual(self.deployed.read_text(), text)
        # And the final reload prompt's wording never claims there's
        # anything new to pick up, since the revert means nothing actually
        # changed on disk after all.
        reload_asks = prompt.asked_matching("systemctl reload nginx")
        self.assertTrue(all("pick up these changes" not in m for m in reload_asks))

    def test_missing_hash_added_calls_the_injected_nginx_test_runner(self):
        text = self._conf_text([])
        self._write(text)
        prompt = FakePrompts({"add the missing hash": True}, default=False)
        calls = []
        with patch("app.cli_checks.shutil.which", return_value="/usr/sbin/nginx"), \
             patch("app.cli_checks.subprocess.run",
                   return_value=type("R", (), {"returncode": 0, "stdout": text})()):
            cli_setup.interactive_setup(
                self._raw(), self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lambda *_: None,
                run_nginx_test=lambda: (calls.append(1) or True),
            )
        self.assertEqual(len(calls), 1)


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


class InteractiveSetupCalendarInviteFormatTest(unittest.TestCase):
    """Step 13, 2026-07-14 -- the "on install" half of a standing
    request (2026-07-09: resync either on install or on the next moment
    this calendar invite is touched again). Never prompts (see the step's
    own comment in app/cli_setup.py for why) -- just calls
    app.calendar_sync.resync_if_format_changed() and reports what it did.
    A REAL, valid settings.toml is needed here (unlike most other tests in
    this file, which get away with the placeholder "x" since they never
    reach load_settings()) -- load_settings() itself is exercised for
    real, only CalDAVClient/resync_if_format_changed are mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "site").mkdir()
        (self.home / "site" / "privacy.html.tmpl").write_text("kept ${retention_months}m")
        secrets = self.home / "secrets"
        secrets.mkdir()
        for name in ("caldav_password", "smtp_password", "admin_password_hash"):
            (secrets / name).write_text("s3cr3t\n", encoding="utf-8")
        (secrets / "erasure_pepper").write_text("aa" * 32 + "\n", encoding="utf-8")  # must be valid hex
        self.settings_path = str(self.home / "settings.toml")
        Path(self.settings_path).write_text(f"""
[site]
timezone = "Europe/Berlin"
admin_email = "admin@example.org"
base_url = "https://example.org"

[calendar]
caldav_url = "https://dav.example.org/"
caldav_username = "calendar@example.org"
caldav_password_file = "{secrets / 'caldav_password'}"
booking_calendar = "Bookings"
conflict_calendars = ["Bookings"]

[smtp]
host = "smtp.example.org"
port = 465
username = "calendar@example.org"
password_file = "{secrets / 'smtp_password'}"
from_address = "admin@example.org"

[admin]
password_hash_file = "{secrets / 'admin_password_hash'}"

[privacy]
erasure_pepper_file = "{secrets / 'erasure_pepper'}"
""", encoding="utf-8")
        self.raw = _raw(calendar={
            "caldav_url": "https://dav.example.org/",
            "caldav_username": "calendar@example.org",
            "caldav_password_file": str(secrets / "caldav_password"),
            "booking_calendar": "Bookings",
            "conflict_calendars": ["Bookings"],
        })

    def _run(self, **patches):
        lines: list[str] = []
        prompt = FakePrompts()
        with patch("app.cancel_flow.build_caldav_client", return_value=object()), \
             patch("app.cancel_flow.calendar_href", return_value="/caldav/Bookings/"), \
             patch("app.calendar_sync.resync_if_format_changed", **patches) as mock_resync:
            cli_setup.interactive_setup(
                self.raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
                print_fn=lines.append,
            )
        return lines, prompt, mock_resync

    def test_not_configured_shows_skip(self):
        lines: list[str] = []
        prompt = FakePrompts()
        cli_setup.interactive_setup(
            _raw(), self.settings_path, str(self.home),
            prompt=prompt, run=lambda cmd: None, is_root=lambda: False,
            print_fn=lines.append,
        )
        self.assertTrue(any("skip" in ln and "caldav_url" in ln for ln in lines))

    def test_format_unchanged_reports_nothing_to_do_and_never_prompts(self):
        # This step never calls `prompt` at all -- see its own comment in
        # app/cli_setup.py -- unlike step 4/11 (nginx/git-snapshot) just
        # above it, which always ask unconditionally regardless of what
        # this test cares about.
        lines, prompt, _mock = self._run(return_value=None)
        text = "\n".join(lines)
        self.assertIn("unchanged since the last resync", text)
        self.assertEqual(prompt.asked_matching("calendar"), [])
        self.assertEqual(prompt.asked_matching("resync"), [])

    def test_format_changed_reports_the_resync_count(self):
        from app.calendar_sync import ResyncResult

        lines, _prompt, _mock = self._run(return_value=ResyncResult(fixed=3))
        text = "\n".join(lines)
        self.assertIn("resynced 3 upcoming occurrence(s)", text)

    def test_format_changed_with_skips_warns_and_names_them(self):
        # 2026-07-15/16: from a real production run, 3 occurrences
        # hit persistent CalDAV conflicts and got skipped, yet this step
        # printed "[ok] ... resynced 6 upcoming occurrence(s)" with no
        # hint anything had failed. The "OK" summary line didn't match
        # what the detailed output actually showed.
        from app.calendar_sync import ResyncResult

        lines, _prompt, _mock = self._run(
            return_value=ResyncResult(fixed=6, skipped=["lux-fri-yoga on 2026-07-10: HTTP 412"]),
        )
        text = "\n".join(lines)
        self.assertIn("[warn]", text)
        self.assertIn("resynced 6", text)
        self.assertIn("1 FAILED", text)
        self.assertIn("lux-fri-yoga on 2026-07-10", text)
        self.assertIn("resync-calendar", text)
        self.assertNotIn("[ok] calendar invite format changed", text)

    def test_caldav_failure_is_a_warning_not_a_crash(self):
        lines, _prompt, _mock = self._run(side_effect=RuntimeError("PROPFIND -> HTTP 401"))
        text = "\n".join(lines)
        self.assertIn("[warn]", text)
        self.assertIn("401", text)


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
    def test_configured_value_differing_from_detected_offers_to_update(self, _access):
        with patch("app.cli_checks._nginx_access_log_for_host", return_value=str(self.log_path)):
            prompt = FakePrompts({"Update nginx_access_log": True})
            lines: list[str] = []
            raw = _raw(watchdog={"nginx_access_log": "/some/stale/path.log"})
            cli_setup.interactive_setup(
                raw, self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lines.append,
            )
        self.assertEqual(len(prompt.asked_matching("Update nginx_access_log")), 1)
        written = Path(self.settings_path).read_text()
        self.assertIn(f'nginx_access_log = "{self.log_path}"', written)
        self.assertNotIn("/some/stale/path.log", written)
        self.assertEqual(raw["watchdog"]["nginx_access_log"], str(self.log_path))

    @patch("app.cli_checks.check_watchdog_nginx_access", return_value=[])
    def test_configured_value_differing_from_detected_declining_leaves_it_stale(self, _access):
        with patch("app.cli_checks._nginx_access_log_for_host", return_value=str(self.log_path)):
            prompt = FakePrompts({"Update nginx_access_log": False})
            Path(self.settings_path).write_text('[watchdog]\nnginx_access_log = "/some/stale/path.log"\n')
            cli_setup.interactive_setup(
                _raw(watchdog={"nginx_access_log": "/some/stale/path.log"}),
                self.settings_path, str(self.home),
                prompt=prompt, run=lambda cmd: None, is_root=lambda: True,
                print_fn=lambda *_: None,
            )
        self.assertIn("/some/stale/path.log", Path(self.settings_path).read_text())


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
    """The closing "Done." line (2026-07-08: it should reflect whether
    there were any problems) -- previously a flat
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
        text, _result = self._run_full(report)
        return text

    def _run_full(self, report: dict) -> tuple[str, tuple[int, int]]:
        lines: list[str] = []
        with patch.object(cli_setup, "build_report", return_value=report):
            result = cli_setup.interactive_setup(
                _raw(), self.settings_path, str(self.home),
                prompt=FakePrompts(), run=lambda cmd: None, is_root=lambda: False,
                print_fn=lines.append,
            )
        return "\n".join(lines), result

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

    def test_repeats_every_warning_and_failure_before_the_done_line(self):
        # 2026-07-08: all warnings are repeated at the end of
        # setup and status explicitly -- same treatment as plain
        # print_report() above, for the interactive walkthrough's own
        # closing summary.
        text = self._run({
            "secrets": [("secret: x", "fail", "missing")],
            "group": [("g", "warn", "not in group")],
        })
        repeated_section = text.split("Still need attention, repeated from above:")[1]
        self.assertIn("[FAIL] secret: x -- missing", repeated_section)
        self.assertIn("[WARN] g -- not in group", repeated_section)

    def test_no_repeated_section_when_all_clear(self):
        text = self._run({"group": [("g", "ok", "fine")]})
        self.assertNotIn("repeated from above", text)

    def test_returns_fails_and_warns_so_the_caller_can_exit_non_zero(self):
        # Regression coverage for 2026-07-10: `my-bt setup -i && my-bt
        # status` used to run `status` unconditionally, because
        # interactive_setup() (unlike print_report()) never returned
        # anything at all, so scripts/my-bt's cmd_setup had nothing to
        # exit non-zero on for the -i branch specifically.
        _text, result = self._run_full({
            "secrets": [("secret: x", "fail", "missing")],
            "group": [("g", "warn", "not in group")],
        })
        self.assertEqual(result, (1, 1))

    def test_returns_zero_zero_when_all_clear(self):
        _text, result = self._run_full({"group": [("g", "ok", "fine")]})
        self.assertEqual(result, (0, 0))


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

    def test_replaces_existing_value_in_place_instead_of_duplicating(self):
        Path(self.settings_path).write_text(
            "[watchdog]\nnginx_access_log = \"/some/stale/path.log\"\nwindow_minutes = 15\n"
        )
        cli_setup._add_nginx_access_log_setting(self.settings_path, "/var/log/nginx/access.log")
        text = Path(self.settings_path).read_text()
        self.assertIn('nginx_access_log = "/var/log/nginx/access.log"', text)
        self.assertNotIn("/some/stale/path.log", text)
        self.assertEqual(text.count("nginx_access_log"), 1)
        self.assertIn("window_minutes = 15", text)


if __name__ == "__main__":
    unittest.main()
