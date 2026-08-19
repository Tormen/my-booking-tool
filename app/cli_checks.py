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

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from . import config, maintenance, site_render
from .caldav_client import CalDAVClient, HttpTransport

Check = tuple[str, str, str]  # (label, "ok"|"warn"|"fail", detail)

# Same env-var-with-localhost-default convention as scripts/my-bt's own
# DEFAULT_INTERNAL_URL and app.cli_setup's own _DEFAULT_INTERNAL_URL --
# this is an internal, always-loopback listener, not something
# deployments realistically reconfigure per-command.
_DEFAULT_INTERNAL_URL = os.environ.get("MY_BOOKING_INTERNAL_URL", "http://127.0.0.1:8811")


def summarize_problems(checks: list[Check]) -> list[str]:
    """Formats every non-"ok" check as a printable "[LEVEL] label -- detail"
    line, in original order -- everything else is dropped.

    2026-07-08: all warnings are now repeated at the end of setup and
    status too. `my-bt admin health`/plain `my-bt admin setup`/
    `my-bt admin setup -i` each print a dozen-plus numbered sections of
    checks, then only a bare count ("2 warning(s), no hard failures") at
    the very end -- by the time you've scrolled to the bottom, the actual
    WARN/FAIL lines from section 2 are long gone above the fold, so you
    have to scroll all the way back up to find them again. Shared here
    (not duplicated three times) so all three end with the same repeated
    list, right before their own final pass/fail summary line."""
    return [
        f"[{level.upper()}] {label}" + (f" -- {detail}" if detail else "")
        for label, level, detail in checks
        if level != "ok"
    ]


def secret_file_map(raw: dict) -> dict[str, str | None]:
    out = {
        "caldav_password": raw.get("booking_calendar", {}).get("password_file"),
        "smtp_password": raw.get("smtp", {}).get("password_file"),
        "admin_password_hash": raw.get("admin", {}).get("password_hash_file"),
        "erasure_pepper": raw.get("privacy", {}).get("erasure_pepper_file"),
    }
    # CalDAV [[conflict_calendar]] entries carry their own password files
    # (2026-07-18 redesign) -- same permission/existence checks apply.
    for i, entry in enumerate(raw.get("conflict_calendar", []) or []):
        if entry.get("caldav_url"):
            name = entry.get("name") or f"conflict-{i + 1}"
            out[f"conflict_calendar '{name}' password"] = entry.get("password_file")
    return out


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


def check_maintenance_mode(data_dir: str | Path) -> list[Check]:
    """Whether sitewide maintenance mode (`my-bt admin site-maintenance on/off`, see
    app/maintenance.py) is currently ON -- reported as "warn", not silence,
    even though it's a perfectly normal, deliberate state to be in: the
    whole point is that leaving it on by accident shouldn't go unnoticed
    for days the way a genuinely forgotten warning could (same reasoning
    as every WARN in this module counting toward `status`/`setup`'s own
    exit code -- see check_nginx_locations()'s neighboring history)."""
    state = maintenance.read_state(data_dir)
    if not state.enabled:
        return [("maintenance mode", "ok", "off")]
    detail = f"ON since {state.set_at}" if state.set_at else "ON"
    if state.message:
        detail += f' -- message: "{state.message}"'
    detail += " -- `my-bt admin site-maintenance off` to reopen bookings"
    return [("maintenance mode", "warn", detail)]


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


def check_directory_fsync_support(data_dir: str | Path) -> list[Check]:
    """Whether `data_dir` actually supports directory fsync -- the thing
    `app.atomic_io.fsync_dir()` relies on, after every write's temp-file
    rename, to make the rename itself durable across a hard power loss
    (see that module's own docstring). fsync_dir() is deliberately
    best-effort on every individual write (an unsupported mount must
    never turn a successful write into a crash), which means a silently
    unsupported mount would otherwise never surface anywhere except a
    routine WARNING line in the app's own log.

    2026-07-15: that's the correct call for
    availability, but it's also the kind of failure that's invisible
    until the one time it matters -- if the actual production mount
    silently doesn't support directory fsync, every write since deploy
    has been getting the weaker guarantee with nobody the wiser. Worth a
    one-time capability probe, rather than relying on someone
    noticing a warning line in a log nobody tails. This is that probe,
    wired into `my-bt admin setup`/`admin health` so it's re-checked on
    every real run of either -- not just once at process startup (see
    app/serve.py's own startup check for that half)."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return [(f"directory fsync support ({data_dir})", "warn",
                  "data dir doesn't exist yet -- created on first booking/run, not checked")]
    from .atomic_io import probe_dir_fsync_support

    if probe_dir_fsync_support(data_dir):
        return [(f"directory fsync support ({data_dir})", "ok",
                  "directory fsync works -- atomic writes get their full crash-safety guarantee")]
    return [(
        f"directory fsync support ({data_dir})", "warn",
        "directory fsync is NOT supported on this mount -- every atomic write here still can't be "
        "torn/partial, but a rename can silently roll back on a hard power loss (see app/atomic_io.py)",
    )]


def check_data_dir_ownership(data_dir: str | Path) -> list[Check]:
    """Whether every `*.csv` directly in `data_dir` is actually owned by
    the `my-booking` system user -- the one every systemd unit
    (my-booking.service, my-booking-git-snapshot.service,
    my-booking-retention.service, my-booking-watchdog.service) runs as.

    2026-07-08, real production incident: `scripts/migrate-simplymeet-
    history.py --commit` was run from a root shell against the real data
    dir. Every CSV it wrote went through Store._atomic_write(), which
    always `os.chmod(tmp_path, 0o600)` before the atomic rename -- fine
    normally, since the file is always OWNED by whoever wrote it, and
    that's always been the my-booking service itself. Run as root
    instead, the freshly-written users.csv/registrations.csv ended up
    root-owned + mode 0600, i.e. readable by literally nobody except
    root -- the my-booking service itself got PermissionError on its very
    next read, a real GET /admin 500 (see the traceback: storage.py's
    _read_csv_plain -> PermissionError: [Errno 13] Permission denied).
    Fixed live with `chown my-booking: *.csv`; this check exists so the
    NEXT time someone (a script, a `sudo -u` slip, anyone) writes to this
    directory as the wrong user, `my-bt status` reports it as a `fail`
    BEFORE it turns into a live 500 someone has to journalctl their way
    back to.

    Deliberately checks real file ownership via os.stat() rather than
    os.access() (what the plain "data dir" check in `my-bt status` uses)
    -- os.access() answers "can the CURRENT process touch this," which is
    always yes for root regardless of who actually owns the file, so
    running `my-bt status` itself as root (a completely normal thing to
    do on this project, see README.md's "Installing" steps) would have
    silently reported "ok" the whole time this exact incident was live.
    An empty data dir (nothing written yet) or a dev checkout with no
    'my-booking' system user at all are not failures -- see below."""
    import pwd

    data_dir = Path(data_dir)
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        return []  # nothing written yet -- nothing to own
    try:
        expected_uid = pwd.getpwnam("my-booking").pw_uid
    except KeyError:
        return [("data dir file ownership", "warn",
                  "'my-booking' system user doesn't exist yet -- install the package first")]
    mismatched = []
    for f in csv_files:
        try:
            owner_uid = f.stat().st_uid
        except OSError:
            continue
        if owner_uid != expected_uid:
            try:
                owner_name = pwd.getpwuid(owner_uid).pw_name
            except KeyError:
                owner_name = f"uid {owner_uid}"
            mismatched.append((f, owner_name))
    if mismatched:
        fix = " ".join(str(f) for f, _ in mismatched)
        detail = ", ".join(f"{f.name} is owned by {owner}" for f, owner in mismatched)
        return [(
            "data dir file ownership", "fail",
            f"{detail} -- the my-booking service (runs as user 'my-booking') can't read/write "
            f"{'this file' if len(mismatched) == 1 else 'these files'}. Usually caused by something "
            "writing to the data dir as root or another user (e.g. scripts/migrate-simplymeet-"
            f"history.py run from a root shell). Fix: sudo chown my-booking:my-booking {fix}"
        )]
    return [("data dir file ownership", "ok", f"all {len(csv_files)} CSV file(s) owned by my-booking")]


def check_path_group_and_selinux(label: str, path: str | Path, expected_group: str = "my-booking") -> list[Check]:
    """Generic, reusable group-ownership + SELinux-file-context audit for
    ANY single data path (file or directory) my-booking reads or writes.

    2026-07-16: extended the group+permissions+SELinux audit to ALL data
    paths, including user-configurable ones in settings.toml (e.g. an
    email-templates directory). Before this, every path grew its
    own bespoke, slightly different check: check_data_dir_ownership
    above only ever checks `*.csv` OWNER uid (never group, never
    SELinux), and (until this change) `data_dir`/`log_file`/
    `static_site_dir` in scripts/my-bt's cmd_admin_health only ever used
    os.access() -- which silently reports "ok" when run as root
    regardless of the real ownership/permissions underneath (same root-
    masking failure mode check_data_dir_ownership's own docstring
    already explains for the 2026-07-08 incident). This is now the ONE
    function every data path -- `data_dir` itself, `[logging].log_file`,
    `[site].static_site_dir`, and any future settings.toml-configurable
    directory -- goes through, so adding one later is a single extra
    call here instead of new bespoke chgrp/restorecon code.

    Real os.stat()-based checks throughout, never os.access(), for the
    same reason.

    Checks, in order:
    - existence (warn, not fail -- several of these paths are created
      lazily on first real use, e.g. data_dir/log_file before the
      service has ever started)
    - GROUP ownership (warn if not `expected_group`'s own gid -- this is
      how a DIFFERENT process, e.g. nginx reading static_site_dir, is
      meant to reach a my-booking-owned path via group-read permission
      without needing to run as the my-booking user itself; "warn", not
      "fail", since a path with a different group can still work fine if
      it's otherwise world-readable -- this flags the common case, not
      every theoretically-working permission combination)
    - SELinux file context, ONLY when `getenforce` reports Enforcing
      (same "not present/not enforcing -> nothing to check" fallback as
      check_selinux() above) -- `matchpathcon` (policycoreutils, same
      package `getsebool`/`setsebool` already come from) says what
      SELinux's OWN policy expects for this exact path; `stat -c %C`
      says what it actually IS right now. A mismatch here is exactly the
      kind of thing that silently 403s/500s the very first time a
      service touches a path that was created or moved outside the
      normal package-install flow (rsync'd in, created by a script run
      from an unexpected working directory, ...) -- `restorecon -Rv
      <path>` is the fix, offered by `setup -i`."""
    import grp

    path = Path(path)
    if not path.exists():
        return [(f"{label} group/SELinux", "warn", f"{path} does not exist yet -- not checked")]

    checks: list[Check] = []
    try:
        expected_gid = grp.getgrnam(expected_group).gr_gid
    except KeyError:
        return [(f"{label} group", "warn",
                  f"'{expected_group}' system group doesn't exist yet -- install the package first")]
    actual_gid = path.stat().st_gid
    if actual_gid == expected_gid:
        checks.append((f"{label} group", "ok", f"group is '{expected_group}'"))
    else:
        try:
            actual_group = grp.getgrgid(actual_gid).gr_name
        except KeyError:
            actual_group = f"gid {actual_gid}"
        checks.append((f"{label} group", "warn",
            f"{path} is group '{actual_group}', expected '{expected_group}' -- "
            f"sudo chgrp -R {expected_group} {path}"))

    checks.extend(_check_selinux_context(label, path))
    return checks


def _check_selinux_context(label: str, path: Path) -> list[Check]:
    """The SELinux half of check_path_group_and_selinux, split out so it
    can be unit-tested (mocked subprocess calls) independently of the
    group-ownership half above. Same "not present/not enforcing -> ok,
    nothing to check" fallback as check_selinux()'s own httpd boolean
    check -- SELinux simply isn't relevant on a non-enforcing box."""
    if not shutil.which("getenforce"):
        return []
    mode = subprocess.run(["getenforce"], capture_output=True, text=True).stdout.strip()
    if mode != "Enforcing":
        return []
    if not shutil.which("matchpathcon"):
        return [(f"{label} SELinux context", "warn",
                  "enforcing, but matchpathcon isn't available to check "
                  "(install policycoreutils-python-utils) -- skipping")]
    expected = subprocess.run(
        ["matchpathcon", "-n", str(path)], capture_output=True, text=True,
    ).stdout.strip()
    actual = subprocess.run(
        ["stat", "-c", "%C", str(path)], capture_output=True, text=True,
    ).stdout.strip()
    if not expected or not actual:
        return [(f"{label} SELinux context", "warn",
                  "couldn't determine the expected/actual context -- skipping")]
    if expected == actual:
        return [(f"{label} SELinux context", "ok", f"matches policy ({actual})")]
    return [(f"{label} SELinux context", "warn",
              f"is '{actual}', policy expects '{expected}' -- sudo restorecon -Rv {path}")]


def _list_calendars_for(caldav_url: str, username: str, password_file: str) -> tuple[dict | None, str]:
    """(calendars, "") on success; (None, "") when not configured enough
    to try (check_secrets covers missing secrets); (None, problem) on a
    live failure worth reporting."""
    if not caldav_url or not username or not password_file:
        return None, ""
    password_path = Path(password_file)
    if not password_path.exists():
        return None, ""
    try:
        password = password_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, ""
    transport = HttpTransport(username, password, timeout=_CALDAV_CHECK_TIMEOUT)
    try:
        return CalDAVClient(caldav_url, username, password, transport=transport).list_calendars(), ""
    except Exception as exc:  # noqa: BLE001 -- network/auth/XML-parse failure, report don't crash
        return None, f"couldn't reach/list calendars at {caldav_url}: {exc}"


def check_caldav_calendars(raw: dict) -> list[Check]:
    """Live check of every configured calendar source (2026-07-18
    redesign: [booking_calendar] + [[conflict_calendar]]):

    - [booking_calendar]: PROPFIND, verify the named calendar exists.
      Catches the exact failure mode hit in practice 2026-07-05: a
      calendar renamed/reset provider-side while settings.toml pointed at
      the stale name -- every /book/<shortname> page 500'd and nothing
      caught it ahead of time.
    - each CalDAV [[conflict_calendar]]: same, with its own credentials.
    - each ICS [[conflict_calendar]]: live GET, verify it parses as a
      calendar (event count reported).
    - structural warn from the 2026-07-14 blocker-event work: without a
      blocks-mode entry covering the booking calendar, the operator's own
      personal events on it won't block any course. (Cancel-entire-session
      itself is NOT at risk -- its CANCELED blocker is caught regardless by
      the always-on, UID-keyed check in conflict.occurrence_is_hidden,
      2026-07-24 -- so this stays a warn about personal-event conflicts,
      not the hard dependency it used to be.)

    Best-effort throughout: failures are warns, never raises -- a
    transient network hiccup shouldn't fail `status`/`setup` outright."""
    booking = raw.get("booking_calendar", {})
    entries = raw.get("conflict_calendar", []) or []
    checks: list[Check] = []

    calendars, problem = _list_calendars_for(
        booking.get("caldav_url", ""), booking.get("username", ""), booking.get("password_file", ""),
    )
    if problem:
        checks.append(("CalDAV booking calendar", "warn", problem))
    elif calendars is not None:
        name = booking.get("calendar")
        if name in calendars:
            checks.append((f"booking calendar '{name}'", "ok", "found"))
        elif name:
            checks.append((f"booking calendar '{name}'", "fail",
                            f"not found among {sorted(calendars)} -- update settings.toml "
                            "[booking_calendar].calendar, or recreate/rename it with your "
                            "CalDAV provider (every booking page 500s until this is fixed)"))

    if not any(
        e.get("mode", "requires") == "blocks"
        and (str(e.get("source", "")) == "booking_calendar"
             or (e.get("caldav_url") == booking.get("caldav_url")
                 and e.get("calendar") == booking.get("calendar")))
        for e in entries
    ):
        checks.append((
            "conflict_calendar coverage", "warn",
            "no blocks-mode [[conflict_calendar]] entry covers the booking calendar "
            "(e.g. source = \"booking_calendar\", mode = \"blocks\") -- your own "
            "personal events on it won't block any course (cancel-entire-session "
            "still works: its blocker is caught by the always-on check)",
        ))

    for i, entry in enumerate(entries):
        name = entry.get("name") or f"conflict-{i + 1}"
        if entry.get("ics_url"):
            checks.append(_check_ics_conflict_source(name, entry["ics_url"]))
        elif entry.get("caldav_url"):
            cals, problem = _list_calendars_for(
                entry.get("caldav_url", ""), entry.get("username", ""), entry.get("password_file", ""),
            )
            if problem:
                checks.append((f"conflict calendar '{name}'", "warn", problem))
            elif cals is not None:
                if entry.get("calendar") in cals:
                    checks.append((f"conflict calendar '{name}'", "ok",
                                    f"calendar '{entry.get('calendar')}' found"))
                else:
                    checks.append((f"conflict calendar '{name}'", "fail",
                                    f"calendar {entry.get('calendar')!r} not found among {sorted(cals)}"))
        # source = "booking_calendar" entries are covered by the booking
        # calendar check above -- nothing separate to reach.
    return checks


def _check_ics_conflict_source(name: str, url: str) -> Check:
    import urllib.error
    import urllib.request

    from . import ics_feed
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "my-booking-tool"})
        with urllib.request.urlopen(req, timeout=_CALDAV_CHECK_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (f"conflict calendar '{name}'", "warn",
                f"ICS fetch failed: {exc} -- bookings fall back to the last cached copy "
                "(see README.md \"Calendars\"); a WARNING email is sent when guests hit this")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        return (f"conflict calendar '{name}'", "fail",
                f"response from {url} is not an ICS calendar")
    feed = ics_feed.parse_feed(text)
    return (f"conflict calendar '{name}'", "ok",
            f"ICS feed fetched and parsed ({len(feed.events)} events)")


def check_calendar_invite_format(raw: dict, data_dir: str | Path) -> list[Check]:
    """2026-07-15: a real `setup -i` run once printed "[warn] couldn't
    check/resync calendar invite format: ..." and then, a few lines
    later, "Done -- all checks pass now" anyway -- setup and health should
    BOTH repeat any warn/error at the end AND exit 1 on any warning or
    error; this specific case had been classified as a warning, but is
    actually treated as an error. The resync ATTEMPT (a live
    CalDAV write) only ever happens in `setup -i` -- see
    app.calendar_sync.resync_if_format_changed() -- but whether it
    actually SUCCEEDED is a cheap, local, no-network fact: does the
    `.calendar_invite_format_version` marker under `data_dir` match
    app.calendar_sync.CALENDAR_INVITE_FORMAT_VERSION right now? This is
    that fact, as a real re-checkable Check -- included in build_report()
    (so plain `setup`/`admin health`, and `setup -i`'s own FRESH final
    re-check, all see it, repeat it, and factor it into the exit code),
    unlike the previous version of this feature, which only ever printed
    an un-recorded, un-recounted line during the walkthrough itself.

    A no-op (empty list, matching every other CalDAV-adjacent check's own
    "not configured yet" convention) if CalDAV isn't fully configured --
    nothing to be stale about yet."""
    cal = raw.get("booking_calendar", {})
    if not cal.get("caldav_url") or not cal.get("username") or not cal.get("password_file"):
        return []
    from . import calendar_sync as app_calendar_sync

    marker_path = Path(data_dir) / app_calendar_sync.CALENDAR_INVITE_FORMAT_VERSION_MARKER_NAME
    try:
        recorded = marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = None
    current = str(app_calendar_sync.CALENDAR_INVITE_FORMAT_VERSION)
    if recorded == current:
        return [("calendar invite format", "ok", f"up to date (v{current})")]
    return [(
        "calendar invite format", "warn",
        f"not yet resynced to the current format (v{current}, marker says {recorded!r}) -- "
        "run `my-bt admin setup -i` or `my-bt admin resync-calendar`",
    )]


def check_calendar_invite_resync_skips(data_dir: str | Path) -> list[Check]:
    """2026-07-15/16: a real production `setup -i` run showed 3
    occurrences hitting persistent CalDAV conflicts during a resync (see
    app.calendar_sync.resync_all_future_calendar_events's own docstring),
    got skipped, and the run still printed "[ok] ... resynced 6 upcoming
    occurrence(s)" then "Done -- all checks pass now" -- because the
    marker check above only ever asks "did an attempt happen for the
    current format version", never "did every occurrence in that attempt
    actually succeed". "-- 13. Calendar invite format -- says 'OK' but
    if you look at the output... I am NOT so sure!"

    This is that second question, as its own re-checkable Check: reads
    app.calendar_sync.CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME (written
    by resync_if_format_changed()/record_resync_skips() after every real
    resync attempt, automatic or manual via `my-bt admin resync-
    calendar`) -- no network call, so this is exactly as safe to run from
    plain `admin health` as check_calendar_invite_format() above. A
    missing/empty marker means the last attempt (if any) was fully
    clean -- `warn` (not `fail`): a stuck occurrence needs a human to
    look at the CalDAV server, not something this tool can force-fix by
    retrying harder, but it's still a real, actionable gap, same
    reasoning as every other warn-that-exits-1 check here."""
    from . import calendar_sync as app_calendar_sync

    marker_path = Path(data_dir) / app_calendar_sync.CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
    try:
        lines = [ln for ln in marker_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        lines = []
    if not lines:
        return [("calendar invite resync", "ok", "no unresolved conflicts from the last resync attempt")]
    return [(
        "calendar invite resync", "warn",
        f"{len(lines)} occurrence(s) failed to resync on the last attempt (persistent CalDAV "
        "conflict) -- run `my-bt admin resync-calendar` to retry: " + "; ".join(lines),
    )]


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
    disk but not live yet. Hit in practice 2026-07-05: after a
    course description was edited on the server, the file was correct, but the
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
    "/courses", "/book/", "/cancel/", "/reinstate/", "/host-cancel/", "/host-reinstate/",
    "/host-cancel-occurrence/", "/my", "/admin",
    # 2026-07-13: added after a real production gap -- this location block
    # didn't exist at all, so GET /schedule-exceptions (index.html's own
    # <script> fetches this to render the ATTENTION banner -- see
    # app/webapp.py::schedule_exceptions) fell through to nginx trying to
    # serve it as a static file and 404ing, and nothing here caught it.
    "/schedule-exceptions",
    # 2026-07-13: the browser's own CSP violation-report target (see
    # site/nginx-locations.conf.example's report-uri directive and
    # app/webapp.py::csp_report) -- added alongside the frame-ancestors
    # fix for the same real production incident, so a report POST has
    # somewhere to land instead of also 404ing at the nginx level.
    "/csp-report",
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
                            "/opt/my-booking/site/my-booking.conf.example"))
    return checks


# Fixed, generic filename for the (optional) real, hand-hardened nginx
# vhost conf kept directly in this checkout's site/ dir -- see
# site/nginx-locations.conf.example. 2026-07-10: renamed from a
# domain-specific filename to this fixed one, so every OTHER
# real-vs-.example pair in site/
# (index.html, impressum.html, terms.html, privacy.html.tmpl) uses
# the same one fixed, domain-agnostic name; the nginx conf used to be the one
# exception (named after the operator's own domain), which meant this module had
# to glob for "*.conf" instead of just checking one known path. Fixed name
# everywhere now -- no more globbing, no more guessing.
_NGINX_CONF_FILENAME = "nginx-locations.conf"


def check_nginx_conf_repo_file(home: str) -> list[Check]:
    """Whether a real, filled-in nginx vhost conf file exists at the fixed
    site/nginx-locations.conf path in this checkout, and if so, whether it
    still has every location block this app currently needs
    (_REQUIRED_NGINX_LOCATIONS) and no leftover REPLACE-ME placeholder.

    This exists to catch, at the SOURCE, the exact drift that actually
    happened 2026-07-10: a new route (/reinstate/, /host-reinstate/) was
    added to the app, and both nginx/my-booking.conf(.example) and
    check_nginx_locations()'s own required-list were updated for it -- but
    the separate, real, hand-hardened nginx conf sitting in this repo was
    NOT, and nothing caught that gap until it was noticed by manual inspection.
    check_nginx_locations() above only ever looks at the LIVE,
    already-deployed `nginx -T` output, so it can't help BEFORE a stale
    file like this is actually deployed; this check looks at the file
    still sitting in the checkout, so it can catch the gap even before
    `nginx -t && systemctl reload nginx` runs.

    Advisory only (warn, never fail) if no real conf file exists yet --
    a from-scratch install legitimately has none until you've hardened
    one, same as [site].static_site_dir being optional elsewhere in this
    module."""
    f = Path(home) / "site" / _NGINX_CONF_FILENAME
    if not f.exists():
        return [(f"nginx vhost conf (site/{_NGINX_CONF_FILENAME})", "warn",
                  f"no real, personal nginx vhost conf file found yet -- copy "
                  f"site/{_NGINX_CONF_FILENAME}.example there as a hardened starting "
                  "point, or nginx/my-booking.conf for a bare-bones one")]
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
                          "nginx/my-booking.conf for the bare version to adapt")
    if problems:
        return [(f"nginx vhost conf (site/{_NGINX_CONF_FILENAME})", "warn", "; ".join(problems))]
    return [(f"nginx vhost conf (site/{_NGINX_CONF_FILENAME})", "ok",
              "has every required location block, no leftover placeholder marker")]


def check_nginx_conf_deployed(raw: dict) -> list[Check]:
    """If `[site].nginx_conf_path` is configured, reads THAT exact file
    directly off disk -- not `nginx -T`'s merged dump (check_nginx_locations()
    above) and not a glob over this checkout's own site/*.conf
    (check_nginx_conf_repo_file() above) -- and checks it has every
    location block this app needs (_REQUIRED_NGINX_LOCATIONS) and no
    leftover REPLACE-ME placeholder.

    Reading the real path directly rather than `nginx -T` means this
    works even without the nginx binary on PATH/reachable, and reflects
    exactly the bytes nginx will load from this path the next time it's
    reloaded -- the most authoritative source short of `nginx -T` itself.

    Unlike every other optional check gated on a settings.toml path
    ([site].static_site_dir, [watchdog].nginx_access_log, ...), a problem
    found HERE is reported as "fail", not "warn" (2026-07-10: this path
    can actually be checked for correctness, so a real problem should
    truly ERROR out, not just warn) -- configuring this path at all is a
    deliberate statement that this file is real and matters, so a gap in
    it is treated as a hard failure, the same way a missing/broken secret
    already is in check_secrets()."""
    path_str = raw.get("site", {}).get("nginx_conf_path")
    if not path_str:
        return []
    p = Path(path_str)
    if not p.exists():
        live_file = _live_nginx_conf_file_for_host(raw)
        if live_file is not None and live_file.exists() and live_file.resolve() != p.resolve():
            return [(f"nginx vhost conf ({path_str})", "fail",
                      f"configured but not found -- nginx currently loads this vhost from "
                      f"{live_file} instead; point [site].nginx_conf_path at it (`my-bt setup -i` "
                      "can do this for you), or rename the file to match nginx_conf_path instead")]
        return [(f"nginx vhost conf ({path_str})", "fail",
                  "configured but not found -- check [site].nginx_conf_path")]
    text = p.read_text(encoding="utf-8", errors="replace")
    problems = []
    if _PLACEHOLDER_MARKER in text:
        problems.append(f'still contains a "{_PLACEHOLDER_MARKER}" placeholder marker -- '
                          "was the .example template deployed as-is?")
    missing = [
        path for path in _REQUIRED_NGINX_LOCATIONS
        if not re.search(rf"^\s*location\s+(?:[=~^]+\*?\s+)?{re.escape(path)}\s*\{{", text, re.MULTILINE)
    ]
    if missing:
        problems.append(f"missing location block(s) for {', '.join(missing)}")
    if problems:
        return [(f"nginx vhost conf ({path_str})", "fail", "; ".join(problems))]
    return [(f"nginx vhost conf ({path_str})", "ok",
              "has every required location block, no leftover placeholder marker")]


def _resolve_nginx_conf_checkout_source(home: str) -> Path | None:
    """This checkout's own copy of the nginx vhost conf, whatever's
    actually deployed at [site].nginx_conf_path -- your real
    site/nginx-locations.conf if you keep one there, falling back to the
    generic site/nginx-locations.conf.example. None if neither exists,
    e.g. running against a server whose vhost was never added to this
    checkout at all. Used only to offer a vimdiff in `setup -i`, mirroring
    `_resolve_static_source`'s same real-then-.example fallback for the
    static html pages. Unlike an earlier version of this function, this NO
    LONGER depends on nginx_conf_path's own basename -- the checkout side
    always uses the one fixed filename (_NGINX_CONF_FILENAME), regardless
    of what nginx_conf_path is called on the live server."""
    real = Path(home) / "site" / _NGINX_CONF_FILENAME
    if real.exists():
        return real
    example = Path(home) / "site" / f"{_NGINX_CONF_FILENAME}.example"
    if example.exists():
        return example
    return None


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


_CONF_FILE_MARKER_RE = re.compile(r"^# configuration file (\S+):\s*$", re.MULTILINE)


def _live_nginx_conf_file_for_host(raw: dict) -> Path | None:
    """Which actual file, right now, nginx says the vhost matching
    [site].base_url's hostname is defined in -- parsed from `nginx -T`'s
    own "# configuration file <path>:" markers, which precede every file
    it dumps (including every conf.d/*.conf resolved via `include`, not
    just nginx.conf itself). Lets `setup -i` notice a vhost that's still
    deployed under an OLD filename and offer to rename it to match
    [site].nginx_conf_path, without the operator ever having to tell this tool
    what that old name is (2026-07-10: this exact gap after the
    site/booking.example.org.conf -> site/nginx-locations.conf rename -- the real
    server's file hadn't been renamed to match yet, and nginx_conf_path's
    own check could only say "not found", not point at where it actually
    still is). None if nginx/the vhost/its enclosing file marker can't be
    determined at all."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    block = _matching_server_block(raw, merged)
    if block is None:
        return None
    idx = merged.find(block)
    if idx == -1:
        return None
    markers = list(_CONF_FILE_MARKER_RE.finditer(merged[:idx]))
    if not markers:
        return None
    return Path(markers[-1].group(1))


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


_ACCESS_LOG_RE = re.compile(r"^[ \t]*access_log[ \t]+([^\s;]+)(?:[ \t]+[^\s;]+)*[ \t]*;", re.MULTILINE)
# Real production bug, 2026-07-10: the previous version used `\s` (which
# matches newlines too) for every gap AND let the path capture group itself
# swallow a trailing `;` -- harmless as long as backtracking still found a
# semicolon on the SAME line, which it always did in this module's own
# tests (every fixture there has a log format name like "main" between the
# path and the `;`, so the greedy path match stopped at the space before
# it). A real production nginx-locations.conf has no format name --
# `access_log /var/log/nginx/booking.example.org.access.log;` with the `;` directly
# against the path -- so the path group greedily consumed the `;` too, and
# rather than backtracking, `(?:\s+\S+)*` happily matched on into the NEXT
# line's `error_log ...;` to find a semicolon to close the pattern with,
# leaving the detected "path" as .../booking.example.org.access.log; (semicolon and
# all). Silent for years because nothing ever WROTE this detected value
# anywhere -- #78 (this same day) added the first code path that actually
# persists it into settings.toml on accept, which is what turned this from
# a latent bug into a real corrupted nginx_access_log setting in production
# ("watchdog: nginx_access_log (.../booking.example.org.access.log;): configured but
# doesn't exist"). Fixed by confining every gap to `[ \t]` (same line only,
# same fix root_re/name_re already apply above) and excluding `;` from
# every captured/skipped token outright, so the path itself can never
# include one, regardless of what does or doesn't follow it on that line.


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


_ERROR_LOG_RE = re.compile(r"^[ \t]*error_log[ \t]+([^\s;]+)(?:[ \t]+[^\s;]+)*[ \t]*;", re.MULTILINE)
# Mirrors _ACCESS_LOG_RE above exactly (same real 2026-07-10 bug/fix this
# comment there explains applies here too -- confined to [ \t] and `;`
# excluded from every captured/skipped token).


def _nginx_error_log_for_host(raw: dict) -> str | None:
    """Live `error_log` path for the vhost matching `[site].base_url`'s
    hostname -- mirrors _nginx_access_log_for_host() exactly, for
    error_log instead of access_log. Added 2026-07-13 for `my-bt admin
    health report`/`errors` (see health_report_log_sources() below);
    nginx's error_log has no "off" spelling (unlike access_log), so
    that's not excluded here."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    block = _matching_server_block(raw, merged)
    for text in filter(None, (block, _strip_server_blocks(merged))):
        m = _ERROR_LOG_RE.search(text)
        if m and not m.group(1).startswith("syslog:"):
            return m.group(1)
    return None


def _nginx_global_access_log(raw: dict) -> str | None:
    """nginx's own http-level `access_log` -- the one OUTSIDE any
    `server {}` block, covering every vhost on this box, not just the one
    matching `[site].base_url` (that's `_nginx_access_log_for_host()`
    above). Distinct on purpose: a problem can show up in nginx's global
    log (or another vhost's traffic on the same box) that would never
    appear in the booking.example.org-specific one, and `my-bt admin health
    report`/`errors` (the operator's own explicit ask: "this includes nginx
    global logs, nginx yoga logs, my-bt logs and anything else...") wants
    both, not just the vhost-specific one the watchdog's burst check
    already covers."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    m = _ACCESS_LOG_RE.search(_strip_server_blocks(merged))
    if m and m.group(1) not in ("off",) and not m.group(1).startswith("syslog:"):
        return m.group(1)
    return None


def _nginx_global_error_log(raw: dict) -> str | None:
    """Mirrors _nginx_global_access_log() above, for error_log."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    m = _ERROR_LOG_RE.search(_strip_server_blocks(merged))
    if m and not m.group(1).startswith("syslog:"):
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
    for name in (site_render.OUTPUT_NAME, site_render.EMBEDDED_OUTPUT_NAME) + _STATIC_PAGES_TO_DEPLOY:
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


def _diffable_static_page_text(text: str) -> str:
    """Strips `my-bt admin site-maintenance on`'s banner block (if present) before
    comparing a deployed static page against this checkout's own source
    (2026-07-10: `setup -i` should know about maintenance mode and
    ignore any change caused only by that banner, rather than proposing
    a vimdiff for it). Without this, `index.html` legitimately differs
    from the checkout's copy the ENTIRE time maintenance mode is on (the
    banner is inserted directly into the live file, see app/maintenance.py
    -- that's the whole point, it needs to show up immediately) --
    `check_static_pages_deployed()` and `interactive_setup()`'s vimdiff
    offer would otherwise both treat that expected, deliberate difference
    as drift needing a manual merge, every single time, for as long as
    maintenance mode stays on. `maintenance.remove_banner()` is a safe
    no-op on any text that never had the banner inserted (its regex only
    matches the exact delimiter comments my-bt itself writes), so this is
    applied unconditionally to every static page's text, not just
    index.html specifically -- there's nothing to strip on the other two
    pages, and this stays correct even if banner insertion ever extends to
    them."""
    return maintenance.remove_banner(text)


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
        same = _diffable_static_page_text(deployed.read_text(encoding="utf-8", errors="replace")) == \
            _diffable_static_page_text(source.read_text(encoding="utf-8", errors="replace"))
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


def fetch_active_sessions(internal_url: str = _DEFAULT_INTERNAL_URL) -> tuple[dict | None, str | None]:
    """GETs {internal_url}/internal/status (see app/webapp.py::
    internal_status) directly and returns the RAW payload -- not just a
    session count (see check_active_sessions() below for that) -- so a
    caller can show WHO is logged in, since when, and how long they have
    left with no further activity, not just how many. Returns (payload,
    error): exactly one set, same fail-open contract used everywhere
    session-awareness matters in this project (the RPM's own %pre gate,
    app.cli_setup._default_check_active_sessions) -- an unreachable
    service means nothing is running to protect, not a reason to
    refuse."""
    url = internal_url.rstrip("/") + "/internal/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - fixed http://127.0.0.1 URL
            return json.loads(resp.read().decode("utf-8")), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def format_session_timeout(seconds: int | None) -> str:
    """Human label for SESSION_TTL_SECONDS (app/webapp.py) -- e.g. "4h" or
    "1h30m" -- the fixed inactivity window after which a session with no
    new activity is gone. Same value for every session (it isn't tracked
    per-session), shown per-row anyway to match how the overview table
    below was asked for."""
    if not seconds:
        return "?"
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def _fmt_session_ts(iso: str | None) -> str:
    if not iso:
        return "(none yet)"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def active_sessions_rows(payload: dict) -> list[dict]:
    """Shapes /internal/status's raw "sessions" list into the one row
    shape every active-sessions view in this project shows -- `my-bt
    status`'s "logged-in users" table (which also keeps its own existing
    "last page" column, still built here so it can't drift either),
    `my-bt setup`'s active-session gate/warning, and (indirectly) the
    RPM's own %pre gate, which just shells out to `my-bt status` and
    reuses its rendering verbatim. One definition, not three
    independently drifting copies."""
    timeout_label = format_session_timeout(payload.get("session_timeout_seconds"))
    rows = []
    for s in payload.get("sessions", []):
        is_admin = s.get("kind") == "admin"
        rows.append({
            "name": "admin" if is_admin else (s.get("name") or "(no name on file)"),
            "email": "-" if is_admin else s.get("who", ""),
            "session start": _fmt_session_ts(s.get("connected_since")),
            "last page": s.get("last_page") or "(none yet)",
            "last activity": _fmt_session_ts(s.get("last_seen")),
            "timeout (no activity)": timeout_label,
        })
    return rows


def format_active_sessions_overview(payload: dict) -> str:
    """The full printable block for "who's logged in right now" -- an
    aligned table (name/email/session start/last activity/timeout) plus
    the exact command to force-end one or every session. Shown by `my-bt
    setup -i`'s upfront hard-refusal gate and plain `my-bt setup`'s own
    warning section (app/cli_setup.py) -- same wording either way."""
    rows = active_sessions_rows(payload)
    if not rows:
        return "(no active sessions)"
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    lines = [header, "-" * len(header)]
    lines.extend("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) for r in rows)
    lines.append("")
    lines.append("Force-end a session with `my-bt admin logout EMAIL`, or every session with")
    lines.append("`my-bt admin logout --all`.")
    return "\n".join(lines)


def check_active_sessions(internal_url: str = _DEFAULT_INTERNAL_URL) -> list[Check]:
    """[] when nobody's logged in (or the service is unreachable --
    fail-open, nothing to report) -- else a single ("active sessions",
    "warn", ...) entry with a compact one-line summary. Part of
    build_report() (see below) so plain `my-bt setup`'s report -- which
    otherwise never touches the live process at all -- surfaces this the
    same way `my-bt status` already does; `print_report()` additionally
    prints the FULL overview (format_active_sessions_overview) right
    after, since a single line can't show who/since-when/timeout."""
    payload, _err = fetch_active_sessions(internal_url)
    sessions = payload.get("sessions") if payload else None
    if not sessions:
        return []
    who = ", ".join(s.get("who", "?") for s in sessions)
    return [(
        "active sessions", "warn",
        f"{len(sessions)} active session(s) right now ({who}) -- see `my-bt status`, "
        "or `my-bt admin logout EMAIL`/`--all` to force-end one or all",
    )]


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


def check_index_embedded_drift(raw: dict) -> list[Check]:
    """Mirrors check_static_site_drift above, for the second generated
    page: index_embedded.html, a no-JavaScript variant of the homepage
    meant for embedding this site via `<iframe>` on another site (see
    app/site_render.py::derive_index_embedded_html's own docstring). Unlike
    privacy.html, there's no separate hand-maintained template for this
    page -- it's DERIVED straight from the LIVE, currently-deployed
    index.html (not this checkout's own copy: the live file is the
    authoritative source for whatever's actually being embedded right now)
    plus whatever upcoming [[course.date_override]] entries settings.toml
    currently has. Compares that derived text against the LIVE deployed
    index_embedded.html itself: this page can't fetch date_override changes
    live via JavaScript the way site/index.html's own small script does
    (that's the whole point of it -- no scripts at all), so staying in sync
    depends entirely on regenerating it whenever index.html or the schedule
    changes.

    A no-op (empty list) unless [site].index_embedded_enabled is true --
    this whole mechanism is opt-in (most deployments don't embed their site
    via iframe elsewhere), so its being off isn't itself something to
    report, unlike privacy.html.tmpl, which every install needs real legal
    text for.

    Compares with the maintenance banner stripped from both sides (same
    `_diffable_static_page_text` helper check_static_pages_deployed()
    already uses for index.html) -- `my-bt admin site-maintenance on/off`
    inserts/removes that banner directly in the deployed file (see
    app/maintenance.py, scripts/my-bt::cmd_maintenance), so an active
    maintenance window must never look like drift here."""
    site = raw.get("site", {})
    if not site.get("index_embedded_enabled"):
        return []
    static_site_dir = site.get("static_site_dir")
    if not static_site_dir:
        return []

    index_path = Path(static_site_dir) / "index.html"
    if not index_path.exists():
        return [("index_embedded.html", "warn",
                  f"index_embedded_enabled is true, but {index_path} isn't deployed yet -- "
                  "nothing to derive index_embedded.html from")]

    courses = config.courses_from_raw(raw)
    today = config.today_in_raw_timezone(raw)
    base_url = (site.get("base_url") or "").rstrip("/")
    new_tab_links = bool(site.get("index_embedded_new_tab_links", True))
    index_html_text = index_path.read_text(encoding="utf-8", errors="replace")
    try:
        expected = site_render.derive_index_embedded_html(
            index_html_text, courses, today, base_url, new_tab_links,
        )
    except site_render.IndexEmbeddedDerivationError as exc:
        return [("index_embedded.html", "fail", str(exc))]

    deployed_path = Path(static_site_dir) / site_render.EMBEDDED_OUTPUT_NAME
    if not deployed_path.exists():
        return [(f"index_embedded.html ({deployed_path})", "warn",
                  "not deployed yet -- run `my-bt setup -i` to derive and deploy it")]
    actual = deployed_path.read_text(encoding="utf-8", errors="replace")
    if _diffable_static_page_text(actual) == _diffable_static_page_text(expected):
        return [(f"index_embedded.html ({deployed_path})", "ok", "matches current index.html + settings.toml")]
    return [(
        f"index_embedded.html ({deployed_path})", "warn",
        "doesn't match what index.html + settings.toml would currently derive -- "
        "run `my-bt setup -i` to regenerate",
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
_SITE_PAGES = ("index.html", "impressum.html", "terms.html", "privacy.html", site_render.EMBEDDED_OUTPUT_NAME)


def _my_booking_can_read(path: Path) -> bool | None:
    """Actually ASKS the OS whether the my-booking user can read `path`
    (via `runuser`), rather than reimplementing POSIX permission logic
    ourselves -- hit in practice 2026-07-05: an earlier version of this
    check inspected `st_mode` bits directly (owner/group/other), which is
    blind to POSIX ACLs. `setfacl` (the fix this check itself recommends!)
    grants access via an ACL entry, not a mode-bit change -- so the old
    check kept reporting "can't read" even immediately after running
    the exact setfacl command it printed, nagging the operator to redo
    a fix that had already worked. `runuser -u my-booking -- test -r <path>` asks
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
    unprivileged user by default -- this is the concrete gap hit in
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


# -- CSP violation scanning (2026-07-13) --------------------------------------
#
# the operator had been manually clicking through every page after any inline-
# <script> edit to catch a stale CSP script-src hash (see the real
# incidents documented in site/nginx-locations.conf.example's own CSP
# comment -- this bit the schedule-exceptions banner and the booking-form's
# MAX_GUESTS validation, each silently, with no error but a browser-console
# CSP violation). The browser already reports every violation to
# app/webapp.py::csp_report (logged at WARNING, [logging].log_file), so
# this scans THAT instead of requiring a manual page-by-page click-through.
#
# `find_csp_violations()` is the one place that knows how to parse those
# log lines -- `check_csp_violations()` below (used by `my-bt health`/
# `admin setup`) and app/watchdog.py's own threshold-gated alert both call
# it directly rather than keeping separate copies of this parsing.

_APP_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")

_CSP_VIOLATION_RE = re.compile(
    r"CSP violation report from \S+: blocked-uri=(?P<blocked>.*?) "
    r"violated-directive=(?P<directive>.*?) document-uri=(?P<doc>.*)$"
)
_CSP_UNPARSEABLE_RE = re.compile(r"CSP violation report from \S+: unparseable body:")


def parse_app_log_timestamp(line: str) -> datetime | None:
    """Parses the leading "%(asctime)s %(levelname)s ..." timestamp
    app/logutil.py::configure_logging's formatter puts on every line of
    [logging].log_file (asctime's default format is "YYYY-MM-DD
    HH:MM:SS,mmm") -- `None` if `line` doesn't start with one (e.g. a
    continuation line of a multi-line traceback). Shared by
    find_csp_violations() below and app/watchdog.py's own app-log checks
    (check_app_log_rate_limit_blocks) -- one place that knows this format,
    not a second copy per module."""
    m = _APP_LOG_TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def find_csp_violations(
    lines: Iterable[str], window_minutes: int, now: datetime | None = None,
) -> list[tuple[int, str]]:
    """Scans already-read [logging].log_file lines for CSP violation
    reports (app/webapp.py::csp_report, logged at WARNING) within the last
    `window_minutes`, groups identical (violated-directive, blocked-uri,
    document-uri) combinations together, and returns (count, detail) pairs
    sorted by count descending -- most frequent first, so a single stale
    script hash (which fires on EVERY load of the affected page) doesn't
    bury a rarer, different violation under a wall of near-identical
    lines. A malformed/unparseable report body (see csp_report's own
    except clause) is grouped into its own single "(unparseable report
    body)" bucket rather than being dropped silently.

    Pure -- no file I/O here, so this is unit-testable without a real log
    file. Lines whose own leading timestamp can't be parsed are excluded
    (same fail-closed convention app/watchdog.py's log-based checks
    already use), not counted as "in window" by default.

    check_csp_violations() below is the real-file wrapper `my-bt health`/
    `setup` use; `my-bt admin csp-violations` shows this same data in
    full; app/watchdog.py's own check_csp_violations() calls this
    directly too, threshold-gated, rather than re-parsing the log itself.
    build_health_report() (below) also reuses group_csp_violation_lines(),
    the actual grouping logic, on a set of lines ALREADY windowed by
    `my-bt admin health errors`, rather than re-applying a second time
    window on top of that one."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=window_minutes)
    windowed = (line for line in lines if (ts := parse_app_log_timestamp(line)) is not None and ts >= since)
    return group_csp_violation_lines(windowed)


def group_csp_violation_lines(lines: Iterable[str]) -> list[tuple[int, str]]:
    """The grouping half of find_csp_violations() above, with no time
    filtering at all -- for callers (build_health_report()'s `errors`
    mode) that have already restricted `lines` to the window they care
    about and don't want a SECOND, redundant timestamp check applied on
    top of that."""
    counts: dict[str, int] = {}
    for line in lines:
        if "CSP violation report from" not in line:
            continue
        m = _CSP_VIOLATION_RE.search(line)
        if m:
            key = (
                f"blocked-uri={m.group('blocked')} violated-directive={m.group('directive')} "
                f"document-uri={m.group('doc').rstrip()}"
            )
        elif _CSP_UNPARSEABLE_RE.search(line):
            key = "(unparseable report body -- possibly not a real browser CSP report)"
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    return sorted(((n, detail) for detail, n in counts.items()), key=lambda t: -t[0])


def check_csp_violations(raw: dict, now: datetime | None = None) -> list[Check]:
    """[] if [logging].log_file isn't configured, doesn't exist, or has no
    CSP violation reports within [watchdog].window_minutes -- else a
    single ("CSP violations", "warn", ...) Check summarizing the grouped
    counts (see find_csp_violations()), so `my-bt health`/`admin setup`
    surface a stale script-src hash or a rogue embed attempt without
    the operator having to click through every page by hand. Full, ungrouped
    detail: `my-bt admin csp-violations`. Always shown when found --
    NOT threshold-gated the way app/watchdog.py's own alert is, since this
    is diagnostic information, not an alerting decision.

    KNOWN GAP (confirmed live, 2026-07-13): this only ever reads the FILE
    at [logging].log_file -- with that unconfigured, a systemd service's
    own stdout (where every log.warning() call, including a CSP violation
    report, still goes by default) is only visible via `journalctl -u
    my-booking.service`, which this function does NOT read. `my-bt admin
    health`/`admin setup`/`admin csp-violations` will all silently show
    nothing in that case, even though the violation IS visible via `my-bt
    admin health errors` (build_health_report() below explicitly gathers
    that journal too, and groups CSP violations found in it). The real
    fix is configuring [logging].log_file (also required for the
    watchdog's own threshold-gated alert) -- not worth duplicating
    journalctl-reading into every one of these check functions just to
    paper over not having it configured."""
    log_path_str = config.log_file_from_raw(raw)
    if not log_path_str:
        return []
    window_minutes = int(raw.get("watchdog", {}).get("window_minutes", 15))
    try:
        with open(log_path_str, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    violations = find_csp_violations(lines, window_minutes, now=now)
    if not violations:
        return []
    total = sum(n for n, _ in violations)
    shown = violations[:5]
    summary = "; ".join(f"{n}x {detail}" for n, detail in shown)
    if len(violations) > len(shown):
        summary += f" (+{len(violations) - len(shown)} more distinct)"
    return [(
        "CSP violations", "warn",
        f"{total} CSP violation report(s) in the last {window_minutes} min: {summary} -- "
        "see `my-bt admin csp-violations` for full detail",
    )]


# -- CSP hash automation (2026-07-13) ------------------------------------------
#
# check_csp_violations() above catches a stale hash only AFTER a real browser
# has hit it and reported back -- reactive, and only as good as
# [logging].log_file/journal visibility. This section is the proactive half:
# it knows, from the app's own source, every inline <script> body my-bt can
# currently produce, computes what its CSP hash SHOULD be, and compares that
# against what's actually allow-listed in the live, deployed nginx config --
# so a forgotten hash update shows up in `my-bt admin health`/`admin setup`
# BEFORE a single guest's browser ever has to report the violation. This is
# the third time this exact class of bug has hit production (see
# site/nginx-locations.conf's own dated incident notes: the schedule-
# exceptions script's hash went stale twice, the booking-form script's once)
# -- the operator asked for this to be automated rather than caught by hand a fourth
# time.

def expected_csp_hashes(raw: dict) -> dict[str, str]:
    """Returns {label: 'sha256-...'} for every inline <script> body this app
    can currently produce: the 8 static, non-interpolated Python module
    constants (app/templates.py's _SUBMIT_FEEDBACK_SCRIPT, appended to every
    page, plus app/webapp.py's 7 per-page scripts -- always present,
    regardless of settings.toml), and -- only if `[site].static_site_dir` is
    configured -- the two <script> blocks in the LIVE, currently-DEPLOYED
    index.html at that path (not necessarily this checkout's own
    site/index.html, which can legitimately differ mid-rollout).

    Every one of these constants is deliberately written so its body never
    changes between renders (no f-string interpolation of any per-request/
    per-setting value -- see each constant's own history in
    site/nginx-locations.conf.example's CSP comment for the real bugs hit
    before that was true), so hashing the *source* constant directly, without
    ever rendering a page, is safe and always reflects exactly what a real
    render would have produced -- confirmed byte-for-byte for
    _BOOKING_FORM_SCRIPT, the most recently extracted one, at the time it was
    refactored out of _book_page()'s f-string."""
    # Local import (not at module level): app/webapp.py has a very large,
    # heavy import chain of its own (calendar_sync, caldav_client, cancel_flow,
    # emailer, ...) that this module has no other reason to pull in just to
    # check installation health -- lazy import keeps that cost (and any risk
    # of an accidental future circular import) out of cli_checks.py's own
    # module load.
    from . import templates, webapp

    def _hash(body: str) -> str:
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        return "sha256-" + base64.b64encode(digest).decode()

    def _body_of(script_tag_text: str) -> str:
        # Reuses site_render's own comment-safe extraction rather than a
        # fresh ad hoc regex -- see extract_script_bodies()'s own docstring
        # for the real incident (a literal "<script>" mentioned in HTML-
        # comment prose) that makes this worth sharing rather than
        # reimplementing per caller.
        bodies = site_render.extract_script_bodies(script_tag_text)
        if len(bodies) != 1:
            raise ValueError(
                f"expected exactly one <script> block, found {len(bodies)} "
                f"in: {script_tag_text[:80]!r}..."
            )
        return bodies[0]

    hashes: dict[str, str] = {}
    static_constants = (
        ("templates._SUBMIT_FEEDBACK_SCRIPT", templates._SUBMIT_FEEDBACK_SCRIPT),
        ("webapp._RESEND_COOLDOWN_SCRIPT", webapp._RESEND_COOLDOWN_SCRIPT),
        ("webapp._RESEND_INLINE_COOLDOWN_SCRIPT", webapp._RESEND_INLINE_COOLDOWN_SCRIPT),
        ("webapp._LOCKOUT_COUNTDOWN_SCRIPT", webapp._LOCKOUT_COUNTDOWN_SCRIPT),
        ("webapp._DIALOG_WIRING_SCRIPT", webapp._DIALOG_WIRING_SCRIPT),
        ("webapp._CANCEL_ENTIRE_SESSION_SCRIPT", webapp._CANCEL_ENTIRE_SESSION_SCRIPT),
        ("webapp._SORTABLE_FILTERABLE_TABLE_SCRIPT", webapp._SORTABLE_FILTERABLE_TABLE_SCRIPT),
        ("webapp._BOOKING_FORM_SCRIPT", webapp._BOOKING_FORM_SCRIPT),
    )
    for label, constant in static_constants:
        hashes[label] = _hash(_body_of(constant))

    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if static_site_dir:
        index_path = Path(static_site_dir) / "index.html"
        if index_path.exists():
            text = index_path.read_text(encoding="utf-8", errors="replace")
            for i, body in enumerate(site_render.extract_script_bodies(text), start=1):
                hashes[f"index.html script #{i}"] = _hash(body)

    return hashes


_CSP_HEADER_RE = re.compile(
    r'add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always\s*;', re.IGNORECASE
)
_CSP_HASH_RE = re.compile(r"'(sha256-[A-Za-z0-9+/=]+)'")


def _nginx_csp_script_hashes(raw: dict) -> set[str] | None:
    """The exact set of 'sha256-...' script-src hashes currently present in
    the LIVE nginx Content-Security-Policy header for [site].base_url's own
    vhost, or None if nginx/the vhost/its CSP header can't be determined at
    all (deliberately distinct from an empty set, which specifically means
    the header WAS found but has zero hashes -- itself worth flagging by the
    caller, not silently treated the same as "couldn't check")."""
    merged = _live_nginx_config(raw)
    if merged is None:
        return None
    block = _matching_server_block(raw, merged)
    if block is None:
        return None
    header_match = _CSP_HEADER_RE.search(block)
    if not header_match:
        return None
    return set(_CSP_HASH_RE.findall(header_match.group(1)))


def check_csp_hashes_deployed(raw: dict) -> list[Check]:
    """Compares expected_csp_hashes() (every inline <script> this app can
    currently produce) against the live nginx CSP header's actual script-src
    hash set -- catches a forgotten hash update BEFORE a browser has to
    report the violation itself (contrast check_csp_violations() above,
    which is purely reactive). A no-op (empty list) if nginx/the vhost/its
    CSP header can't be determined at all -- same "can't check what isn't
    reachable" convention as every other _live_nginx_config-based check in
    this module (e.g. check_nginx_locations()); this is a defense-in-depth
    extra, not a replacement for a human being able to read the header by
    hand. "warn", not "fail": a missing hash breaks only that ONE script's
    own behavior, not the whole site, same severity class as a missing
    `location` block."""
    deployed = _nginx_csp_script_hashes(raw)
    if deployed is None:
        return []
    expected = expected_csp_hashes(raw)
    missing = [(label, h) for label, h in expected.items() if h not in deployed]
    if not missing:
        return [(
            "CSP script hashes deployed", "ok",
            f"all {len(expected)} expected inline <script> hash(es) present in the live CSP header",
        )]
    detail = "; ".join(f"{label} needs {h!r} added" for label, h in missing)
    return [(
        "CSP script hashes deployed", "warn",
        f"{len(missing)} of {len(expected)} inline <script> hash(es) missing from the live CSP "
        f"header -- {detail} (see site/nginx-locations.conf's script-src -- "
        '"ADD, never replace" -- an old hash is kept for rollback safety)',
    )]


def csp_script_src_patch(conf_text: str, hashes_to_add: list[str]) -> str:
    """Self-heal for check_csp_hashes_deployed()'s own "warn" (2026-07-16,
    the operator: "can we automate and fix this within the build or setup ... so
    that setup does not break out with an error but maybe can self-heal?"
    -- this used to be deliberately warn-only, since a bad CSP edit can
    break every script on the site, not just one; see this function's own
    caller in app/cli_setup.py::interactive_setup() for the nginx -t-
    verified, rollback-on-failure orchestration that makes adding this
    safe).

    Pure string transform -- no file I/O, no subprocess, so it's trivially
    unit-tested on its own. Returns `conf_text` with each hash in
    `hashes_to_add` appended (space-separated, single-quoted) right after
    the `script-src` token, additive only -- same "ADD, never replace"
    rollback-safety rule documented throughout site/nginx-locations.conf's
    own comments, nothing existing on that line or anywhere else in the
    file is touched. Raises ValueError if no Content-Security-Policy
    add_header line is found at all, or that line has no script-src
    directive -- the caller should treat either as "can't safely patch
    this file" and leave it untouched, not as "nothing to do" (contrast
    the empty-`hashes_to_add` case, which is a legitimate no-op returning
    `conf_text` unchanged)."""
    if not hashes_to_add:
        return conf_text
    match = _CSP_HEADER_RE.search(conf_text)
    if not match:
        raise ValueError("no Content-Security-Policy add_header line found")
    header_value = match.group(1)
    script_src_match = re.search(r"script-src(?=\s)", header_value)
    if not script_src_match:
        raise ValueError("no script-src directive found in the CSP header")
    insert_at = script_src_match.end()
    addition = "".join(f" '{h}'" for h in hashes_to_add)
    new_header_value = header_value[:insert_at] + addition + header_value[insert_at:]
    return conf_text[:match.start(1)] + new_header_value + conf_text[match.end(1):]


# -- Forensic log aggregation: `my-bt admin health report`/`errors` (2026-07-13) --
#
# the operator's own explicit ask: "my-bt health should also allow to collect
# information from all possible places for a given period ... in order to
# support investigating anything strange happening with booking.example.org", to
# include "nginx global logs, nginx yoga logs, my-bt logs and anything
# else relevant" -- proposed shape: `my-bt admin health report [--last Xh]
# [--since TS] [--till TS]` (every matching line, source-labeled) and
# `my-bt admin health errors [...]` (same window, filtered down to
# actual problems -- both a curated pass reusing the SAME detectors
# health/watchdog already have, e.g. find_csp_violations() above, AND a
# raw severity-based pass, so a brand-new kind of problem those don't
# already know about still shows up). Both default to "since nginx's own
# last restart" when no time flag is given.
#
# Log PATHS are deliberately never a new settings.toml entry: every one of
# them is derived live from `nginx -T` (see _nginx_global_access_log() et
# al above, and _nginx_access_log_for_host()/_nginx_error_log_for_host()
# already used by the watchdog config-drift check) or is simply
# [logging].log_file, already configured -- one less hardcoded path to
# keep in sync by hand (see the no-duplicated-hardcoded-paths lesson
# elsewhere in this project).

_DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)


def parse_last_duration(text: str) -> timedelta:
    """Parses a `--last` duration like "2h", "90m", "1h30m", "45s", "1d"
    into a timedelta -- any combination of d/h/m/s components, each
    optional, in that order. Raises ValueError on anything that doesn't
    match (including an empty string) -- scripts/my-bt's argparse
    `type=parse_last_duration` turns that into a normal "invalid value"
    error for the user, not a traceback."""
    text = text.strip()
    m = _DURATION_RE.match(text)
    if not m or not any(m.groups()):
        raise ValueError(f"invalid duration {text!r} -- expected e.g. '2h', '90m', '1h30m', '45s', '1d'")
    parts = {k: int(v) for k, v in m.groupdict(default="0").items()}
    return timedelta(days=parts["days"], hours=parts["hours"], minutes=parts["minutes"], seconds=parts["seconds"])


def _parse_iso_utc(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def nginx_last_restart_at() -> datetime | None:
    """When nginx.service last (re)started, or None if that can't be
    determined (not a systemd host, nginx not installed, never started,
    ...) -- the default start of `my-bt admin health report`/`errors`'s
    window when no --last/--since/--till is given (the operator's own explicit
    ask: "both without parameters showing you the report since the last
    reboot of nginx"). Reuses _service_active_since(), the same
    systemctl-show-then-`date -d` helper check_settings_fresh() already
    relies on for my-booking.service, just pointed at nginx.service
    instead -- one implementation, two callers."""
    epoch = _service_active_since("nginx.service")
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch is not None else None


def resolve_report_window(
    last: str | None = None, since: str | None = None, till: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    """Resolves `my-bt admin health report`/`errors`'s --last/--since/
    --till into a concrete (start, end, description) UTC window --
    `description` is a short human string for the report's own header.

    - --last (a duration, see parse_last_duration): `now` minus that,
      through `now`.
    - --since/--till (ISO-8601 timestamps; naive ones are treated as
      UTC): either or both may be given. An omitted --till defaults to
      `now`; an omitted --since (with --till given) defaults to 24h
      before that --till.
    - None of the three given: starts "since nginx's last restart" (see
      nginx_last_restart_at()), through `now` -- falling back to 24h ago
      if nginx's restart time can't be determined."""
    now = now or datetime.now(timezone.utc)
    if last:
        start = now - parse_last_duration(last)
        return start, now, f"last {last}"
    if since or till:
        end = _parse_iso_utc(till) if till else now
        start = _parse_iso_utc(since) if since else end - timedelta(hours=24)
        return start, end, f"{start.isoformat()} to {end.isoformat()}"
    restart = nginx_last_restart_at()
    if restart is not None:
        return restart, now, f"since nginx's last restart ({restart.isoformat()})"
    return now - timedelta(hours=24), now, "last 24h (nginx's restart time could not be determined)"


def health_report_log_sources(raw: dict) -> list[tuple[str, str | None]]:
    """Ordered (label, path) pairs for every FILE-based log source `my-bt
    admin health report`/`errors` aggregates -- nginx's own global access/
    error logs (outside any vhost -- covers every site on this box, not
    just this one), THIS vhost's own access/error logs (matching
    [site].base_url, same derivation the watchdog config-drift check
    already uses), and the app's own [logging].log_file. A None path
    means that source couldn't be determined right now (nginx unreachable,
    vhost not found, log_file not configured, ...) -- callers skip it,
    not an error. sshd/systemd-journal sources aren't file-based, so
    they're gathered separately (see scripts/my-bt::cmd_admin_health_report/
    cmd_admin_health_errors, which reach for journalctl directly, same as
    app/watchdog.py's own _sshd_lines_since())."""
    return [
        ("nginx global access log", _nginx_global_access_log(raw)),
        ("nginx global error log", _nginx_global_error_log(raw)),
        ("nginx vhost access log", _nginx_access_log_for_host(raw)),
        ("nginx vhost error log", _nginx_error_log_for_host(raw)),
        ("app log", config.log_file_from_raw(raw)),
    ]


_NGINX_ERROR_LOG_TS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def _nginx_error_log_timestamp(line: str) -> datetime | None:
    """nginx's own error_log timestamp format ("YYYY/MM/DD HH:MM:SS
    [level] ..."), always in the SERVER's local timezone (unlike the
    access log's $time_local, it carries no explicit UTC offset at all)
    -- treated as this machine's own local time, correct as long as
    `my-bt` runs on the same box as nginx (the only deployment shape this
    project supports)."""
    m = _NGINX_ERROR_LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone().astimezone(timezone.utc)


def _nginx_access_log_timestamp(line: str) -> datetime | None:
    from .watchdog import _NGINX_LINE_RE, _parse_nginx_timestamp
    m = _NGINX_LINE_RE.match(line)
    if not m:
        return None
    ts = _parse_nginx_timestamp(m.group("time"))
    return ts.astimezone(timezone.utc) if ts else None


def _filter_lines_by_window(lines: Iterable[str], start: datetime, end: datetime, parse_ts) -> list[str]:
    """Keeps only lines whose own timestamp (via `parse_ts`, one of the
    per-format parsers above) falls in [start, end] -- lines whose
    timestamp can't be parsed at all are excluded, same fail-closed
    convention app/watchdog.py's log-based checks already use."""
    kept = []
    for line in lines:
        ts = parse_ts(line)
        if ts is not None and start <= ts <= end:
            kept.append(line)
    return kept


def _is_error_status_line(line: str) -> bool:
    """access-log line worth showing in `errors` mode: HTTP status 4xx/5xx."""
    from .watchdog import _NGINX_LINE_RE
    m = _NGINX_LINE_RE.match(line)
    return bool(m and m.group("status").startswith(("4", "5")))


def _is_error_app_log_line(line: str) -> bool:
    """[logging].log_file line worth showing in `errors` mode -- anything
    at WARNING or above (app/logutil.py::configure_logging's own
    formatter always includes the level name right after the timestamp).
    This naturally includes CSP violations and rate-limiter rejections,
    both already logged at WARNING -- see find_csp_violations() above,
    used separately for the grouped/counted CSP summary appended to
    `errors` mode's own output."""
    return any(f" {level} " in line for level in ("WARNING", "ERROR", "CRITICAL"))


def build_health_report(
    raw: dict, start: datetime, end: datetime, description: str,
    sshd_lines: Iterable[str] = (), app_service_lines: Iterable[str] = (),
    errors_only: bool = False,
) -> str:
    """Assembles the full printable text for `my-bt admin health
    report`/`errors` -- every configured log source (see
    health_report_log_sources()), each filtered to [start, end] and
    source-labeled, plus the sshd/my-booking-service journal lines the
    caller already gathered (real I/O -- journalctl calls, scoped to this
    exact window -- happens in scripts/my-bt, not here, so this stays a
    pure, unit-testable function).

    `errors_only=False` ("report"): every matching line, verbatim,
    chronological order isn't enforced across sources (each source is
    printed as its own labeled section) -- this is raw material for a
    human to read, not a merged single timeline.

    `errors_only=True` ("errors"): access logs filtered to 4xx/5xx status,
    the app log filtered to WARNING-or-above, sshd/service journals kept
    as-is (already curated at the source -- see check_sshd_failures()'s
    same "Failed password" signal), PLUS a grouped/counted CSP-violation
    summary (find_csp_violations()) appended at the end -- combining the
    SAME curated detectors health/watchdog already have with a raw
    severity-based pass, so a kind of problem no existing detector knows
    about yet still shows up (the operator's own answer: "both")."""
    sshd_lines = list(sshd_lines)
    app_service_lines = list(app_service_lines)
    sections: list[str] = [f"my-bt admin health {'errors' if errors_only else 'report'} -- {description}\n"]
    app_log_window: list[str] = []
    for label, path in health_report_log_sources(raw):
        if not path:
            sections.append(f"=== {label} (not configured / not detected) ===\n")
            continue
        lines = _read_log_lines(path)
        if lines is None:
            sections.append(f"=== {label} ({path}) -- could not be read ===\n")
            continue
        is_access = "access log" in label
        parse_ts = _nginx_access_log_timestamp if is_access else _nginx_error_log_timestamp
        if label == "app log":
            parse_ts = parse_app_log_timestamp
        windowed = _filter_lines_by_window(lines, start, end, parse_ts)
        if label == "app log":
            app_log_window = windowed
        if errors_only:
            if is_access:
                windowed = [ln for ln in windowed if _is_error_status_line(ln)]
            elif label == "app log":
                windowed = [ln for ln in windowed if _is_error_app_log_line(ln)]
            # nginx error logs are already nothing-but-problems by nginx's
            # own design (error_log's configured level already excludes
            # info/debug noise) -- kept as-is.
        sections.append(f"=== {label} ({path}) -- {len(windowed)} line(s) ===\n" + "".join(windowed))
    if sshd_lines:
        sections.append(f"=== sshd (journalctl -u sshd) -- {len(sshd_lines)} line(s) ===\n"
                         + "".join(sshd_lines))
    if app_service_lines:
        sections.append(
            f"=== my-booking service/timers (journalctl) -- {len(app_service_lines)} line(s) ===\n"
            + "".join(app_service_lines)
        )
    if errors_only:
        # 2026-07-13, real gap hit in practice: without [logging].log_file
        # configured, health_report_log_sources()'s "app log" entry is
        # None -- app_log_window stays empty -- yet the app's own WARNING
        # lines (including CSP violation reports) still reach the systemd
        # journal via app_service_lines, since a systemd service's stdout
        # goes to the journal by default regardless of whether a log FILE
        # is also configured. Grouping only app_log_window meant this
        # summary silently produced nothing on exactly that (common)
        # setup, even though the raw violation was sitting right there in
        # the my-booking service/timers section above. Group across BOTH
        # sources -- whichever one(s) actually captured it.
        violations = group_csp_violation_lines(app_log_window + app_service_lines)
        if violations:
            summary = "; ".join(f"{n}x {detail}" for n, detail in violations)
            sections.append(f"=== CSP violations, grouped ===\n{summary}\n")
    return "\n".join(sections)


def _read_log_lines(path: str) -> list[str] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return None


# -- `my-bt status` 24h activity summary (2026-07-14) -------------------------
#
# "status should collect a 360-degree full status, including what's
# happening in the logs, and some log-related stats: how many sessions /
# logins in the last 24h" -- status stays FAST (summary counts, never log
# dumps; the dumps are `my-bt admin health report`/`log-errors`, pointed
# at from status's own output), reusing the same per-format timestamp
# parsers the health report already has rather than a second copy.

def count_recent_logins(store, now: datetime | None = None, hours: int = 24) -> int:
    """Distinct live accounts whose last_login_at falls within the last
    `hours`. last_login_at only ever stores each account's LATEST login
    (Store.touch_login overwrites in place), so this counts accounts that
    logged in, not individual login events -- the honest number this
    storage model can give without a new audit log, and labeled
    accordingly by `my-bt status` ("account(s) logged in")."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    n = 0
    for row in store.read_users(scope="live"):
        iso = row.get("last_login_at", "")
        if not iso:
            continue
        try:
            if datetime.fromisoformat(iso) >= since:
                n += 1
        except ValueError:
            continue
    return n


def count_recent_registrations(store, now: datetime | None = None, hours: int = 24) -> int:
    """Registrations created (any status -- confirmed, waitlisted, or
    still pending confirmation) within the last `hours`, live rows only.
    registered_at is stamped once at creation and only ever re-stamped by
    an explicit rebook (see Store.add_registration_checking_capacity), so
    this is a faithful "bookings made" count."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    n = 0
    for row in store.read_registrations(scope="live"):
        iso = row.get("registered_at", "")
        if not iso:
            continue
        try:
            if datetime.fromisoformat(iso) >= since:
                n += 1
        except ValueError:
            continue
    return n


def log_activity_stats(
    nginx_access_lines: list[str] | None,
    app_log_lines: list[str],
    now: datetime | None = None,
    hours: int = 24,
) -> dict:
    """Summary counts over the last `hours` for `my-bt status`:

    - nginx_requests / nginx_errors: total requests and their 4xx/5xx
      subset from the vhost access log's own lines (same combined-format
      parser the watchdog and `admin health errors` use). Both None when
      `nginx_access_lines` is None -- "couldn't read the log" must render
      as "(not available)", never as a false quiet 0.
    - app_warnings: WARNING-or-above lines among `app_log_lines` --
      whichever ONE source the caller picked ([logging].log_file when
      configured, else the service journal; both would double-count the
      same events, see cmd_status). File lines are windowed here via
      their own timestamps; journal lines arrive pre-windowed by
      journalctl --since and parse as in-window either way.

    Pure -- callers do the file/journal I/O (same split as
    build_health_report)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    stats: dict = {"nginx_requests": None, "nginx_errors": None, "app_warnings": 0}
    if nginx_access_lines is not None:
        windowed = _filter_lines_by_window(nginx_access_lines, since, now, _nginx_access_log_timestamp)
        stats["nginx_requests"] = len(windowed)
        stats["nginx_errors"] = sum(1 for ln in windowed if _is_error_status_line(ln))
    for line in app_log_lines:
        ts = parse_app_log_timestamp(line)
        if ts is not None and ts < since:
            continue  # a FILE line older than the window; journal lines (no
            # parseable file-format timestamp -> ts None) count as in-window
        if _is_error_app_log_line(line):
            stats["app_warnings"] += 1
    return stats
