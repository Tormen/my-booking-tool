"""Builds and prints/walks-through `my-bt setup`'s report (scripts/my-bt).
Kept in this importable package (see app/cli_checks.py's docstring for
why) so the interactive walkthrough is unit-testable: every
side-effecting dependency -- asking a yes/no question, reading a secret,
running an external command, checking for root -- is injected as a plain
callable, defaulting to the real thing. Tests substitute canned
answers/fake runners instead of piping stdin at a real process or
needing an actual tty/root/systemd/rpm on the machine running the suite.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import cli_checks, config, maintenance, security as app_security, site_render
from .atomic_io import atomic_write_text

# Same env-var-with-localhost-default convention as scripts/my-bt's own
# DEFAULT_INTERNAL_URL -- this is an internal, always-loopback listener,
# not something deployments realistically reconfigure per-command.
_DEFAULT_INTERNAL_URL = os.environ.get("MY_BOOKING_INTERNAL_URL", "http://127.0.0.1:8811")


def _default_check_active_sessions(internal_url: str = _DEFAULT_INTERNAL_URL) -> tuple[int | None, str | None]:
    """GETs {internal_url}/internal/status (see app/webapp.py::
    internal_status) and returns (session_count, error) -- exactly one
    set, mirroring scripts/my-bt's own _query_internal_status (kept
    separate rather than imported: that one lives in the non-.py CLI
    script and isn't importable from here -- see that module's own
    comment on why check/report logic belongs in this package instead).

    2026-07-10: added after realizing interactive_setup's own
    "Restart my-booking.service now?" step (below) had no session-
    awareness at all, unlike the RPM's own %pre gate before an upgrade --
    a restart here silently drops every session (SESSIONS is in-memory,
    see app/webapp.py's module docstring). A connection failure (service
    not running/reachable) returns (None, error) -- the call site treats
    that as "nothing running, nothing to protect", not a reason to
    refuse, same as the RPM gate's own fail-open behavior."""
    url = internal_url.rstrip("/") + "/internal/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - fixed http://127.0.0.1 URL
            payload = json.loads(resp.read().decode("utf-8"))
            return len(payload.get("sessions", [])), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def _default_prompt(message: str) -> bool:
    try:
        return input(f"{message} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _default_read_secret(label: str) -> str:
    return getpass.getpass(f"{label}: ")


def _default_is_root() -> bool:
    return os.geteuid() == 0


def _run_tolerant(cmd: list[str], print_fn: Callable[[str], None]) -> None:
    """subprocess.call, but tolerant of the binary not being installed -- a
    minimal Fedora image may be missing e.g. vim-enhanced (`vimdiff`) or
    policycoreutils-python-utils (`setsebool`), and a missing tool here
    shouldn't crash the whole interactive walkthrough, just that step."""
    try:
        subprocess.call(cmd)
    except FileNotFoundError:
        print_fn(f"[fail] {cmd[0]}: not installed -- run this step manually: {' '.join(cmd)}")


def _default_run_nginx_test() -> bool:
    """Runs `nginx -t`, letting its own stdout/stderr print straight to the
    terminal (same as every other `run([...])` call in this walkthrough),
    but -- unlike those, which are fire-and-forget via subprocess.call's
    discarded return value -- actually reports whether it passed.

    Needed specifically for the CSP self-heal step below: `run` itself
    (subprocess.call, return value thrown away) can't tell a caller
    whether `nginx -t` passed, which is fine for the existing "reload now?"
    prompt (a human is watching the output either way) but not safe enough
    to gate an automatic revert-on-failure. A missing `nginx` binary counts
    as failure here (can't verify, so don't trust the edit), unlike
    `_run_tolerant`'s more forgiving treatment of a missing tool elsewhere
    in this file -- there's nothing tolerant about silently keeping a CSP
    edit nobody could actually verify."""
    try:
        return subprocess.call(["nginx", "-t"]) == 0
    except FileNotFoundError:
        return False


_WATCHDOG_HEADER_RE = re.compile(r"^\[watchdog\][ \t]*\r?\n", re.MULTILINE)
_NGINX_ACCESS_LOG_LINE_RE = re.compile(r'^nginx_access_log[ \t]*=.*\r?\n', re.MULTILINE)


def _add_nginx_access_log_setting(settings_path: str, value: str) -> None:
    """Writes `nginx_access_log = "<value>"` into settings.toml's
    [watchdog] table -- two distinct callers, both from interactive_setup
    step 10: the "not configured yet" case (no existing line -- inserted
    right after the [watchdog] header, creating the table at the end of
    the file if it doesn't exist yet) and the "configured but stale" case
    (nginx's live config has moved on -- the existing line is replaced in
    place, keeping its original position rather than appending a
    duplicate, which TOML wouldn't parse anyway). A plain text edit, not a
    parse-and-re-serialize round-trip -- tomllib has no writer anyway, and
    re-emitting the whole file from the parsed dict would silently drop
    every comment in this hand-maintained file."""
    text = Path(settings_path).read_text(encoding="utf-8")
    line = f'nginx_access_log = "{value}"\n'
    m = _NGINX_ACCESS_LOG_LINE_RE.search(text)
    if m is not None:
        text = text[:m.start()] + line + text[m.end():]
    else:
        m = _WATCHDOG_HEADER_RE.search(text)
        if m is None:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n[watchdog]\n{line}"
        else:
            text = text[:m.end()] + line + text[m.end():]
    # 2026-07-15: atomic_write_text, not a bare write_text() -- this is
    # the live settings.toml. See app/atomic_io.py.
    atomic_write_text(settings_path, text)


_SITE_HEADER_RE = re.compile(r"^\[site\][ \t]*\r?\n", re.MULTILINE)
_NGINX_CONF_PATH_LINE_RE = re.compile(r'^nginx_conf_path[ \t]*=.*\r?\n', re.MULTILINE)


def _write_nginx_conf_path_setting(settings_path: str, value: str) -> None:
    """Writes `nginx_conf_path = "<value>"` into settings.toml's [site]
    table -- replacing an existing line in place if there is one (the
    normal case: this is only ever called when [site].nginx_conf_path was
    ALREADY configured, just naming a file nginx doesn't actually have --
    see interactive_setup's own comment on why "point the setting at
    reality" is offered before "rename the live file to match the
    setting"), falling back to inserting one right after the [site] header
    otherwise. Same plain-text-edit approach as
    _add_nginx_access_log_setting() and for the same reason -- tomllib has
    no writer, and re-emitting the whole file from the parsed dict would
    drop every comment in this hand-maintained file."""
    text = Path(settings_path).read_text(encoding="utf-8")
    line = f'nginx_conf_path = "{value}"\n'
    m = _NGINX_CONF_PATH_LINE_RE.search(text)
    if m is not None:
        text = text[:m.start()] + line + text[m.end():]
    else:
        m = _SITE_HEADER_RE.search(text)
        if m is None:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n[site]\n{line}"
        else:
            text = text[:m.end()] + line + text[m.end():]
    # 2026-07-15: atomic_write_text, not a bare write_text() -- this is
    # the live settings.toml. See app/atomic_io.py.
    atomic_write_text(settings_path, text)


def tmpl_path(home: str) -> Path:
    return Path(home) / "site" / site_render.TEMPLATE_NAME


def _course_summary(raw: dict) -> str:
    return ", ".join(c.get("shortname", "?") for c in raw.get("course", [])) or "(none configured)"


def _privacy_summary(raw: dict) -> str:
    privacy = raw.get("privacy", {})
    return (f"retention_months={privacy.get('retention_months', 24)}, "
            f"canceled_retention_months={privacy.get('canceled_retention_months', 6)}")


def build_report(raw: dict, settings_path: str, home: str, data_dir: str = "/var/lib/my-booking") -> dict[str, list[cli_checks.Check]]:
    """All the check groups `setup` shows, computed once here so the
    printed and interactive modes (and `status`, for the ones it also
    reports) can't drift out of sync with each other."""
    config_paths = [settings_path, str(tmpl_path(home))]
    return {
        "secrets": cli_checks.check_secrets(raw),
        "rpmnew": cli_checks.check_rpmnew(config_paths),
        "group": cli_checks.check_group_membership(),
        "systemd": cli_checks.check_systemd(),
        "settings_fresh": cli_checks.check_settings_fresh(settings_path),
        "selinux": cli_checks.check_selinux(),
        "nginx_locations": cli_checks.check_nginx_locations(),
        "nginx_conf_repo_file": cli_checks.check_nginx_conf_repo_file(home),
        "nginx_conf_deployed": cli_checks.check_nginx_conf_deployed(raw),
        "static_site": cli_checks.check_static_site_drift(raw, tmpl_path(home)),
        "index_embedded_drift": cli_checks.check_index_embedded_drift(raw),
        "static_site_compliance": cli_checks.check_static_site_compliance(raw),
        "static_pages_deployed": cli_checks.check_static_pages_deployed(raw, home),
        "static_pages_reachable": cli_checks.check_static_pages_reachable(raw),
        "caldav_calendars": cli_checks.check_caldav_calendars(raw),
        "watchdog_nginx_log_config": cli_checks.check_watchdog_nginx_access_log_config(raw),
        "watchdog_nginx_access": cli_checks.check_watchdog_nginx_access(raw),
        "data_dir_git": cli_checks.check_data_dir_git(data_dir),
        "data_dir_ownership": cli_checks.check_data_dir_ownership(data_dir),
        "directory_fsync_support": cli_checks.check_directory_fsync_support(data_dir),
        "data_dir_group_selinux": cli_checks.check_path_group_and_selinux("data dir", data_dir),
        "log_file_group_selinux": (
            cli_checks.check_path_group_and_selinux("log file", log_file_path)
            # 2026-07-16: resolved from the already-parsed `raw` dict, NOT
            # config.peek_log_file(settings_path) -- that re-reads
            # settings_path off disk, which several tests here point at a
            # fake/incomplete path since build_report never used to touch
            # settings.toml itself beyond the `raw` it was handed. Same
            # value peek_log_file would return either way; None only when
            # log_file = "" explicitly disables file logging (absent key
            # = config.DEFAULT_LOG_FILE, since 2026-07-16 same day).
            if (log_file_path := config.log_file_from_raw(raw)) else []
        ),
        "static_site_dir_group_selinux": (
            cli_checks.check_path_group_and_selinux("static_site_dir", static_site_dir)
            if (static_site_dir := raw.get("site", {}).get("static_site_dir")) else []
        ),
        "maintenance": cli_checks.check_maintenance_mode(data_dir),
        "calendar_invite_format": cli_checks.check_calendar_invite_format(raw, data_dir),
        "calendar_invite_resync_skips": cli_checks.check_calendar_invite_resync_skips(data_dir),
        "active_sessions": cli_checks.check_active_sessions(),
        "csp_violations": cli_checks.check_csp_violations(raw),
        "csp_hashes_deployed": cli_checks.check_csp_hashes_deployed(raw),
    }


def print_report(
    raw: dict, settings_path: str, home: str,
    print_fn: Callable[[str], None] = print,
    data_dir: str = "/var/lib/my-booking",
) -> tuple[int, int]:
    """Prints the full setup report and returns (fails, warns) -- the same
    two counts `cmd_status` in scripts/my-bt already computes from its own
    flat `checks` list -- so plain `my-bt setup` (no `-i`) can exit
    non-zero on either, exactly like `my-bt status` already does
    (2026-07-10: the same summary-line + nonzero-exit behavior was added
    to plain `my-bt setup`, for consistency with `status` and so it's
    usable in a script/cron check too). Before this,
    plain `setup` always exited 0 and had no rollup line at all: every
    check was individually marked [OK]/[WARN]/[FAIL], so a human reading
    the whole report could tell, but there was nothing a script could
    check."""
    report = build_report(raw, settings_path, home, data_dir)

    def show(checks: list[cli_checks.Check]) -> None:
        for label, level, detail in checks:
            print_fn(f"   [{level.upper():4}] {label}" + (f" -- {detail}" if detail else ""))

    print_fn("my-booking-tool: setup steps (generated fresh from current state,")
    print_fn("full detail also in README.md)\n")

    # 2026-07-13: unlike every numbered step below (all read from
    # settings.toml/the filesystem), this one queries the LIVE running
    # process directly, same as `my-bt status` -- surfaced here,
    # unnumbered, right up front, because it's a WARNING about safety
    # (someone's actively using the site right now), not a config/install
    # check. A single compact line (report["active_sessions"]) drives the
    # warn/fail tally below; the full breakdown of who/since-when/timeout
    # is worth showing in full immediately under it.
    if report["active_sessions"]:
        show(report["active_sessions"])
        payload, _err = cli_checks.fetch_active_sessions()
        if payload:
            print_fn(cli_checks.format_active_sessions_overview(payload))
        print_fn("")

    print_fn("1. Secrets in /etc/my-booking/secrets/ (mode 600, owned by my-booking):")
    show(report["secrets"])
    print_fn("   Generate erasure_pepper with: openssl rand -hex 32")
    print_fn("   Generate admin_password_hash with: my-bt hash-password")
    print_fn("     my-bt hash-password | sudo tee /etc/my-booking/secrets/admin_password_hash")
    print_fn("   If written elsewhere and `mv`d in: sudo restorecon -Rv /etc/my-booking/secrets")

    print_fn("\n2. Pending .rpmnew merges (settings.toml, privacy.html.tmpl):")
    show(report["rpmnew"])

    print_fn(f"\n3. Review {settings_path}:")
    print_fn(f"   courses: {_course_summary(raw)}")
    print_fn(f"   {_privacy_summary(raw)}")

    print_fn("\n4. nginx location blocks (checked live via `nginx -T`):")
    show(report["nginx_locations"])
    if any(level != "ok" and label.startswith("nginx location ")
           for label, level, _ in report["nginx_locations"]):
        # If this checkout already has a real, complete personal vhost conf
        # (site/nginx-locations.conf -- see check_nginx_conf_repo_file()
        # just below), point at THAT instead of the bare generic packaged
        # example: it's your own already-hardened file, not a from-scratch
        # template to re-adapt (2026-07-10: this hint used to fire even
        # when a complete file already sat unused in site/, which is
        # confusing when it's already prepared and correct).
        if any(level == "ok" for _, level, _ in report["nginx_conf_repo_file"]):
            print_fn(f"   Add any missing block(s) from this checkout's own, already-complete")
            print_fn(f"   site/{cli_checks._NGINX_CONF_FILENAME} to your live vhost (not deployed")
            print_fn("   automatically -- see README.md \"Installing\"), then:")
            print_fn("   sudo nginx -t && sudo systemctl reload nginx")
        else:
            print_fn("   Add any missing block(s) from /opt/my-booking/site/my-booking.conf.example")
            print_fn("   to your existing vhost (not edited automatically -- see README.md \"Installing\"),")
            print_fn("   then: sudo nginx -t && sudo systemctl reload nginx")

    print_fn("\n   Real, personal nginx vhost conf kept in this checkout's site/ dir (if any):")
    show(report["nginx_conf_repo_file"])

    print_fn("\n   Real, DEPLOYED nginx vhost conf ([site].nginx_conf_path, read directly off disk):")
    if report["nginx_conf_deployed"]:
        show(report["nginx_conf_deployed"])
    else:
        print_fn("   [SKIP] [site].nginx_conf_path not configured -- not checked")

    print_fn("\n5. my-booking group membership:")
    show(report["group"])

    print_fn("\n6. Service + retention timer:")
    show(report["systemd"])
    show(report["settings_fresh"])

    print_fn("\n7. SELinux:")
    show(report["selinux"])

    print_fn("\n8. Static site (site/*.html on your live host):")
    static_site_checks = (
        report["static_site"] + report["index_embedded_drift"] + report["static_site_compliance"]
        + report["static_pages_deployed"] + report["static_pages_reachable"]
        + report["static_site_dir_group_selinux"]
    )
    if static_site_checks:
        show(static_site_checks)
    else:
        print_fn("   [SKIP] [site].static_site_dir not configured -- not checked")
    print_fn("   Swap each course's booking link to /book/<shortname> if you haven't.")

    print_fn("\n9. Calendars ([booking_calendar] + [[conflict_calendar]], checked live):")
    caldav_checks = report["caldav_calendars"]
    if caldav_checks:
        show(caldav_checks)
    else:
        print_fn("   [SKIP] caldav_url/username/password not fully configured yet -- not checked")

    print_fn("\n10. Watchdog: nginx access log (configured + readable by my-booking):")
    watchdog_checks = report["watchdog_nginx_log_config"] + report["watchdog_nginx_access"]
    if watchdog_checks:
        show(watchdog_checks)
    else:
        print_fn("   [SKIP] nginx not detected for this vhost -- not checked")

    print_fn("\n11. Data dir git snapshot (hourly auto-commit safety net) and file ownership:")
    show(report["data_dir_git"])
    show(report["data_dir_ownership"])
    show(report["directory_fsync_support"])
    show(report["data_dir_group_selinux"])

    print_fn("\n11d. Configured log file group/SELinux ([logging].log_file, if set):")
    if report["log_file_group_selinux"]:
        show(report["log_file_group_selinux"])
    else:
        print_fn('   [SKIP] file logging disabled (log_file = ""), or the file doesn\'t exist yet -- not checked')

    print_fn("\n12. Maintenance mode (`my-bt admin site-maintenance on/off/status`):")
    show(report["maintenance"])

    print_fn("\n13. Calendar invite format (marker file, no network call):")
    calendar_format_checks = report["calendar_invite_format"]
    if calendar_format_checks:
        show(calendar_format_checks)
    else:
        print_fn("   [SKIP] caldav_url/username/password not fully configured yet -- not checked")
    show(report["calendar_invite_resync_skips"])

    print_fn("\n14. CSP violations reported by browsers ([logging].log_file, if configured):")
    if report["csp_violations"]:
        show(report["csp_violations"])
    elif config.log_file_from_raw(raw):
        print_fn("   [OK  ] none in the last [watchdog].window_minutes")
    else:
        print_fn('   [SKIP] file logging disabled (log_file = "") -- not checked')

    # 2026-07-13: the PROACTIVE half of the CSP-hash automation (contrast
    # step 14 above, which is purely reactive -- it can only ever surface a
    # stale hash AFTER a real browser has hit it and reported back). This
    # instead computes every inline <script> hash the app can currently
    # produce from source (app.cli_checks.expected_csp_hashes()) and
    # compares it against what's actually allow-listed in the live nginx
    # CSP header right now -- built after this exact class of bug (a
    # forgotten hash update) hit production four separate times (see
    # site/nginx-locations.conf's own dated incident notes).
    print_fn("\n15. CSP script hashes deployed (checked live via `nginx -T`):")
    if report["csp_hashes_deployed"]:
        show(report["csp_hashes_deployed"])
    else:
        print_fn("   [SKIP] nginx/the matching vhost/its CSP header not detected -- not checked")

    print_fn("\nRun `my-bt setup --interactive` to be walked through what's left.")

    all_checks = [c for group in report.values() for c in group]
    fails = sum(1 for _, level, _ in all_checks if level == "fail")
    warns = sum(1 for _, level, _ in all_checks if level == "warn")
    problems = cli_checks.summarize_problems(all_checks)
    if problems:
        print_fn("\nWarnings/failures, repeated from above:")
        for line in problems:
            print_fn(f"   {line}")
    if fails:
        print_fn(f"\n{fails} problem(s), {warns} warning(s) -- fix the FAIL item(s) above, "
                  "then re-run `my-bt setup` (or `my-bt status`).")
    elif warns:
        print_fn(f"\n{warns} warning(s), no hard failures -- worth a look above.")
    else:
        print_fn("\nall checks passed -- nothing left to do.")
    return fails, warns


def interactive_setup(
    raw: dict,
    settings_path: str,
    home: str,
    *,
    prompt: Callable[[str], bool] = _default_prompt,
    read_secret: Callable[[str], str] = _default_read_secret,
    run: Callable[[list[str]], None] | None = None,
    is_root: Callable[[], bool] = _default_is_root,
    print_fn: Callable[[str], None] = print,
    data_dir: str = "/var/lib/my-booking",
    check_active_sessions: Callable[[], tuple[int | None, str | None]] = _default_check_active_sessions,
    run_nginx_test: Callable[[], bool] = _default_run_nginx_test,
) -> tuple[int, int]:
    """Runs the interactive walkthrough and returns (fails, warns) -- the
    same CURRENT-state counts (after whatever this walkthrough just fixed)
    the closing "Done -- ..." line already prints -- so `cmd_setup` in
    scripts/my-bt can exit non-zero when either is nonzero, exactly like
    plain `setup` and `status` already do. Before this, `my-bt setup -i`
    always exited 0 regardless of what its own closing line said, so
    `my-bt setup -i && <next step>` silently ran `<next step>` even when
    the walkthrough's own summary said "N problem(s) ... still need
    attention" (hit in practice 2026-07-10: `my-bt setup -i && my-bt
    status` ran status unconditionally)."""
    if run is None:
        run = lambda cmd: _run_tolerant(cmd, print_fn)  # noqa: E731

    # 2026-07-13: refuse the WHOLE walkthrough outright while anyone's
    # actively logged in, mirroring the RPM's own %pre gate before an
    # upgrade (packaging/my-booking-tool.spec) -- before this, only step
    # 6's own "Restart my-booking.service now?" prompt (below) had any
    # session-awareness at all, so a run where settings.toml was already
    # fresh (nothing to restart) sailed straight through every other step
    # -- nginx reload, systemd enable, group membership, SELinux -- with
    # someone live on the site, even though the RPM would have refused an
    # upgrade in the exact same situation. Uses the raw payload (not just
    # a count, unlike check_active_sessions above) so the refusal can show
    # WHO exactly is logged in, not just how many.
    sessions_payload, _sessions_err = cli_checks.fetch_active_sessions()
    if sessions_payload and sessions_payload.get("sessions"):
        n = len(sessions_payload["sessions"])
        print_fn(f"my-bt setup --interactive: refusing to proceed -- {n} active session(s) right now.\n")
        print_fn(cli_checks.format_active_sessions_overview(sessions_payload))
        print_fn("\nWait for them to log out/expire (see `my-bt status`), or force-clear them")
        print_fn("yourself with the command above, then re-run `my-bt setup -i`.")
        return 1, 0

    print_fn("my-bt setup --interactive: walking through remaining steps.")
    print_fn("Already-done steps are shown and skipped without asking.\n")

    # 1. secrets
    print_fn("-- 1. Secrets --")
    for name, path_str in cli_checks.secret_file_map(raw).items():
        if not path_str:
            print_fn(f"[skip] {name}: not configured in settings.toml")
            continue
        p = Path(path_str)
        if p.exists() and p.read_text(encoding="utf-8", errors="replace").strip():
            print_fn(f"[ok] {name} already present ({path_str})")
            continue
        print_fn(f"\n{name} ({path_str}) is missing.")
        value = None
        if name == "admin_password_hash":
            if prompt("Set the admin password now?"):
                pw1 = read_secret("Admin password")
                pw2 = read_secret("Confirm")
                if pw1 and pw1 == pw2:
                    value = app_security.hash_admin_password(pw1)
                else:
                    print_fn("passwords empty or didn't match -- skipped")
        elif name == "erasure_pepper":
            if prompt("Generate a random erasure_pepper now?"):
                value = _secrets.token_hex(32)
        else:
            if prompt(f"Enter {name} now?"):
                value = read_secret(name).strip() or None
        if value is None:
            print_fn(f"[skip] {name}")
            continue
        try:
            # 2026-07-15: atomic_write_text (also makes the parent dir),
            # not a bare write_text() -- a torn write here is a secret
            # file (e.g. erasure_pepper), and a half-written one is worse
            # than a missing one. See app/atomic_io.py. chmod/chown below
            # apply to the real path AFTER the write, same as before --
            # atomic_write_text's rename doesn't preserve them.
            atomic_write_text(p, value + "\n")
            p.chmod(0o600)
            if is_root():
                import grp
                import pwd
                try:
                    os.chown(p, pwd.getpwnam("my-booking").pw_uid, grp.getgrnam("my-booking").gr_gid)
                except KeyError:
                    pass
            print_fn(f"[ok] wrote {path_str}")
        except OSError as exc:
            print_fn(f"[fail] could not write {path_str}: {exc}")

    # 2. rpmnew merges
    print_fn("\n-- 2. Pending .rpmnew merges --")
    for path_str in (settings_path, str(tmpl_path(home))):
        rpmnew = Path(path_str + ".rpmnew")
        if not rpmnew.exists():
            print_fn(f"[ok] {Path(path_str).name}.rpmnew: none pending")
            continue
        print_fn(f"{rpmnew} exists: a newer packaged version is waiting to be merged.")
        if prompt(f"Open vimdiff {path_str} {rpmnew} now?"):
            run(["vimdiff", path_str, str(rpmnew)])
            if prompt(f"Merged? Remove {rpmnew} now?"):
                rpmnew.unlink()

    # 3. settings.toml review
    print_fn(f"\n-- 3. {settings_path} current values (review, edit by hand if needed) --")
    print_fn(f"   courses: {_course_summary(raw)}")
    print_fn(f"   {_privacy_summary(raw)}")

    # 4. nginx -- checked live via `nginx -T` (the fully merged config, not
    # a guessed single vhost file); never edited automatically -- rewriting
    # a stranger's hand-maintained nginx config would be worse than asking.
    print_fn("\n-- 4. nginx --")
    nginx_checks = cli_checks.check_nginx_locations()
    missing_locations = [
        label for label, level, _ in nginx_checks
        if level != "ok" and label.startswith("nginx location ")
    ]
    for label, level, detail in nginx_checks:
        print_fn(f"[{level}] {label}: {detail}")
    repo_file_checks = cli_checks.check_nginx_conf_repo_file(home)
    repo_file_ok = any(level == "ok" for _, level, _ in repo_file_checks)
    if missing_locations:
        # Point at this checkout's own real, already-complete vhost conf
        # (checked just below) instead of the bare generic packaged example
        # when one exists -- it's already hardened and has every block this
        # app needs, not a from-scratch template to re-adapt (2026-07-10:
        # this hint used to fire even when a complete file already sat
        # unused in site/, which is confusing when it's already prepared
        # and correct).
        print_fn("Add the missing location block(s) above from")
        if repo_file_ok:
            print_fn(f"  this checkout's own site/{cli_checks._NGINX_CONF_FILENAME} (already complete)")
        else:
            print_fn("  /opt/my-booking/site/my-booking.conf.example")
        print_fn("to your existing vhost (not automated -- this would mean")
        print_fn("guessing at and editing your hand-maintained nginx config).")
    # 2026-07-13: the reload prompt used to live right here, BEFORE the
    # [site].nginx_conf_path vimdiff-reconciliation block below ever runs
    # -- so a real run could reload nginx with the OLD on-disk content,
    # THEN immediately be offered a vimdiff merge that changes that same
    # file (e.g. adding a new CSP script-src hash), landing on disk only
    # AFTER the reload already happened. The 2026-07-13 patch before this
    # one added a second, follow-up reload prompt right after that vimdiff
    # to paper over the gap, but that just meant TWO separate reload
    # prompts in one run, in a confusing order (reload, then a merge that
    # itself asks to reload again) -- exactly what the operator flagged from a
    # real run ("vimdiff still happens after nginx restart"). Fixed
    # properly this time: no reload prompt here at all -- one single,
    # unconditional prompt at the very end of this whole nginx section
    # (below), asked only once every possible content change (location-
    # block hint above, plus any vimdiff merge/rename below) is already
    # known, so accepting it always reloads the FINAL, fully-reconciled
    # config, not a stale snapshot from partway through this section.
    nginx_content_changed = False

    # Real, personal nginx vhost conf kept directly in this checkout's
    # site/ dir at the fixed name site/nginx-locations.conf -- informational
    # only, same reasoning as the CalDAV calendar checks below: this tool
    # never guesses at and edits your own hand-hardened vhost file for you,
    # it just surfaces whether it's missing required location blocks or a
    # leftover REPLACE-ME placeholder.
    for label, level, detail in repo_file_checks:
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
        else:
            print_fn(f"[{level}] {label}: {detail}")

    # Real, DEPLOYED nginx vhost conf at [site].nginx_conf_path, if
    # configured -- read directly off disk (not `nginx -T`'s merged dump),
    # so this reflects exactly what's on this box right now, and works
    # even before nginx is reloaded or if the nginx binary isn't
    # reachable at all. A configured-but-broken file here is a hard FAIL,
    # not a warning (2026-07-10: this path can actually be checked for
    # correctness, so a real problem should truly ERROR out) -- unlike
    # check_nginx_conf_repo_file() above, setting
    # this path is a deliberate statement that this exact file matters.
    # Still never auto-edited -- rewriting a hand-hardened vhost would be
    # worse than asking -- but if this checkout has its own copy (real or
    # .example, matched by filename) and it differs from what's actually
    # deployed, offer the same vimdiff-to-reconcile pattern already used
    # for .rpmnew merges and stale hand-authored static pages. The checkout
    # side is always the one fixed site/nginx-locations.conf(.example) --
    # unrelated to whatever nginx_conf_path itself is named on the live
    # server (2026-07-10: fixed filename convention so all content in
    # site/ works the same way).
    nginx_conf_path = raw.get("site", {}).get("nginx_conf_path")
    if not nginx_conf_path:
        print_fn("[skip] [site].nginx_conf_path not configured -- not checked")
    else:
        deployed = Path(nginx_conf_path)
        # Nothing at the configured path yet? `nginx -T`'s own
        # "# configuration file <path>:" markers can reveal this vhost is
        # actually still deployed under a DIFFERENT (e.g. pre-rename)
        # filename -- exactly the gap hit right after a real
        # site/booking.example.org.conf -> site/nginx-locations.conf rename: the
        # real file still needed a manual `sudo mv`, and until now there
        # was nothing for this tool to point at, let alone fix
        # (2026-07-10: added so `my-bt setup -i` can fix this directly).
        live_file = None
        if not deployed.exists():
            candidate = cli_checks._live_nginx_conf_file_for_host(raw)
            if candidate is not None and candidate.exists() and candidate.resolve() != deployed.resolve():
                live_file = candidate

        for label, level, detail in cli_checks.check_nginx_conf_deployed(raw):
            print_fn(f"[{level}] {label}: {detail}")

        # nginx doesn't care what a conf.d file is called -- the live file
        # having a different name than [site].nginx_conf_path isn't itself
        # wrong (_resolve_nginx_conf_checkout_source()'s own docstring
        # already says as much: the checkout side is deliberately
        # independent of this too). So the FIRST, lowest-risk offer here is
        # just correcting the SETTING to point at reality, not touching the
        # live file at all (2026-07-10: an earlier version of this step
        # only ever offered to rename the live file, but settings.toml
        # should instead be told which file is actually in use -- renaming
        # an already-working, hand-hardened vhost file is real risk for no
        # benefit if the setting can simply be corrected instead). Renaming
        # the file to match the setting is still offered further down, for
        # anyone who genuinely wants that convention enforced on the server
        # too -- just no longer the only option, and no longer asked first.
        if live_file is not None and prompt(
            f"[site].nginx_conf_path doesn't match a real file, but nginx is loading this vhost "
            f"from {live_file} -- point nginx_conf_path there instead of renaming the file?"
        ):
            try:
                _write_nginx_conf_path_setting(settings_path, str(live_file))
                raw.setdefault("site", {})["nginx_conf_path"] = str(live_file)
                deployed = live_file
                print_fn(f"[ok] updated [site].nginx_conf_path to {live_file}")
                live_file = None  # resolved -- nothing left to rename
            except OSError as exc:
                print_fn(f"[fail] could not write {settings_path}: {exc}")

        # Reconcile CONTENT next, against whichever file is actually live
        # right now (the configured path if it exists -- possibly just
        # updated above -- else the old-named one detected earlier) --
        # still never auto-written, just the same vimdiff offer already
        # used for .rpmnew merges and stale hand-authored static pages, so
        # a location block that's in the checkout but was never deployed
        # (e.g. /reinstate, /host-reinstate) gets caught regardless of what
        # the live file happens to be named.
        source = cli_checks._resolve_nginx_conf_checkout_source(home)
        live_now = deployed if deployed.exists() else live_file
        if source is not None and live_now is not None:
            same = live_now.read_text(encoding="utf-8", errors="replace") == \
                source.read_text(encoding="utf-8", errors="replace")
            if not same and prompt(f"Open vimdiff {live_now} {source} now?"):
                run(["vimdiff", str(live_now), str(source)])
                # 2026-07-13: no reload prompt here -- see the
                # nginx_content_changed comment above step 4's location
                # checks for why. Just record that the on-disk config may
                # have changed; the ONE reload prompt for this whole
                # section fires once, at the very end, after this AND the
                # rename branch below have both had a chance to run.
                nginx_content_changed = True

        # CSP hash self-heal (2026-07-16, the operator: "can we automate and fix
        # this within the build or setup so that the setup does not break
        # out with an error but maybe can self-heal?"). Runs against
        # whichever file is actually live right now, same as the vimdiff
        # reconciliation just above. check_csp_hashes_deployed() itself
        # stays read-only (still used as-is by plain `status`/`health`,
        # and by the non-interactive branch of this same walkthrough) --
        # this is the ONE place that actually edits the live CSP header,
        # gated behind an explicit prompt, additive-only
        # (csp_script_src_patch()), and verified with a REAL `nginx -t`
        # pass/fail check (run_nginx_test(), not the fire-and-forget `run`
        # used elsewhere in this section, which throws its exit code away)
        # before the edit is ever kept -- reverted to the original content
        # immediately if that check fails, so this can never leave a
        # broken CSP header on a live web server. Deliberately still
        # prompt-gated even though the location-block hint above and the
        # vimdiff/rename branches around it already imply a fair amount of
        # trust in this walkthrough -- a bad CSP edit breaks every inline
        # script on the site at once, not just one page, so this is the
        # one nginx change in this whole section that stays opt-in no
        # matter what.
        live_for_csp = deployed if deployed.exists() else live_file
        if live_for_csp is not None and live_for_csp.exists():
            csp_warn_detail = next(
                (detail for _, level, detail in cli_checks.check_csp_hashes_deployed(raw) if level == "warn"),
                None,
            )
            if csp_warn_detail and prompt(
                f"CSP script hashes deployed: {csp_warn_detail} -- add the missing hash(es) to "
                f"{live_for_csp} and verify with `nginx -t` now?"
            ):
                expected = cli_checks.expected_csp_hashes(raw)
                currently_deployed = cli_checks._nginx_csp_script_hashes(raw) or set()
                to_add = sorted({h for h in expected.values() if h not in currently_deployed})
                original_text = live_for_csp.read_text(encoding="utf-8")
                try:
                    patched_text = cli_checks.csp_script_src_patch(original_text, to_add)
                except ValueError as exc:
                    print_fn(f"[fail] couldn't patch {live_for_csp}: {exc} -- left untouched")
                else:
                    atomic_write_text(live_for_csp, patched_text)
                    if run_nginx_test():
                        print_fn(f"[ok] added {len(to_add)} CSP hash(es) to {live_for_csp} -- nginx -t passed")
                        nginx_content_changed = True
                    else:
                        atomic_write_text(live_for_csp, original_text)
                        print_fn(
                            f"[fail] `nginx -t` did not pass after adding {len(to_add)} hash(es) -- "
                            f"reverted {live_for_csp} to its original content, nothing was left broken"
                        )

        # Renaming into place: the alternative fix, only still relevant if
        # you didn't just update the setting above. Unlike that offer, this
        # one does need root (it's touching a file outside anything this
        # tool/its group otherwise owns).
        if live_file is not None:
            if not is_root():
                print_fn("(needs root to rename into place -- re-run `sudo my-bt setup -i`)")
            elif prompt(f"Rename {live_file} to {deployed} now?"):
                try:
                    live_file.rename(deployed)
                    print_fn(f"[ok] renamed {live_file} -> {deployed}")
                    nginx_content_changed = True
                except OSError as exc:
                    print_fn(f"[fail] could not rename {live_file}: {exc}")

    # ONE reload prompt for the whole nginx section, asked here at the end
    # -- after the location-block hint above AND any vimdiff-merge/rename
    # against [site].nginx_conf_path just above have all already had their
    # chance to change what's actually on disk -- so accepting this always
    # reloads the fully-reconciled config, never a stale mid-section
    # snapshot. See nginx_content_changed's own comment near the top of
    # step 4 for the real run that prompted this reordering.
    reload_prompt = (
        "Run `nginx -t && systemctl reload nginx` to pick up these changes now?"
        if nginx_content_changed else "Run `nginx -t && systemctl reload nginx` now?"
    )
    if prompt(reload_prompt):
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])

    # 5. group membership
    print_fn("\n-- 5. my-booking group membership --")
    label, level, detail = cli_checks.check_group_membership()[0]
    if level == "ok":
        print_fn(f"[ok] {label}")
    else:
        print_fn(detail)
        if is_root() and prompt("Run that usermod now?"):
            target = os.environ.get("SUDO_USER") or getpass.getuser()
            run(["usermod", "-aG", "my-booking", target])
        elif not is_root():
            print_fn("(needs root -- re-run `sudo my-bt setup -i` to have this done for you)")

    # 6. systemd
    print_fn("\n-- 6. Service + retention timer --")
    for unit, level, detail in cli_checks.check_systemd():
        if level == "ok":
            print_fn(f"[ok] {unit}: {detail}")
        else:
            print_fn(f"{unit}: {detail}")
            if is_root() and prompt(f"Enable+start {unit} now?"):
                run(["systemctl", "enable", "--now", unit])
            elif not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")

    # settings.toml can be edited on disk without the running service ever
    # noticing (see check_settings_fresh()'s docstring) -- flag it right
    # here, next to the service's own status, and offer the one-line fix.
    for label, level, detail in cli_checks.check_settings_fresh(settings_path):
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
        else:
            print_fn(f"{label}: {detail}")
            if "aren't live yet" in detail:
                # 2026-07-10: unlike the RPM's own %pre gate before
                # an upgrade, this restart used to have NO session-
                # awareness at all -- SESSIONS is in-memory (see
                # app/webapp.py's module docstring), so restarting here
                # silently logs out anyone currently logged in. Refuse the
                # same way %pre does, but tell you exactly how to proceed:
                # `my-bt admin logout` clears sessions on purpose, then a
                # re-run of `setup -i` picks this restart back up.
                sessions, _err = check_active_sessions()
                if sessions:
                    print_fn(
                        f"[blocked] {sessions} active session(s) right now -- restarting "
                        "would log them all out. Run `my-bt admin logout EMAIL` (one "
                        "attendee) or `my-bt admin logout --all` (everyone), then "
                        "re-run `my-bt setup -i` to actually apply this restart."
                    )
                elif is_root() and prompt("Restart my-booking.service now?"):
                    run(["systemctl", "restart", "my-booking.service"])
                elif not is_root():
                    print_fn("(needs root -- re-run `sudo my-bt setup -i`)")

    # 7. SELinux
    print_fn("\n-- 7. SELinux --")
    for label, level, detail in cli_checks.check_selinux():
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
        else:
            print_fn(f"{label}: {detail}")
            if "httpd_can_network_connect" in label and is_root() and prompt("Run setsebool now?"):
                run(["setsebool", "-P", "httpd_can_network_connect", "on"])
            elif not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")

    # 8. static site
    print_fn("\n-- 8. Static site --")
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        print_fn("[site].static_site_dir not configured -- skipping live-page checks.")
        print_fn("Copy site/*.html to your live host and swap each course's booking")
        print_fn("link to /book/<shortname> (separate checkout, manual on purpose).")
    else:
        t = tmpl_path(home)
        out_path = Path(static_site_dir) / site_render.OUTPUT_NAME
        drift_checks = cli_checks.check_static_site_drift(raw, t)
        for label, level, detail in drift_checks:
            print_fn(f"[{level}] {label}: {detail}")
        # Compliance (leftover REPLACE-ME/${...} placeholders) is
        # informational only here -- it's about YOUR wording, which this
        # tool never edits for you, so there's nothing to prompt/act on
        # beyond surfacing it.
        for label, level, detail in cli_checks.check_static_site_compliance(raw):
            if level != "ok":
                print_fn(f"[{level}] {label}: {detail}")
        # Only ask if there's actually something to regenerate -- unlike
        # nginx's reload prompt (which stays unconditional because
        # `nginx -T` reflects disk, not what the running process has
        # loaded), this check re-renders from the CURRENT settings.toml
        # and template and compares those exact bytes against what's
        # deployed: "matches" here means regenerating would write the
        # identical bytes again, so asking anyway was pure noise.
        needs_regen = any(level != "ok" for _, level, _ in drift_checks)
        if needs_regen and prompt(f"(Re)generate {out_path} from current settings.toml now?"):
            privacy = raw.get("privacy", {})
            try:
                site_render.write_privacy_html(
                    t, privacy.get("retention_months", 24),
                    privacy.get("canceled_retention_months", 6), out_path,
                )
                print_fn(f"[ok] wrote {out_path}")
            except OSError as exc:
                print_fn(f"[fail] could not write {out_path}: {exc}")

        # index.html/impressum.html/terms.html: hand-authored, so never
        # auto-copied silently -- but actively OFFER to act on each one
        # your checkout has a real (or .example) source for, instead of
        # just reporting a problem and leaving you to fix it yourself.
        # Staying silent here was the actual bug (see the maintainer's
        # local notes).
        # Two different offers depending on the situation: if nothing's
        # deployed yet, there's nothing to lose by a straight copy; but if
        # BOTH sides already have real content that differs, blindly
        # overwriting either one could throw content away -- open vimdiff
        # instead (same reasoning/pattern as the .rpmnew merge above) and
        # let you reconcile and save by hand.
        for label, level, detail in cli_checks.check_static_pages_deployed(raw, home):
            print_fn(f"[{level}] {label}: {detail}")
        for name in cli_checks._STATIC_PAGES_TO_DEPLOY:
            source = cli_checks._resolve_static_source(home, name)
            if source is None:
                continue  # nothing in the checkout to offer copying from
            deployed = Path(static_site_dir) / name
            if not deployed.exists():
                if prompt(f"Copy {source} to {deployed} now?"):
                    try:
                        # 2026-07-15: atomic_write_text, not a bare
                        # write_text() -- this is a live, publicly-served
                        # page. See app/atomic_io.py.
                        atomic_write_text(deployed, source.read_text(encoding="utf-8"))
                        print_fn(f"[ok] wrote {deployed}")
                    except OSError as exc:
                        print_fn(f"[fail] could not write {deployed}: {exc}")
                continue
            # cli_checks._diffable_static_page_text() strips
            # `my-bt admin site-maintenance on`'s banner block before comparing
            # (2026-07-10: `setup -i` should know about maintenance mode
            # and ignore any change caused only by that banner, rather
            # than proposing a vimdiff for it) -- without it, index.html looks permanently
            # "different" from the checkout for as long as maintenance mode
            # stays on, offering a pointless vimdiff every single run.
            same = cli_checks._diffable_static_page_text(deployed.read_text(encoding="utf-8", errors="replace")) == \
                cli_checks._diffable_static_page_text(source.read_text(encoding="utf-8", errors="replace"))
            if same:
                continue  # already in sync -- already reported "[ok]" above
            if prompt(f"Open vimdiff {deployed} {source} now?"):
                run(["vimdiff", str(deployed), str(source)])

        # index_embedded.html: DERIVED fresh from the LIVE index.html above
        # (not this checkout's own copy -- the live file is authoritative
        # for whatever's actually being embedded right now) plus current
        # settings.toml, gated on [site].index_embedded_enabled (off by
        # default -- most deployments don't embed their site via <iframe>
        # elsewhere). Runs AFTER the index.html copy/vimdiff loop just
        # above on purpose: any index.html drift is reconciled first, so
        # this always derives from the version you just confirmed is
        # correct. Same copy-if-missing/vimdiff-if-different UX as
        # index.html itself -- NOT a separate generated-page mechanism, see
        # app.site_render.derive_index_embedded_html's own docstring.
        if raw.get("site", {}).get("index_embedded_enabled"):
            live_index_path = Path(static_site_dir) / "index.html"
            if not live_index_path.exists():
                print_fn(f"[skip] index_embedded.html: {live_index_path} isn't deployed yet -- "
                         "nothing to derive it from")
            else:
                from . import config as app_config

                courses = app_config.courses_from_raw(raw)
                today = app_config.today_in_raw_timezone(raw)
                base_url = (raw.get("site", {}).get("base_url") or "").rstrip("/")
                new_tab_links = bool(raw.get("site", {}).get("index_embedded_new_tab_links", True))
                custom_attention_message = raw.get("site", {}).get("custom_attention_message", "")
                derived = None
                try:
                    derived = site_render.derive_index_embedded_html(
                        live_index_path.read_text(encoding="utf-8", errors="replace"),
                        courses, today, base_url, new_tab_links, custom_attention_message,
                    )
                except site_render.IndexEmbeddedDerivationError as exc:
                    print_fn(f"[fail] index_embedded.html: {exc}")

                if derived is not None:
                    embedded_out_path = Path(static_site_dir) / site_render.EMBEDDED_OUTPUT_NAME
                    if not embedded_out_path.exists():
                        if prompt(f"Derive and copy {embedded_out_path} from index.html now?"):
                            try:
                                atomic_write_text(embedded_out_path, derived)
                                print_fn(f"[ok] wrote {embedded_out_path}")
                            except OSError as exc:
                                print_fn(f"[fail] could not write {embedded_out_path}: {exc}")
                    else:
                        deployed_text = embedded_out_path.read_text(encoding="utf-8", errors="replace")
                        same = cli_checks._diffable_static_page_text(deployed_text) == \
                            cli_checks._diffable_static_page_text(derived)
                        if same:
                            print_fn(f"[ok] {embedded_out_path} matches current index.html + settings.toml")
                        elif prompt(
                            f"{embedded_out_path} doesn't match what index.html + settings.toml would "
                            "currently derive -- open vimdiff against the freshly-derived version now?"
                        ):
                            tmp_fd, tmp_path_str = tempfile.mkstemp(
                                prefix="index_embedded.derived.", suffix=".html"
                            )
                            tmp_path = Path(tmp_path_str)
                            try:
                                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                                    f.write(derived)
                                run(["vimdiff", str(embedded_out_path), str(tmp_path)])
                            finally:
                                tmp_path.unlink(missing_ok=True)

        # Reachability from nginx's actual root -- a per-file symlink, never
        # a static_site_dir rewrite (see check_static_pages_reachable()'s
        # own comment: some setups keep static_site_dir as a separate
        # git-tracked staging dir on purpose, symlinking in only what's
        # meant to be public).
        for label, level, detail in cli_checks.check_static_pages_reachable(raw):
            if level == "ok":
                print_fn(f"[ok] {label}: {detail}")
                continue
            print_fn(f"{label}: {detail}")
            name = label.split(": ", 1)[1]
            nginx_root = cli_checks._nginx_root_for_host(raw)
            if nginx_root is None:
                continue
            link_path = Path(nginx_root) / name
            target = Path(static_site_dir) / name
            if not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
            elif prompt(f"Symlink {link_path} -> {target} now?"):
                try:
                    link_path.symlink_to(target)
                    print_fn(f"[ok] symlinked {link_path} -> {target}")
                except OSError as exc:
                    print_fn(f"[fail] could not symlink {link_path}: {exc}")

    # 9. CalDAV calendars -- live PROPFIND, informational only. There's no
    # safe auto-fix for a naming mismatch (this tool has no way to know
    # which real calendar on your CalDAV server you *meant*), so unlike
    # the other steps this one never prompts/acts -- it just surfaces
    # exactly what check_caldav_calendars() found, the same as
    # print_report()'s step 9, so `status`/`setup`/`setup -i` never drift
    # out of sync with each other about this.
    print_fn("\n-- 9. Calendars ([booking_calendar] + [[conflict_calendar]], checked live) --")
    caldav_checks = cli_checks.check_caldav_calendars(raw)
    if not caldav_checks:
        print_fn("[skip] caldav_url/username/password not fully configured yet -- not checked")
    else:
        for label, level, detail in caldav_checks:
            print_fn(f"[{level}] {label}: {detail}")
        if any(level != "ok" for _, level, _ in caldav_checks):
            print_fn(f"Fix by editing {settings_path}'s [booking_calendar] / ")
            print_fn("[[conflict_calendar]] entries to match a real calendar/feed on the server")
            print_fn("(every /book/<shortname> page 500s until this matches).")

    # 10. Watchdog: nginx_access_log detection + read access. The `acl`
    # package (setfacl) is deliberately NOT an RPM Requires -- it's only
    # needed if you opt into the nginx-burst check at all, so making every
    # install pull it in unconditionally would be the wrong trade-off for
    # people who never touch this setting. Detect it's missing here and
    # say so plainly instead of just letting the command silently fail.
    print_fn("\n-- 10. Watchdog: nginx access log --")
    detected = cli_checks._nginx_access_log_for_host(raw)
    configured = raw.get("watchdog", {}).get("nginx_access_log")
    if not configured and detected:
        print_fn(f"nginx_access_log not set -- nginx's live config for this vhost logs to {detected}.")
        if prompt(f'Add nginx_access_log = "{detected}" to settings.toml now?'):
            try:
                _add_nginx_access_log_setting(settings_path, detected)
                raw.setdefault("watchdog", {})["nginx_access_log"] = detected
                configured = detected
                print_fn(f"[ok] wrote nginx_access_log to {settings_path}")
            except OSError as exc:
                print_fn(f"[fail] could not write {settings_path}: {exc}")
    elif configured and detected and Path(configured).resolve() != Path(detected).resolve():
        print_fn(f"[warn] settings.toml has nginx_access_log = {configured}, but nginx's live "
                 f"config for this vhost logs to {detected}.")
        if prompt(f'Update nginx_access_log to "{detected}" in settings.toml now?'):
            try:
                _add_nginx_access_log_setting(settings_path, detected)
                raw.setdefault("watchdog", {})["nginx_access_log"] = detected
                configured = detected
                print_fn(f"[ok] updated nginx_access_log in {settings_path}")
            except OSError as exc:
                print_fn(f"[fail] could not write {settings_path}: {exc}")

    watchdog_checks = cli_checks.check_watchdog_nginx_access(raw)
    if not watchdog_checks:
        print_fn("[skip] [watchdog].nginx_access_log not configured -- read-access not checked")
    else:
        for label, level, detail in watchdog_checks:
            if level == "ok":
                print_fn(f"[ok] {label}: {detail}")
                continue
            print_fn(f"{label}: {detail}")
            if "user 'my-booking' doesn't exist" in detail:
                continue  # nothing to fix here yet -- install the package first
            log_path = Path(raw["watchdog"]["nginx_access_log"])
            if not log_path.exists():
                continue  # bad path -- fix settings.toml by hand, nothing to setfacl
            if not shutil.which("setfacl"):
                print_fn("setfacl not found -- install it first (e.g. `sudo dnf install acl`), "
                         "then re-run `sudo my-bt setup -i`.")
                continue
            if not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
            elif prompt(f"Run setfacl to grant my-booking read access under {log_path.parent} now?"):
                run(["setfacl", "-R", "-m", "u:my-booking:rX", str(log_path.parent)])
                run(["setfacl", "-d", "-m", "u:my-booking:rX", str(log_path.parent)])

    # 11. Data dir git snapshot -- unlike the systemd/SELinux offers above,
    # this deliberately does NOT gate on is_root(): initializing a git repo
    # only needs filesystem write access to `data_dir`, which a member of
    # the `my-booking` group already has (see README.md "Installing" step
    # 4) -- there's no privileged operation involved, so requiring root
    # here would just be an unnecessary hurdle. `git init`/`config` are run
    # directly via subprocess here (NOT through the injected `run`
    # callable) -- unlike vimdiff/systemctl/setsebool/setfacl above, whose
    # tests only care THAT they were invoked with the right args, this step
    # needs the repo to genuinely exist on disk afterwards so the
    # subsequent app.git_snapshot.snapshot() call (which insists on a real
    # `.git`, see that module's docstring) actually finds one -- a faked,
    # recording-only `run` in tests would otherwise silently no-op `git
    # init` and make every test here report "not_a_repo" regardless of
    # what's being exercised. The actual add+commit is still delegated to
    # snapshot() itself, so there's exactly one place that owns "how to
    # stage and commit."
    print_fn("\n-- 11. Data dir git snapshot --")
    data_dir_checks = cli_checks.check_data_dir_git(data_dir)
    for label, level, detail in data_dir_checks:
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
            continue
        print_fn(f"{label}: {detail}")
        if prompt("Initialize a git repo for the data directory now?"):
            from . import git_snapshot as app_git_snapshot

            data_dir_path = Path(data_dir)
            try:
                data_dir_path.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init", str(data_dir_path)], capture_output=True, text=True)
                subprocess.run(
                    ["git", "-C", str(data_dir_path), "config", "user.email", "my-booking-tool <noreply@localhost>"],
                    capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "-C", str(data_dir_path), "config", "user.name", "my-booking-tool"],
                    capture_output=True, text=True,
                )
                gitignore = data_dir_path / ".gitignore"
                if not gitignore.exists():
                    atomic_write_text(gitignore, "*.tmp\n")
                result = app_git_snapshot.snapshot(data_dir_path)
                print_fn(f"[ok] initialized git repo at {data_dir_path} ({result.detail})")
            except OSError as exc:
                print_fn(f"[fail] could not initialize git repo at {data_dir_path}: {exc}")

    # 11b. Data dir file ownership -- see cli_checks.check_data_dir_ownership's
    # own docstring for the real 2026-07-08 incident this closes (a root-run
    # migration script left users.csv/registrations.csv root-owned, mode
    # 0600, so the my-booking service got PermissionError on its very next
    # read -- a live GET /admin 500). UNLIKE the git-snapshot step just
    # above, this DOES gate on is_root(): chown-ing a file to a DIFFERENT
    # system user (my-booking) is a genuinely privileged operation, not
    # something a mere my-booking-group member can do -- no point prompting
    # for something that would just fail.
    print_fn("\n-- 11b. Data dir file ownership --")
    ownership_checks = cli_checks.check_data_dir_ownership(data_dir)
    for label, level, detail in ownership_checks:
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
            continue
        print_fn(f"{label}: {detail}")
        if level != "fail":
            continue  # "warn" here means the my-booking user doesn't exist yet -- nothing to chown
        if not is_root():
            print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
            continue
        if prompt("Fix ownership now (chown my-booking:my-booking)?"):
            mismatched = sorted(Path(data_dir).glob("*.csv"))
            run(["chown", "my-booking:my-booking", *[str(p) for p in mismatched]])
            print_fn("[ok] ownership fixed")

    # 11c. Directory fsync support -- informational only, same reasoning
    # as maintenance mode/CalDAV below: there's nothing `my-bt` can fix
    # here (an unsupported mount is an OS/infrastructure fact, not a
    # misconfiguration this tool caused or can chown/edit its way out
    # of), just surfacing it clearly instead of leaving it to a routine
    # WARNING line in the app's own log that nobody tails (2026-07-15 --
    # see cli_checks.check_directory_fsync_support's own docstring for
    # the full reasoning).
    print_fn("\n-- 11c. Directory fsync support --")
    for label, level, detail in cli_checks.check_directory_fsync_support(data_dir):
        print_fn(f"[{level}] {label}: {detail}")

    # 11d. Data path group/SELinux audit -- data_dir itself,
    # [logging].log_file, and [site].static_site_dir all go through the
    # SAME cli_checks.check_path_group_and_selinux() (see its own
    # docstring: this is deliberately the ONE place any FUTURE
    # settings.toml-configurable directory (e.g. an
    # email-templates dir) gets wired in too, one line here plus one
    # in build_report() above, instead of new bespoke chgrp/restorecon
    # code each time). Auto-fix gated on is_root(), same reasoning as the
    # data-dir-ownership chown step above -- chgrp/restorecon are both
    # genuinely privileged operations, no point prompting for something
    # that would just fail as a non-root my-booking-group member.
    print_fn("\n-- 11d. Data path group/SELinux audit --")
    audit_paths = [("data dir", Path(data_dir))]
    log_file_configured = config.log_file_from_raw(raw)
    if log_file_configured and Path(log_file_configured).exists():
        audit_paths.append(("log file", Path(log_file_configured)))
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if static_site_dir:
        audit_paths.append(("static_site_dir", Path(static_site_dir)))
    for path_label, path in audit_paths:
        for label, level, detail in cli_checks.check_path_group_and_selinux(path_label, path):
            if level == "ok":
                print_fn(f"[ok] {label}: {detail}")
                continue
            print_fn(f"{label}: {detail}")
            if label.endswith(" group"):
                if not is_root():
                    print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
                elif prompt(f"Run chgrp -R my-booking {path} now?"):
                    run(["chgrp", "-R", "my-booking", str(path)])
                    print_fn("[ok] group fixed")
            elif label.endswith(" SELinux context"):
                if not is_root():
                    print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
                elif prompt(f"Run restorecon -Rv {path} now?"):
                    run(["restorecon", "-Rv", str(path)])
                    print_fn("[ok] SELinux context restored")

    # 12. Maintenance mode -- informational only, same reasoning as CalDAV
    # above: there's no safe "fix" to offer here (it's a deliberate toggle,
    # not a misconfiguration), just surfacing whether it's currently ON so
    # it doesn't stay on by accident, unnoticed, after a real maintenance
    # window ends. Use `my-bt admin site-maintenance off` directly, not this
    # walkthrough, to turn it off.
    print_fn("\n-- 12. Maintenance mode --")
    for label, level, detail in cli_checks.check_maintenance_mode(data_dir):
        print_fn(f"[{level}] {label}: {detail}")

    # 13. Calendar invite format -- the "on install" half of the
    # standing requirement (2026-07-09) that any change to the CALENDAR
    # INVITE(s) also updates existing (future) calendar invites, either
    # on install or the next time this calendar invite is touched. Unlike
    # every other step above, this one never prompts -- it's idempotent
    # (a no-op once the marker matches CALENDAR_INVITE_FORMAT_VERSION, see
    # that constant's own docstring in app/calendar_sync.py) and touches
    # nothing outside the app's own normal job (re-describing the
    # operator's own live CalDAV events, exactly as sync_occurrence()
    # already does on every booking/cancellation) -- so there's nothing
    # here for a human to weigh in on, unlike e.g. rewriting nginx config.
    # Best-effort, same as the CalDAV calendar-name check above: a
    # transient network hiccup, or CalDAV not fully configured yet, must
    # not crash the rest of this walkthrough.
    print_fn("\n-- 13. Calendar invite format --")
    caldav_cal = raw.get("booking_calendar", {})
    if not caldav_cal.get("caldav_url") or not caldav_cal.get("username") or not caldav_cal.get("password_file"):
        print_fn("[skip] caldav_url/username/password not fully configured yet -- not checked")
    else:
        try:
            from . import calendar_sync as app_calendar_sync
            from . import cancel_flow as app_cancel_flow
            from .config import load_settings
            from .storage import Store

            settings = load_settings(settings_path)
            caldav = app_cancel_flow.build_caldav_client(settings)
            href = app_cancel_flow.calendar_href(caldav, settings)
            store = Store(data_dir)
            result = app_calendar_sync.resync_if_format_changed(caldav, href, store, settings, data_dir)
            if result is None:
                print_fn("[ok] calendar invite format unchanged since the last resync -- nothing to do")
            elif result.skipped:
                # 2026-07-15/16: a real run showed step 13 printing an
                # "[ok]"-looking result even though not everything had
                # actually succeeded --
                # `fixed` alone (the old message here) hid the fact that
                # some occurrences were SKIPPED after a persistent CalDAV
                # conflict -- this must never print [ok] when that
                # happened, even though the marker itself still gets
                # written (see resync_if_format_changed's own docstring
                # for why). record_resync_skips() (already called inside
                # resync_if_format_changed) is what makes this visible
                # again later too, in `admin health`/`admin setup` --
                # see cli_checks.check_calendar_invite_resync_skips.
                print_fn(
                    f"[warn] calendar invite format changed -- resynced {result.fixed} upcoming "
                    f"occurrence(s), but {len(result.skipped)} FAILED (persistent CalDAV conflict):"
                )
                for line in result.skipped:
                    print_fn(f"       - {line}")
                print_fn("       run `my-bt admin resync-calendar` to retry these")
            else:
                print_fn(f"[ok] calendar invite format changed -- resynced {result.fixed} upcoming occurrence(s)")
        except Exception as exc:  # noqa: BLE001 -- config/network/CalDAV failure, report don't crash
            print_fn(f"[warn] couldn't check/resync calendar invite format: {exc}")

    # 2026-07-08: "Done" now reflects whether there were any problems --
    # previously this was a flat "Done." no matter
    # how many [warn]/[fail] lines had just scrolled by above, so a real
    # outstanding issue (e.g. step 10's stale nginx_access_log path) was
    # exactly as easy to miss as a totally clean run. Re-running
    # build_report() here (the same check set `status`/plain `setup` use,
    # see its own docstring) picks up whatever the interactive prompts
    # above actually fixed -- a check that was warn/fail at the top of this
    # walkthrough may already be ok now, so this reflects the CURRENT state,
    # not just "were there problems originally."
    final_checks = [c for group in build_report(raw, settings_path, home, data_dir).values() for c in group]
    fails = sum(1 for _, level, _ in final_checks if level == "fail")
    warns = sum(1 for _, level, _ in final_checks if level == "warn")
    problems = cli_checks.summarize_problems(final_checks)
    if problems:
        print_fn("\nStill need attention, repeated from above:")
        for line in problems:
            print_fn(f"   {line}")
    if fails or warns:
        print_fn(f"\nDone -- {fails} problem(s), {warns} warning(s) still need attention (see above, "
                  "or re-run `my-bt status`).")
    else:
        print_fn("\nDone -- all checks pass now. Re-run `my-bt status` any time to re-check everything.")
    return fails, warns
