"""Sitewide maintenance mode: `my-bt admin site-maintenance on/off/status`
(renamed from `admin maintenance` 2026-07-14, the operator: "rename my-bt admin
maintenance to my-bt admin site-maintenance" -- see scripts/my-bt). While ON, the running web app blocks every guest-facing
route -- /courses, /book/<shortname>, /cancel/<token>, /reinstate/<token>,
and every /my/* endpoint (login, signup, reset, confirm, cancel, reinstate,
settings, delete-account, ...) -- and shows a 503 maintenance page instead
(see app/webapp.py::App._maintenance_guard, called from each of those).

2026-07-10, the operator originally asked to gate only "any booking URL (like the
links on index.html)" (courses/book), leaving existing-booking management
(/my, /admin, /cancel/, /reinstate/, /host-cancel/, /host-reinstate/)
untouched -- but then, the same day, caught via a real external-IP test
that this left a real gap: "I was able to click on login and see the
normal login page from an external IP in maintenance mode. This should
not be!" The scope above is the corrected version: every GUEST-facing
route is now gated, uniformly, via one shared check instead of N separate
inlined copies.

/admin/*, /host-cancel/<id>, and /host-reinstate/<id> are the one
deliberate exception -- those are the HOST's own tools (the latter two are
unguessable-uuid4 "magic links" only ever emailed to admin_email), and
blocking the host's own ability to manage bookings during a maintenance
window they themselves declared would be counterproductive.

An IP bypass ([site].maintenance_bypass_hostname /
maintenance_bypass_ip_log, see app/webapp.py::_maintenance_bypass_allowed)
lets a recognized visitor (the operator's own dynamic-IP setup) use every gated
route completely normally regardless of maintenance state -- the ONE
exception is the static site/index.html) itself, which unavoidably shows
the banner to every visitor (see below), since it's a plain file nginx
serves with no per-visitor awareness at all.

State lives in a small JSON flag file in the data dir, NOT settings.toml:
settings.toml is read exactly once at process startup (see
app/cli_checks.py::check_settings_fresh's docstring), so a setting there
wouldn't take effect until a service restart -- the whole point of a
maintenance toggle is that it takes effect on the very next request.

This module also renders the identical message as a banner that
`my-bt admin site-maintenance on/off` inserts into / removes from the TOP of the
live, deployed index.html at [site].static_site_dir (2026-07-10, the operator:
"the my-bt should not modify the package installed TEMPLATE folder site"
-- this used to ALSO patch this checkout's own HOME/site/index.html, but
that copy is a template/reference only, never what nginx actually serves;
static_site_dir is the one real, live location, same as privacy.html/
terms.html already treat it). index.html is hand-authored and never
auto-copied otherwise (see README.md "Static-site pages"), so a dedicated,
idempotent insert/remove step is needed here specifically because
maintenance mode needs to show up immediately, not at the next manual
copy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .atomic_io import atomic_write_text, fsync_dir
from .templates import esc

_FLAG_FILENAME = "maintenance.json"

# HTML comment markers delimiting the managed banner block in index.html --
# lets `on`/`off` be run any number of times idempotently (replace-in-place
# if already present, insert fresh if not, remove cleanly either way)
# without ever touching anything else in a hand-authored file we don't
# otherwise own.
#
# 2026-07-14: the CLI command itself was renamed `admin maintenance` ->
# `admin site-maintenance`, but this literal string is deliberately LEFT
# AS-IS -- _BANNER_BLOCK_RE below matches an already-live banner against
# THIS exact constant, so if a maintenance window happened to be active
# on the real site at the moment this rename shipped, changing the text
# here would make the next `off` unable to find/remove that already-
# inserted (old-text) banner. It's just a human-readable HTML comment
# either way -- no functional loss in leaving the old command name in it.
_BANNER_START = "<!-- MAINTENANCE-BANNER:START (managed by `my-bt maintenance` -- do not hand-edit, your changes will be overwritten) -->"
_BANNER_END = "<!-- MAINTENANCE-BANNER:END -->"
# The leading `\n?` mirrors the trailing one: insert_banner() always adds
# exactly one newline before the banner (so it starts on its own line
# after <body ...>) -- without also eating that same newline back out
# here, repeated on/off (or on/on) cycles would each leave one more stray
# blank line behind, never quite returning to the original bytes.
_BANNER_BLOCK_RE = re.compile(
    r"\n?" + re.escape(_BANNER_START) + r".*?" + re.escape(_BANNER_END) + r"\n?",
    re.DOTALL,
)
_BODY_TAG_RE = re.compile(r"<body[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class MaintenanceState:
    enabled: bool
    message: str = ""
    set_at: str = ""  # ISO 8601 UTC timestamp, "" if never set/currently off


def flag_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _FLAG_FILENAME


def read_state(data_dir: str | Path) -> MaintenanceState:
    """Never raises -- a missing, corrupt, or unreadable flag file is
    treated the same as "off" (fail open on the side of the site working
    normally, not the side of an outage nobody can undo)."""
    p = flag_path(data_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MaintenanceState(enabled=False)
    return MaintenanceState(
        enabled=bool(raw.get("enabled", False)),
        message=raw.get("message", "") or "",
        set_at=raw.get("set_at", "") or "",
    )


def enable(data_dir: str | Path, message: str = "") -> MaintenanceState:
    state = MaintenanceState(enabled=True, message=message, set_at=datetime.now(timezone.utc).isoformat())
    p = flag_path(data_dir)
    # 2026-07-15: atomic_write_text, not a bare write_text() -- a torn
    # write here is actually harmless either way (read_state() fails
    # open to "off" on unreadable/corrupt JSON), but there's no reason
    # for this one to be the odd one out once every other write in the
    # project uses the crash-safe pattern. See app/atomic_io.py.
    # 2026-07-10: secure=True too -- this flag lives in the same shared
    # data_dir as users.csv, and `my-bt maintenance on/off` runs as root
    # same as every other my-bt invocation. Without this, a root-run
    # toggle would leave the flag root-owned and my-booking.service would
    # silently fail to even READ it (read_state() fails open to "off" on
    # any OSError) -- not a crash, but "maintenance on" quietly not taking
    # effect for real visitors is exactly the kind of silent breakage
    # secure_data_path exists to prevent. See app.atomic_io.secure_data_path.
    atomic_write_text(
        p, json.dumps({"enabled": True, "message": state.message, "set_at": state.set_at}),
        secure=True, mode=0o640,
    )
    return state


def disable(data_dir: str | Path) -> None:
    p = flag_path(data_dir)
    if p.exists():
        p.unlink()
        # 2026-07-15: fsync the directory after the unlink too, same
        # reasoning as atomic_write_text's own rename -- on Linux a
        # deletion isn't guaranteed durable across a hard crash until
        # the containing directory's inode is fsynced. Best-effort, same
        # as everywhere else fsync_dir is used.
        fsync_dir(p.parent)


def message_html(admin_email: str, custom_message: str = "") -> str:
    """The core maintenance message -- shared verbatim by both
    site/index.html's banner (banner_html below) and the app's own
    maintenance interstitial page (app/webapp.py::App._maintenance_response),
    so a visitor sees the exact same wording however they got here."""
    extra = f"<p>{esc(custom_message)}</p>" if custom_message else ""
    return (
        "<p><strong>This site is currently down for maintenance.</strong> "
        "Booking links won't work right now.</p>"
        f"{extra}"
        f'<p>Please reach out via email at <a href="mailto:{esc(admin_email)}">{esc(admin_email)}</a> '
        "-- or via Teams if you're a DBG Lux colleague.</p>"
    )


def banner_html(admin_email: str, custom_message: str = "") -> str:
    return (
        f"{_BANNER_START}\n"
        '<div style="background:#fff3cd;border-bottom:2px solid #f0ad4e;color:#7a5b00;'
        'padding:10px 20px;text-align:center;font-family:sans-serif;font-size:0.95em;">'
        f"{message_html(admin_email, custom_message)}"
        "</div>\n"
        f"{_BANNER_END}\n"
    )


def insert_banner(html: str, banner: str) -> str:
    """Idempotent: strips any existing banner block first, then inserts
    the given one right after the opening <body ...> tag ("right at the
    top" per the operator's request), or right at the very start of the document
    if there's no <body> tag at all (an unusual hand-authored page, but
    better to still show the message than silently do nothing)."""
    stripped = remove_banner(html)
    m = _BODY_TAG_RE.search(stripped)
    if m is None:
        return banner + stripped
    idx = m.end()
    return stripped[:idx] + "\n" + banner + stripped[idx:]


def remove_banner(html: str) -> str:
    return _BANNER_BLOCK_RE.sub("", html)


def apply_banner_to_file(path: Path, enabled: bool, admin_email: str, message: str = "") -> bool:
    """Inserts or removes the banner in the html file at `path`, in place.
    Returns True if the file was actually changed, False if it didn't
    exist or already matched the desired state (so callers -- see
    scripts/my-bt's cmd_maintenance -- can report "already up to date"
    instead of a misleading "done" on a no-op)."""
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8")
    updated = insert_banner(original, banner_html(admin_email, message)) if enabled else remove_banner(original)
    if updated == original:
        return False
    # 2026-07-15: atomic_write_text, not a bare write_text() -- this is
    # the actual LIVE homepage nginx serves; a torn write here (unlike
    # the flag file above) has no fail-open fallback, so a crash
    # mid-write would leave a genuinely broken page. See app/atomic_io.py.
    atomic_write_text(path, updated)
    return True
