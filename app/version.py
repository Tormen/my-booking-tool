"""Version info for `my-bt --version`. Two sources, tried in order:

0. A `SOURCE_STAMP` file baked in at RPM-build time -- the newest
   modification time across the packaged SOURCE, NOT a build clock (see
   compute_source_stamp for why that distinction is the whole point).
1. A `GIT_COMMIT` file baked in at RPM-build time (scripts/build-rpm.sh
   runs `git rev-parse` in the checkout being packaged and writes the
   result here, falling back to "unknown" if that checkout isn't a git
   repo at all) -- this is what an actually-installed system uses, since
   the installed tree has no `.git` directory (deliberately excluded from
   the package, see build-rpm.sh).
2. A live `git rev-parse` against this checkout, for the case of running
   `my-bt` straight from a git clone (e.g. via MY_BOOKING_HOME pointed at
   a dev checkout) rather than an installed copy.

Falls back to a clear "unknown" string rather than raising either way --
`--version` should never be the one command that crashes.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Keep in sync with packaging/my-booking-tool.spec's `Version:` field --
# there's no single source of truth shared between a TOML/spec file and
# Python without adding a parsing dependency, so this is a manual (but
# rare -- only on an actual semver bump) sync point, same as the Release-
# vs-Version split documented in the spec itself.
PACKAGE_VERSION = "1.1.0"

_UNKNOWN = "unknown (not built via scripts/build-rpm.sh, and not a git checkout either)"


def _read_baked_commit(home: str) -> str | None:
    p = Path(home) / "GIT_COMMIT"
    if not p.exists():
        return None
    value = p.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def _live_git_commit(home: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", home, "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_commit(home: str) -> str:
    return _read_baked_commit(home) or _live_git_commit(home) or _UNKNOWN


# What counts as SOURCE for the stamp below. Deliberately a whitelist:
# the package also carries files the build itself regenerates every run
# (site/privacy.html, site/index_embedded.html, GIT_COMMIT, this stamp),
# and letting those in would make the stamp move on every rebuild, which
# is exactly what it must not do.
_SOURCE_PATHS = (
    "app", "scripts", "packaging", "systemd", "email_templates", "nginx", "tests",
    "site", "settings.toml.example", "README.md", "LICENSE",
)
# Under site/, these two are RE-RENDERED by every build (scripts/
# render-site.py, and `my-bt setup -i` for the embedded one), so their
# mtime is a build clock wearing a source file's name. Everything else in
# site/ -- index.html, the .example pages, privacy.html.tmpl,
# nginx-locations.conf -- is genuine source and must move the stamp.
#
# 2026-08-27: site/ was missing from the list entirely at first, so a CSS
# fix to index.html did not move the stamp at all. Excluding a whole
# directory to be rid of two files inside it was the wrong cut.
_GENERATED_NAMES = frozenset({"privacy.html", "index_embedded.html"})
_SOURCE_SUFFIXES = (".py", ".sh", ".spec", ".service", ".timer", ".txt", ".html",
                    ".toml", ".conf", ".md", ".example", "")
_STAMP_FILE = "SOURCE_STAMP"


def compute_source_stamp(root: str | Path) -> str | None:
    """The newest modification time across this checkout's SOURCE files,
    as "YYYY-MM-DD_HHMM" in UTC. None if nothing can be read.

    This is a version of the CODE, not a record of when someone ran
    rpmbuild: rebuilding untouched source twice must report the same
    string both times, and editing one line must move it. A build clock
    gets that backwards -- it moves when nothing changed, and says
    nothing about what changed.

    Generated artefacts are excluded by name (see _GENERATED_NAMES):
    site/privacy.html and site/index_embedded.html are re-rendered by
    every build, so counting them would make the stamp tick on its own."""
    root = Path(root)
    newest = 0.0
    for rel in _SOURCE_PATHS:
        path = root / rel
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = [p for p in path.rglob("*") if p.is_file()]
        else:
            continue
        for p in candidates:
            if "__pycache__" in p.parts or p.name == _STAMP_FILE:
                continue
            if p.name in _GENERATED_NAMES:
                continue
            if p.suffix not in _SOURCE_SUFFIXES:
                continue
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    if not newest:
        return None
    return datetime.fromtimestamp(newest, timezone.utc).strftime("%Y-%m-%d_%H%M")


def source_stamp(home: str) -> str | None:
    """The stamp for the RUNNING copy: the one baked in at package time
    if there is one, else computed live (running straight from a
    checkout). None when neither is possible."""
    p = Path(home) / _STAMP_FILE
    if p.exists():
        value = p.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return value
    return compute_source_stamp(home)


def version_string(home: str) -> str:
    """e.g. "my-booking-tool 1.1.0 (source 2026-08-27_0813 UTC, commit
    f052637-dirty)". The stamp dates the SOURCE, not the build -- see
    compute_source_stamp; on a `-dirty` build the commit alone cannot say
    which edit of it this is, and that is the gap the stamp fills."""
    stamp = source_stamp(home)
    dated = f"source {stamp} UTC, " if stamp else ""
    return f"my-booking-tool {PACKAGE_VERSION} ({dated}commit {git_commit(home)})"


# The running tree, for callers with no `home` of their own: app/ sits
# directly under it, the same way app/email_templates.py resolves its
# own default.
_HOME = str(Path(__file__).resolve().parent.parent)
_SHORT_CACHE: str | None = None


def short_version() -> str:
    """A compact "1.1.0 - 2026-08-27_0833 - f052637" for the footer of
    every app-rendered page.

    Exists so a screenshot identifies its own build: without it, a report
    like "this looks wrong" cannot be tied to a version, and the answer
    is usually "which build were you on?" -- a round-trip that this line
    removes. Short commit, not the full one: enough to identify, short
    enough not to shout.

    Computed once per process. The parts come from the same files
    `my-bt --version` reads, so the page and the CLI can never disagree.
    """
    global _SHORT_CACHE
    if _SHORT_CACHE is None:
        stamp = source_stamp(_HOME)
        commit = git_commit(_HOME)
        short = commit.split()[0][:7] if commit and not commit.startswith("unknown") else "?"
        _SHORT_CACHE = f"{PACKAGE_VERSION} - {stamp or '?'} - {short}"
    return _SHORT_CACHE
