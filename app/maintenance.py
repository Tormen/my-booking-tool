"""Sitewide maintenance mode: `my-bt maintenance on/off/status` (see
scripts/my-bt). While ON, the running web app refuses to start any NEW
booking (`/courses`, `/book/<shortname>` -- the two routes index.html
actually links to) and shows a maintenance message instead; existing-
booking management (`/my`, `/admin`, `/cancel/`, `/reinstate/`,
`/host-cancel/`, `/host-reinstate/`) is deliberately left untouched --
2026-07-10, the operator only asked to gate "any booking URL (like the links on
index.html)", and there's no reason to also stop someone from viewing or
canceling an EXISTING booking while new intake is paused.

State lives in a small JSON flag file in the data dir, NOT settings.toml:
settings.toml is read exactly once at process startup (see
app/cli_checks.py::check_settings_fresh's docstring), so a setting there
wouldn't take effect until a service restart -- the whole point of a
maintenance toggle is that it takes effect on the very next request.

This module also renders the identical message as a banner that
`my-bt maintenance on/off` inserts into / removes from the TOP of
site/index.html (both this checkout's own copy and, if
[site].static_site_dir is configured, the live deployed copy) -- unlike
privacy.html, index.html is hand-authored and never auto-copied
otherwise (see README.md "Static-site pages"), so a dedicated, idempotent
insert/remove step is needed here specifically because maintenance mode
needs to show up immediately, not at the next manual copy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .templates import esc

_FLAG_FILENAME = "maintenance.json"

# HTML comment markers delimiting the managed banner block in index.html --
# lets `on`/`off` be run any number of times idempotently (replace-in-place
# if already present, insert fresh if not, remove cleanly either way)
# without ever touching anything else in a hand-authored file we don't
# otherwise own.
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
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"enabled": True, "message": state.message, "set_at": state.set_at}),
        encoding="utf-8",
    )
    return state


def disable(data_dir: str | Path) -> None:
    p = flag_path(data_dir)
    if p.exists():
        p.unlink()


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
    path.write_text(updated, encoding="utf-8")
    return True
