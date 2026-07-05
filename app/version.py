"""Version info for `my-bt --version`. Two sources, tried in order:

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
from pathlib import Path

# Keep in sync with packaging/my-booking-tool.spec's `Version:` field --
# there's no single source of truth shared between a TOML/spec file and
# Python without adding a parsing dependency, so this is a manual (but
# rare -- only on an actual semver bump) sync point, same as the Release-
# vs-Version split documented in the spec itself.
PACKAGE_VERSION = "1.0.0"

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


def version_string(home: str) -> str:
    return f"my-booking-tool {PACKAGE_VERSION} (commit {git_commit(home)})"
