"""Health/setup checks shared by `my-bt status` and `my-bt setup`
(scripts/my-bt). Pure(-ish) data functions: each returns a list of
`(label, level, detail)` triples ("ok"/"warn"/"fail") and never prints or
prompts -- that's `app/cli_setup.py`'s job. Kept in this importable
package (not in scripts/my-bt, which has no .py extension and lives
outside `app/`, so unittest can't reach it directly) specifically so
these are unit-testable the same way everything else in this project is:
mock `subprocess`/`shutil` here, or point a check at a tmp directory,
rather than only ever exercising this via manual smoke testing.

`status` and `setup` both call the exact same functions here -- if you
add a new check, add it once, in one place. Separate copies of this kind
of thing have drifted and caused real bugs in this project before (see
the maintainer's local notes).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import site_render
from .caldav_client import CalDAVClient, HttpTransport

Check = tuple[str, str, str]  # (label, "ok"|"warn"|"fail", detail)


def secret_file_map(raw: dict) -> dict[str, str | None]:
    return {
        "caldav_password": raw.get("calendar", {}).get("caldav_password_file"),
        "smtp_password": raw.get("smtp", {}).get("password_file"),
        "admin_password_hash": raw.get("admin", {}).get("password_hash_file"),
        "erasure_pepper": raw.get("privacy", {}).get("erasure_pepper_file"),
    }


def check_secrets(raw: dict) -> list[Check]:
    checks: list[Check] = []
    for name, path_str in secret_file_map(raw).items():
        if not path_str:
            checks.append((f"secret: {name}", "warn", "not configured in settings.toml"))
            continue
        p = Path(path_str)
        if not p.exists():
            checks.append((f"secret: {name}", "fail", f"missing -- create {path_str}"))
            continue
        content = p.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            checks.append((f"secret: {name}", "fail", f"{path_str} exists but is empty"))
            continue
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode != 0o600:
            checks.append((f"secret: {name}", "warn", f"{path_str} is mode {oct(mode)}, expected 0600"))
        else:
            checks.append((f"secret: {name}", "ok", "present, mode 0600"))
        if name == "admin_password_hash" and "$" not in content:
            checks.append((f"secret: {name}", "fail",
                "doesn't look like a hash (no '$') -- did you paste the "
                "plain password instead of hash_admin_password()'s output? see README.md"))
        if name == "erasure_pepper":
            try:
                if len(bytes.fromhex(content)) != 32:
                    checks.append((f"secret: {name}", "warn",
                        "not 32 bytes of hex -- expected `openssl rand -hex 32` output"))
            except ValueError:
                checks.append((f"secret: {name}", "fail", "not valid hex -- expected `openssl rand -hex 32` output"))
    return checks


# Timeout kept short (unlike CalDAVClient's own 15s production default) so
# `my-bt status`/`setup` stay responsive if the CalDAV server is slow or
# unreachable -- this is a health check, not a real booking request.
_CALDAV_CHECK_TIMEOUT = 5.0


def check_data_dir_git(data_dir: str | Path) -> list[Check]:
    """Whether `data_dir` (the CSV data directory, users.csv/
    registrations.csv/data/archived/*) is already protected by its own,
    separate git repository -- see app/git_snapshot.py, which the hourly
    systemd timer uses to auto-commit any change. `warn` (not `fail`):
    this is a defense-in-depth safety net, not a hard requirement for the
    app to function. A no-op-looking `warn` rather than silence, since
    the whole point is to catch a fresh/pre-existing install that hasn't
    opted in yet -- `my-bt setup -i` offers to initialize it right there
    (git init, a `.gitignore` excluding `*.tmp`, local `user.email`/
    `user.name`, and an initial commit via app.git_snapshot.snapshot())."""
    data_dir = Path(data_dir)
    if not (data_dir / ".git").exists():
        return [(f"data dir git snapshot ({data_dir})", "warn",
                  "not yet a git-protected repo -- run `my-bt setup -i` to initialize it")]
    return [(f"data dir git snapshot ({data_dir})", "ok", "git repo present -- hourly snapshot timer keeps it committed")]


def check_caldav_calendars(raw: dict) -> list[Check]:
    """Live PROPFIND against the configured CalDAV server, verifying
    `[calendar].booking_calendar` and every `[calendar].conflict_calendars`
    name actually exists there right now. Catches the exact failure mode
    hit in practice 2026-07-05: a calendar got renamed/reset on the
    provider's side (mailbox.org), so settings.toml pointed at names that
    no longer existed -- every single `/book/<shortname>` page 500'd with
    a CalDAVError, and nothing caught it ahead of time (`status` only
    checked local files/services, never actually asked the CalDAV server
    what it has). A no-op if caldav_url/username/the password secret
    aren't all configured yet -- check_secrets() already covers that.
    Best-effort: any connection/auth/parsing failure is reported as one
    warn rather than raised, since a transient network hiccup shouldn't
    make `status` itself fail."""
    cal = raw.get("calendar", {})
    caldav_url = cal.get("caldav_url")
    username = cal.get("caldav_username")
    password_file = cal.get("caldav_password_file")
    if not caldav_url or not username or not password_file:
        return []
    password_path = Path(password_file)
    if not password_path.exists():
        return []  # check_secrets() already reports this
    try:
        password = password_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    transport = HttpTransport(username, password, timeout=_CALDAV_CHECK_TIMEOUT)
    try:
        calendars = CalDAVClient(caldav_url, username, password, transport=transport).list_calendars()
    except Exception as exc:  # noqa: BLE001 -- network/auth/XML-parse failure, report don't crash
        return [("CalDAV calendars", "warn", f"couldn't reach/list calendars at {caldav_url}: {exc}")]
    wanted = {cal.get("booking_calendar")} | set(cal.get("conflict_calendars") or ())
    wanted.discard(None)
    checks: list[Check] = []
    for name in sorted(wanted):
        if name in calendars:
            checks.append((f"CalDAV calendar '{name}'", "ok", "found"))
        else:
            checks.append((f"CalDAV calendar '{name}'", "fail",
                            f"not found among {sorted(calendars)} -- update settings.toml "
                            "[calendar].booking_calendar/conflict_calendars, or recreate/rename "
                            "it with your CalDAV provider (every booking page 500s until this "
                            "is fixed)"))
    return checks


def check_systemd() -> list[Check]:
    if not shutil.which("systemctl"):
        return [("systemd", "warn", "systemctl not found -- skipping (not on the target server?)")]
    checks: list[Check] = []
    for unit in (
        "my-booking.service", "my-booking-retention.timer",
        "my-booking-watchdog.timer", "my-booking-git-snapshot.timer",
    ):
        enabled = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True
        ).stdout.strip()
        active = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True
        ).stdout.strip()
        if enabled == "enabled" and active in ("active", "waiting"):
            checks.append((unit, "ok", f"enabled, {active}"))
        elif enabled != "enabled":
            checks.append((unit, "warn", f"not enabled -- sudo systemctl enable --now {unit}"))
        else:
            checks.append((unit, "warn", f"enabled but not active ({active or 'unknown'}) -- systemctl status {unit}"))
    return checks


def _service_active_since(unit: str) -> float | None:
    """Epoch seconds `unit` last transitioned to active, or None if that
    can't be determined (not on a systemd host, no GNU `date`, unit never
    started, etc.) -- best-effort, same as every other live-system check
    here. `systemctl show --value` gives a human timestamp string (e.g.
    "Sat 2026-07-05 10:00:00 CEST"), not an epoch, and there's no portable
    systemctl flag for epoch directly -- shelling out to `date -d` to parse
    it is simpler and more correct than reimplementing systemd's timestamp
    format ourselves (same "ask the system" reasoning as _my_booking_can_read)."""
    if not shutil.which("systemctl") or not shutil.which("date"):
        return None
    try:
        raw_ts = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not raw_ts or raw_ts in ("n/a", "0"):
        return None  # never (yet) entered the active state
    try:
        epoch_str = subprocess.run(
            ["date", "-d", raw_ts, "+%s"], capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        return float(epoch_str) if epoch_str else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def check_settings_fresh(settings_path: str, unit: str = "my-booking.service") -> list[Check]:
    """settings.toml is read exactly once, at app/serve.py's startup -- see
    that module's main(). There's no file-watching or SIGHUP-triggered
    reload, so an edit made *after* `unit` last (re)started is correct on
    disk but not live yet. Hit in practice 2026-07-05: the operator edited a
    course description on the server, the file was correct, but the
    booking page kept showing the old text because my-booking.service
    hadn't been restarted -- nothing flagged this, so it looked like a
    bug rather than a pending restart. Silent (returns []) if `unit`
    isn't currently active: check_systemd() already reports that, and
    "stale relative to a service that isn't even running" isn't a
    meaningful thing to also say here."""
    if not shutil.which("systemctl"):
        return []
    p = Path(settings_path)
    if not p.exists():
        return []  # a missing settings.toml is already reported elsewhere
    is_active = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5, check=False,
    ).stdout.strip()
    if is_active != "active":
        return []
    since = _service_active_since(unit)
    if since is None:
        return [(f"{unit} freshness", "warn", "can't determine when it last (re)started -- skipping")]
    if p.stat().st_mtime > since:
        return [(f"{unit} freshness", "warn",
                  f"settings.toml was edited after {unit} last (re)started -- those edits aren't "
                  f"live yet: sudo systemctl restart {unit}")]
    return [(f"{unit} freshness", "ok", "settings.toml unchanged since last (re)start")]


def check_selinux() -> list[Check]:
    if not shutil.which("getenforce"):
        return [("SELinux", "ok", "not present on this system")]
    mode = subprocess.run(["getenforce"], capture_output=True, text=True).stdout.strip()
    if mode != "Enforcing":
        return [("SELinux", "ok", f"{mode or 'not present'} (nginx proxy restriction doesn't apply)")]
    if not shutil.which("getsebool"):
        return [("SELinux", "warn", "enforcing, but getsebool isn't available to check the boolean")]
    out = subprocess.run(
        ["getsebool", "httpd_can_network_connect"], capture_output=True, text=True
    ).stdout.strip()
    if out.endswith("on"):
        return [("SELinux httpd_can_network_connect", "ok", "on")]
    return [("SELinux httpd_can_network_connect", "fail",
             f"{out or 'unknown'} -- sudo setsebool -P httpd_can_network_connect on")]


# The location paths this app needs -- see nginx/my-booking.conf(.example).
# Kept as a plain tuple, not derived from the .example file itself, so this
# check has no dependency on that file's exact on-disk location/format at
# runtime (it only needs to know what to look FOR, not read the example).
_REQUIRED_NGINX_LOCATIONS = (
    "/courses", "/book/", "/cancel/", "/reinstate/", "/host-cancel/", "/host-reinstate/", "/my", "/admin",
)


def check_nginx_locations() -> list[Check]:
    """Checks whether each `location` block this app needs is already
    present in the LIVE, fully-merged nginx config (`nginx -T`, which
    resolves every `include` -- nginx.conf, conf.d/*, sites-enabled/*,
    snippets, etc. -- not just one guessed vhost file, so this can't miss a
    location block just because it lives in a different file than expected).
    Read-only: this never edits nginx config itself -- guessing at and
    rewriting a stranger's hand-maintained vhost would be worse than asking
    (see setup -i's own comment on this)."""
    if not shutil.which("nginx"):
        return [("nginx", "warn", "nginx not found -- skipping (not on the target server?)")]
    result = subprocess.run(["nginx", "-T"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return [("nginx -T", "warn", f"failed to read the live config ({detail}) -- check manually with sudo nginx -T")]
    merged = result.stdout
    checks: list[Check] = []
    for path in _REQUIRED_NGINX_LOCATIONS:
        # Matches e.g. "location /book/ {" or "location = /my {" -- allows
        # an optional match modifier (=, ~, ~*, ^~) between "location" and
        # the path, same as nginx itself accepts.
        pattern = re.compile(rf"^\s*location\s+(?:[=~^]+\*?\s+)?{re.escape(path)}\s*\{{", re.MULTILINE)
        if pattern.search(merged):
            checks.append((f"nginx location {path}", "ok", "found in the live config"))
        else:
            checks.append((f"nginx location {path}", "warn",
                            "not found in the live config -- add it from "
                            "/usr/share/my-booking-tool/my-booking.conf.example"))
    return checks


def _repo_nginx_conf_files(home: str) -> list[Path]:
    """Real, personal nginx vhost conf file(s) placed directly in this
    checkout's site/ dir -- e.g. site/booking.example.org.conf, named after your own
    domain, gitignored the same way site/index.html and friends are (see
    .gitignore). Deliberately globs for "*.conf" rather than a single
    hardcoded filename: the whole point of this convention is that the
    filename is whatever domain *you* run this under, which my-bt can't
    know in advance. Excludes *.conf.example, the generic tracked template
    this ships (site/booking.example.org.conf.example is the operator's own real one,
    anonymized -- see README.md)."""
    site_dir = Path(home) / "site"
    if not site_dir.is_dir():
        return []
    return sorted(p for p in site_dir.glob("*.conf") if not p.name.endswith(".conf.example"))


def check_nginx_conf_repo_file(home: str) -> list[Check]:
    """Whether a real, filled-in nginx vhost conf file exists directly in
    this checkout's site/ dir (see _repo_nginx_conf_files), and if so,
    whether it still has every location block this app currently needs
    (_REQUIRED_NGINX_LOCATIONS) and no leftover REPLACE-ME placeholder.

    This exists to catch, at the SOURCE, the exact drift that actually
    happened 2026-07-10: a new route (/reinstate/, /host-reinstate/) was
    added to the app, and both nginx/my-booking.conf(.example) and
    check_nginx_locations()'s own required-list were updated for it -- but
    the separate, real, hand-hardened site/booking.example.org.conf sitting in this
    repo was NOT, and nothing caught that gap until the operator noticed it by
    inspection. check_nginx_locations() above only ever looks at the LIVE,
    already-deployed `nginx -T` output, so it can't help BEFORE a stale
    file like this is actually deployed; this check looks at the file
    still sitting in the checkout, so it can catch the gap even before
    `nginx -t && systemctl reload nginx` runs.

    Advisory only (warn, never fail) if no real conf file exists yet --
    a from-scratch install legitimately has none until you've hardened
    one, same as [site].static_site_dir being optional elsewhere in this
    module."""
    files = _repo_nginx_conf_files(home)
    if not files:
        return [("nginx vhost conf (site/*.conf)", "warn",
                  "no real, personal nginx vhost conf file found in site/ yet -- copy "
                  "site/booking.example.org.conf.example there (renamed for your own domain) as a "
                  "hardened starting point, or nginx/my-booking.conf.example for a bare-bones one")]
    checks: list[Check] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        problems = []
        if _PLACEHOLDER_MARKER in text:
            problems.append(f'still contains a "{_PLACEHOLDER_MARKER}" placeholder marker -- '
                              "copied from the .example without customizing it?")
        missing = [
            path for path in _REQUIRED_NGINX_LOCATIONS
            if not re.search(rf"^\s*location\s+(?:[=~^]+\*?\s+)?{re.escape(path)}\s*\{{", text, re.MULTILINE)
        ]
        if missing:
            problems.append(f"missing location block(s) for {', '.join(missing)} -- see "
                              "nginx/my-booking.conf.example for the bare version to adapt")
        if problems:
            checks.append((f"nginx vhost conf (site/{f.name})", "warn", "; ".join(problems)))
        else:
            checks.append((f"nginx vhost conf (site/{f.name})", "ok",
                            "has every required location block, no leftover placeholder marker"))
    return checks


def _iter_server_blocks(merged_config: str):
    """Yields the raw text of each top-level `server { ... }` block in a
    `nginx -T` dump. Depth-tracks braces rather than doing full parsing --
    `nginx -T` only ever prints an already-`nginx -t`-valid config, so
    braces are guaranteed balanced; this is just enough to isolate one
    vhost's own directives (e.g. `root`) from any `location` sub-blocks it
    contains, without needing a real nginx-config parser."""
    depth = 0
    current: list[str] | None = None
    start_depth = None
    for line in merged_config.splitlines():
        if current is None and re.match(r"^\s*server\s*\{", line):
            current = [line]
            start_depth = depth
            depth += line.count("{") - line.count("}")
            continue
        if current is not None:
            current.append(line)
            depth += line.count("{") - line.count("}")
            if depth == start_depth:
                yield "\n".join(current)
                current = None
                start_depth = None
            continue
        depth += line.count("{") - line.count("}")


def _live_nginx_config(raw: dict) -> str | None:
    """The full `nginx -T` dump (every `include` resolved), or None if
    nginx/the base_url hostname isn't available at all. Shared by every
    live-nginx-config check below so each one doesn't re-invoke `nginx -T`
    separately."""
    hostname = urlparse(raw.get("site", {}).get("base_url", "")).hostname
    if not hostname or not shutil.which("nginx"):
        return None
    result = subprocess.run(["nginx", "-T"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


def _matching_server_block(raw: dict, merged: str) -> str | None:
    """The one `server { ... }` block (if any) whose `server_name` list
    includes `[site].base_url`'s hostname."""
    hostname = urlparse(raw.get("site", {}).get("base_url", "")).hostname
    name_re = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)
    for block in _iter_server_blocks(merged):
        names = name_re.search(block)
        if names and hostname in names.group(1).split():
            return block
    return None


def _nginx_root_for_host(raw: dict) -> str | None:
    """Returns nginx's `root` for the server block matching
    `[site].base_url`'s hostname, or None if nginx/that block/its root
    can't be determined. Shared by check_static_pages_reachable()."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    block = _matching_server_block(raw, merged)
    if block is None:
        return None
    root_re = re.compile(r"^\s*root\s+([^;]+);", re.MULTILINE)
    root_match = root_re.search(block)
    return root_match.group(1).strip() if root_match else None


_ACCESS_LOG_RE = re.compile(r"^\s*access_log\s+(\S+)(?:\s+\S+)*\s*;", re.MULTILINE)


def _strip_server_blocks(merged: str) -> str:
    """`merged` with every `server { ... }` body removed -- what's left is
    the http-level (or otherwise outside-any-vhost) config, used as the
    fallback search space for a directive a specific vhost doesn't
    override (mirrors nginx's own inheritance: a server block that
    doesn't set access_log itself inherits the http block's)."""
    text = merged
    for block in _iter_server_blocks(merged):
        text = text.replace(block, "", 1)
    return text


def _nginx_access_log_for_host(raw: dict) -> str | None:
    """Live `access_log` path for the vhost matching `[site].base_url`'s
    hostname: checked in the matching server block first, falling back to
    an http-level directive outside any server block if that vhost
    doesn't override it. None if nginx/the vhost/a real file target can't
    be determined, or logging there is disabled (`access_log off;`) or
    sent to syslog rather than a file -- none of those are something
    [watchdog].nginx_access_log could point at anyway."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    block = _matching_server_block(raw, merged)
    for text in filter(None, (block, _strip_server_blocks(merged))):
        m = _ACCESS_LOG_RE.search(text)
        if m and m.group(1) not in ("off",) and not m.group(1).startswith("syslog:"):
            return m.group(1)
    return None


def _looks_like_combined_log_format(path_str: str) -> bool | None:
    """Spot-checks the first non-empty line of an access log against
    app.watchdog's own combined-format parser -- surfaced only as a soft
    hint. A custom nginx `log_format` wouldn't match it, which would make
    the nginx-burst check silently useless forever (0 lines ever parsed)
    if enabled without anyone noticing the mismatch. Returns None
    (couldn't tell -- unreadable or empty) rather than False, so callers
    don't turn an inconclusive result into a false warning."""
    from .watchdog import _NGINX_LINE_RE
    try:
        with open(path_str, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    return bool(_NGINX_LINE_RE.match(line))
    except OSError:
        return None
    return None


def check_watchdog_nginx_access_log_config(raw: dict) -> list[Check]:
    """Cross-checks [watchdog].nginx_access_log against nginx's OWN live
    config for the vhost, the same way check_static_pages_reachable()
    cross-checks static_site_dir against nginx's real root -- catches a
    stale/typo'd path, and (the more common case in practice) surfaces
    that the nginx-burst check isn't even turned on yet when it easily
    could be, without ever writing to settings.toml itself (that's
    cli_setup.py's job -- see its interactive offer to add this same
    setting for you). A no-op ([]) if nginx/the vhost/an access_log
    directive can't be determined at all -- nothing useful to say then."""
    detected = _nginx_access_log_for_host(raw)
    configured = raw.get("watchdog", {}).get("nginx_access_log")
    if configured and detected:
        if Path(configured).resolve() == Path(detected).resolve():
            return [("watchdog: nginx_access_log", "ok", "matches nginx's live config")]
        return [("watchdog: nginx_access_log", "warn",
                  f"settings.toml has {configured}, but nginx's live config for this vhost "
                  f"logs to {detected} -- update settings.toml if that's stale")]
    if not configured and detected:
        looks_ok = _looks_like_combined_log_format(detected)
        caveat = "" if looks_ok in (True, None) else (
            " (note: its format doesn't look like the default combined log format the "
            "burst-check parser expects -- enabling it might not actually detect anything)"
        )
        return [("watchdog: nginx-burst check", "warn",
                  f'not enabled yet -- nginx\'s live config logs to {detected}. Add '
                  f'nginx_access_log = "{detected}" under [watchdog] in settings.toml to '
                  f"turn it on{caveat}")]
    return []


# Pages this tool never templates/generates (unlike privacy.html -- see
# check_static_site_drift) but that still benefit from knowing whether the
# LIVE deployed copy matches what's in the checkout.
_STATIC_PAGES_TO_DEPLOY = ("index.html", "impressum.html", "terms.html")


def check_static_pages_reachable(raw: dict) -> list[Check]:
    """For each page my-bt has actually put in `[site].static_site_dir`,
    checks it's reachable from nginx's real `root` for that host -- either
    because static_site_dir IS that root, or because a symlink (or copy)
    for that specific file exists there, same as an already-working
    `index.html -> ../index.html` symlink would. Deliberately does NOT
    assume static_site_dir should just equal nginx's root: some setups
    keep a git-tracked staging directory (static_site_dir) separate from
    the public webroot on purpose (e.g. to avoid exposing a `.git` folder
    under the public root), symlinking in only the specific files meant to
    be public -- see the maintainer's local notes. So the suggested fix here is always
    a per-file symlink, never "change static_site_dir". Hit in practice
    2026-07-05: privacy.html/terms.html/impressum.html existed in
    static_site_dir but had no matching symlink in nginx's actual root, so
    they 404'd for every visitor despite `status` reporting them fine."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    nginx_root = _nginx_root_for_host(raw)
    if nginx_root is None:
        return []  # nginx missing/not configured for this host -- nothing to cross-check
    if Path(nginx_root).resolve() == Path(static_site_dir).resolve():
        return []  # same directory -- every file is trivially reachable, nothing to report
    checks: list[Check] = []
    for name in (site_render.OUTPUT_NAME,) + _STATIC_PAGES_TO_DEPLOY:
        managed = Path(static_site_dir) / name
        if not managed.exists():
            continue  # not deployed at all yet -- already covered by other checks
        served = Path(nginx_root) / name
        if served.exists() and served.resolve() == managed.resolve():
            checks.append((f"nginx-reachable: {name}", "ok", f"symlinked/present at {served}"))
        else:
            checks.append((f"nginx-reachable: {name}", "warn",
                            f"{managed} exists but nginx's root ({nginx_root}) has no matching "
                            f"file -- visitors get a 404. Fix: ln -s {managed} {served}"))
    return checks


# Fallback source location on an INSTALLED system. `home` (HOME,
# /opt/my-booking) deliberately does NOT carry index.html/impressum.html/
# terms.html at all -- only privacy.html.tmpl, the one thing my-bt reads/
# writes at runtime (see packaging/my-booking-tool.spec's %install: the
# other three are installed under %{_docdir}/%{name}/site/ instead, as a
# %doc reference copy, already resolved real-or-.example at build time by
# scripts/build-rpm.sh's materialization step). Hit in practice
# 2026-07-05: without this fallback, check_static_pages_deployed() found
# nothing to compare against on a real installed server and silently
# printed nothing at all for these three pages, even after a rebuild that
# genuinely had newer content.
_DOC_SITE_DIR = Path("/usr/share/doc/my-booking-tool/site")


def _resolve_static_source(home: str, name: str) -> Path | None:
    """The checkout's real site/<name>, falling back to site/<name>.example
    (both relative to `home` -- correct when running straight from a git
    checkout), then to the RPM's installed %doc reference copy at
    _DOC_SITE_DIR (correct on an actual installed system)."""
    real = Path(home) / "site" / name
    if real.exists():
        return real
    example = Path(home) / "site" / f"{name}.example"
    if example.exists():
        return example
    doc_copy = _DOC_SITE_DIR / name
    if doc_copy.exists():
        return doc_copy
    return None


def check_static_pages_deployed(raw: dict, home: str) -> list[Check]:
    """For each of index.html/impressum.html/terms.html: is it deployed to
    [site].static_site_dir at all, and if so, does it match what's in this
    checkout right now? Purely informational -- these pages are
    hand-authored and deliberately never auto-copied (README.md
    "Static-site pages") -- but staying silent about a missing or stale
    page was itself the bug: an index.html footer edit sat in the checkout
    for weeks without ever reaching the live site, and neither `status`
    nor `setup` ever mentioned it (hit in practice 2026-07-05)."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    checks: list[Check] = []
    for name in _STATIC_PAGES_TO_DEPLOY:
        deployed = Path(static_site_dir) / name
        source = _resolve_static_source(home, name)
        if source is None:
            continue  # nothing in the checkout to compare against either
        if not deployed.exists():
            checks.append((f"static site content ({deployed})", "warn",
                            f"not deployed yet -- copy {source} there"))
            continue
        same = deployed.read_text(encoding="utf-8", errors="replace") == \
            source.read_text(encoding="utf-8", errors="replace")
        if same:
            checks.append((f"static site content ({deployed})", "ok", "matches your checkout"))
        else:
            checks.append((f"static site content ({deployed})", "warn",
                            f"differs from your checkout's {source} -- vimdiff {deployed} {source} "
                            "to compare/merge (both sides may have real content, unlike privacy.html "
                            "-- see the maintainer's local notes)"))
    return checks


def check_rpmnew(paths: list[str]) -> list[Check]:
    """settings.toml and the installed site/privacy.html.tmpl are both
    %config(noreplace) in the RPM (see packaging/*.spec) -- rpm never
    overwrites a locally-modified copy of either on upgrade, but if the
    packaged version also changed, it drops the new one alongside yours as
    `<path>.rpmnew` instead of silently discarding either side. Surface
    that here so a pending manual merge doesn't go unnoticed."""
    checks: list[Check] = []
    for path_str in paths:
        rpmnew = Path(path_str + ".rpmnew")
        name = Path(path_str).name
        if not rpmnew.exists():
            checks.append((f"{name}.rpmnew", "ok", "none pending -- nothing to merge"))
        else:
            checks.append((
                f"{name}.rpmnew ({rpmnew})", "warn",
                "a newer packaged version is waiting to be merged in by hand: "
                f"vimdiff {path_str} {rpmnew}, then remove the .rpmnew"
            ))
    return checks


def check_group_membership() -> list[Check]:
    import getpass
    import grp

    target_user = os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        g = grp.getgrnam("my-booking")
    except KeyError:
        return [("my-booking group membership", "warn",
                  "group 'my-booking' doesn't exist yet -- install the package first")]
    if target_user == "root" or target_user in g.gr_mem:
        return [(f"my-booking group membership ({target_user})", "ok", "already a member")]
    return [(
        f"my-booking group membership ({target_user})", "warn",
        f"not in the my-booking group yet -- sudo usermod -aG my-booking {target_user} "
        "(log out/in, or start a new shell, for it to take effect)"
    )]


# %config(noreplace) paths whose own drift is already tracked via
# check_rpmnew() above -- check_rpm_verify() skips these so a locally
# customized settings.toml/privacy.html.tmpl (the expected, supported case)
# doesn't also show up as a scary "modified since install" package-wide
# warning; rpm -V still catches anything unexpected elsewhere.
_KNOWN_CONFIG_MARKERS = ("c",)

# rpm -V's 9-char flag string, position by position: S=size M=mode 5=MD5
# sum D=device L=readlink U=user G=group T=mtime P=capabilities. Only
# S/5/L/D indicate the file's actual DATA (or type) changed -- everything
# else is metadata. %post's `chown -R my-booking:my-booking /etc/my-booking`
# (see packaging/my-booking-tool.spec) deliberately changes ownership away
# from whatever the RPM recorded at build time, so a U/G(/M/T)-only diff on
# a file under /etc/my-booking is the package's OWN intended behavior, not
# tampering -- flagging it as "modified since install" was a false
# positive (hit in practice 2026-07-05 on settings.toml.example). "missing"
# (the file doesn't exist at all) is always worth flagging regardless.
_CONTENT_FLAGS = frozenset("S5LD")


def check_rpm_verify(package_name: str = "my-booking-tool") -> list[Check]:
    """`rpm -V <package>` compares every file the package owns (size, mode,
    checksum, owner, mtime, ...) against what was actually installed --
    catches a manual edit to *any* packaged file, not just the ones
    deliberately marked %config(noreplace) (systemd units, the nginx
    example, the app code itself). Report-only: unlike %config(noreplace),
    these files get silently overwritten back to the packaged version on
    the next upgrade regardless, so this is purely "something changed,
    here's what" -- not a merge workflow like check_rpmnew()."""
    if not shutil.which("rpm"):
        return [("package integrity (rpm -V)", "ok", "rpm not present -- not an rpm install, skipping")]
    q = subprocess.run(["rpm", "-q", package_name], capture_output=True, text=True)
    if q.returncode != 0:
        return [("package integrity (rpm -V)", "ok",
                  "not installed via rpm -- skipping (manual scripts/install.sh install?)")]
    v = subprocess.run(["rpm", "-V", package_name], capture_output=True, text=True)
    lines = [ln for ln in v.stdout.splitlines() if ln.strip()]
    if not lines:
        return [("package integrity (rpm -V)", "ok", "no unexpected changes to any packaged file")]

    checks: list[Check] = []
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        flags, path = parts[0], parts[-1]
        # rpm -V line shape: "<9-char flags>[ <type-marker>] <path>" -- the
        # type marker (one of c/d/g/l/r) only appears when that file is
        # %config/%doc/%ghost/%license/%readme *and* differs from the
        # package's record. A "missing" file reports the literal word
        # "missing" in place of the flag string.
        marker = parts[1] if len(parts) >= 3 and parts[1] in ("c", "d", "g", "l", "r") else ""
        if marker in _KNOWN_CONFIG_MARKERS:
            continue  # settings.toml / privacy.html.tmpl -- tracked via check_rpmnew() instead
        if flags != "missing" and not any(f in flags for f in _CONTENT_FLAGS):
            continue  # ownership/mode/mtime-only -- expected (see _CONTENT_FLAGS above), not tampering
        checks.append((f"modified since install: {path}", "warn", f"rpm -V flags: {flags}"))
    if not checks:
        checks.append(("package integrity (rpm -V)", "ok",
                        "no unexpected content changes (ownership/mode/mtime-only differences -- "
                        "e.g. from the package's own postinstall chown -- and %config files, "
                        "tracked via .rpmnew checks above, are expected and excluded)"))
    return checks


def check_static_site_drift(raw: dict, template_path: str | Path) -> list[Check]:
    """If `[site].static_site_dir` is configured, compares the LIVE
    deployed privacy.html there against what the current settings.toml
    would actually render -- catches the case this whole mechanism exists
    for: someone changes retention_months in settings.toml but never
    re-runs the render step, so the public page silently keeps quoting the
    old number. A no-op (empty list) if static_site_dir isn't configured,
    same as the optional log_file check."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    template_path = Path(template_path)
    if not template_path.exists():
        return [("static site (privacy.html)", "warn",
                  f"can't check -- template not found at {template_path}")]
    privacy = raw.get("privacy", {})
    retention_months = privacy.get("retention_months", 24)
    canceled_retention_months = privacy.get("canceled_retention_months", 6)
    expected = site_render.render_privacy_html(template_path, retention_months, canceled_retention_months)

    deployed_path = Path(static_site_dir) / site_render.OUTPUT_NAME
    if not deployed_path.exists():
        # State only -- no "run `my-bt setup -i`" instruction here: this
        # same message is shown both by `my-bt status` (which already ends
        # its report with that instruction once, generically) and inside
        # `my-bt setup -i` itself, where telling the user to run the very
        # command they're already running is just confusing (see
        # the maintainer's local notes).
        return [(f"static site ({deployed_path})", "warn",
                  "not deployed yet -- once generated, copy site/*.html to "
                  "your live host as usual (see README.md)")]
    actual = deployed_path.read_text(encoding="utf-8", errors="replace")
    if actual == expected:
        return [(f"static site ({deployed_path})", "ok", "matches current settings.toml")]
    return [(
        f"static site ({deployed_path})", "warn",
        "doesn't match current settings.toml (retention numbers or wording "
        "changed since it was last generated)"
    )]


# Sentinel left in every tracked site/*.html.example placeholder (see
# site/index.html.example etc.) specifically so this check has something
# reliable to grep for -- if you see this in a LIVE page, the generic
# template was published without being customized first.
_PLACEHOLDER_MARKER = "REPLACE-ME"
_UNSUBSTITUTED_PLACEHOLDER = re.compile(r"\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

# Just the pages this project ships a generic .example starting point
# for (see "site/*.example" in the repo) -- privacy.html's own drift
# (stale retention numbers) is already covered by check_static_site_drift
# above, so this only adds the placeholder-marker check for it, same as
# the other three.
_SITE_PAGES = ("index.html", "impressum.html", "terms.html", "privacy.html")


def _my_booking_can_read(path: Path) -> bool | None:
    """Actually ASKS the OS whether the my-booking user can read `path`
    (via `runuser`), rather than reimplementing POSIX permission logic
    ourselves -- hit in practice 2026-07-05: an earlier version of this
    check inspected `st_mode` bits directly (owner/group/other), which is
    blind to POSIX ACLs. `setfacl` (the fix this check itself recommends!)
    grants access via an ACL entry, not a mode-bit change -- so the old
    check kept reporting "can't read" even immediately after the operator ran
    the exact setfacl command it printed, nagging him to redo a fix that
    had already worked. `runuser -u my-booking -- test -r <path>` asks
    the kernel directly, so it's correct regardless of ACLs, SELinux, or
    anything else that could affect real access -- the same "ask the
    system, don't model it" approach check_rpm_verify/check_selinux/
    check_nginx_locations already use via their own subprocess calls.
    Returns None (couldn't determine, not "can't read") if this process
    isn't root or `runuser` isn't installed -- switching to another user
    needs root, and a wrong guess here is worse than admitting we don't
    know (see the incident above)."""
    if os.geteuid() != 0 or not shutil.which("runuser"):
        return None
    result = subprocess.run(
        ["runuser", "-u", "my-booking", "--", "test", "-r", str(path)],
        capture_output=True,
    )
    return result.returncode == 0


def check_watchdog_nginx_access(raw: dict) -> list[Check]:
    """If [watchdog].nginx_access_log is set, checks whether the my-booking
    user can actually read it -- the watchdog service's own
    ReadOnlyPaths=-/var/log/nginx (see systemd/my-booking-watchdog.service)
    only grants a systemd sandboxing EXCEPTION for that path; it does
    nothing about the underlying file's owner/group/mode/ACLs, which is
    nginx's/the distro's call, not this app's. Fedora's nginx package
    typically leaves /var/log/nginx root:root, not readable by another
    unprivileged user by default -- this is the concrete gap the operator hit in
    practice 2026-07-05 setting the watchdog up for real. A no-op ([]) if
    nginx_access_log isn't configured at all -- see check_secrets() for
    the same "not configured yet" convention."""
    log_path_str = raw.get("watchdog", {}).get("nginx_access_log")
    if not log_path_str:
        return []
    log_path = Path(log_path_str)
    import pwd
    try:
        pwd.getpwnam("my-booking")
    except KeyError:
        return [("watchdog: nginx_access_log access", "warn",
                  "user 'my-booking' doesn't exist yet -- install the package first")]
    if not log_path.exists():
        return [(f"watchdog: nginx_access_log ({log_path})", "warn",
                  "configured but doesn't exist -- check [watchdog].nginx_access_log")]
    can_read = _my_booking_can_read(log_path)
    if can_read is None:
        return [(f"watchdog: nginx_access_log ({log_path})", "warn",
                  "can't verify read access without root -- re-run `sudo my-bt status`/"
                  "`setup` for an authoritative check")]
    if can_read:
        return [(f"watchdog: nginx_access_log ({log_path})", "ok", "my-booking can read it")]
    return [(
        f"watchdog: nginx_access_log ({log_path})", "warn",
        f"my-booking can't read this yet (nginx's own file permissions, not this app's) -- "
        f"sudo setfacl -R -m u:my-booking:rX {log_path.parent} && "
        f"sudo setfacl -d -m u:my-booking:rX {log_path.parent} "
        "(the -d default ACL keeps new files readable after nginx's own log rotation; "
        "needs the 'acl' package for setfacl)"
    )]


def check_static_site_compliance(raw: dict) -> list[Check]:
    """If `[site].static_site_dir` is configured, checks the LIVE deployed
    site/*.html pages for signs the generic `.example` placeholder (see
    site/index.html.example et al) was published without being customized
    first: a leftover "REPLACE-ME" marker, or literal unsubstituted
    `${...}` template syntax that should have been filled in by
    app/site_render.py. Deliberately does NOT inspect or judge the actual
    legal wording you *did* write -- see README.md's disclaimer, that part
    is entirely your call/responsibility. This only catches the cheap,
    unambiguous mistake of "forgot to replace the placeholder at all"."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    checks: list[Check] = []
    for name in _SITE_PAGES:
        p = Path(static_site_dir) / name
        if not p.exists():
            continue  # optional/manual per-page copy -- nothing to flag yet
        text = p.read_text(encoding="utf-8", errors="replace")
        problems = []
        if _PLACEHOLDER_MARKER in text:
            problems.append(f'still contains a "{_PLACEHOLDER_MARKER}" placeholder marker')
        if _UNSUBSTITUTED_PLACEHOLDER.search(text):
            problems.append("still contains an unsubstituted ${...} template placeholder")
        if problems:
            checks.append((f"static site content ({p})", "warn", "; ".join(problems)))
        else:
            checks.append((f"static site content ({p})", "ok", "no leftover placeholder markers"))
    return checks
