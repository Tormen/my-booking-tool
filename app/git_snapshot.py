"""Hourly automatic git snapshot of the CSV data directory
(`/var/lib/my-booking` -- users.csv/registrations.csv and data/archived/*).

This is a SEPARATE git repository, rooted at `data_dir/.git`, entirely
independent of this project's own git checkout -- it exists purely so an
operator has a cheap, local, commit-per-change safety net (accidental
`my-bt erase`, a bad manual CSV edit, a botched migration) on top of
whatever off-box backup they already run (see README.md "Known
simplifications" -- off-box backups are still the operator's own job).

Runs from a systemd timer (see systemd/my-booking-git-snapshot.timer),
same "cronjob-equivalent" pattern as app/retention.py -- see that module's
docstring. `git init` itself is NOT done here: that's a one-time setup
step offered by `my-bt setup -i` (see app/cli_setup.py), which also sets
local `git config user.email`/`user.name` and writes a `.gitignore`
(excluding `*.tmp` -- see app/storage.py::_LockedCsv._atomic_write for why
a stray `.tmp` file could theoretically exist after a hard crash) before
making the first commit via snapshot() below. This module only knows how
to add+commit into an ALREADY-initialized repo; if `.git` doesn't exist
yet it just reports that state rather than creating one itself, so a
fresh/uninitialized data dir never silently becomes a git repo just
because the hourly timer happened to fire.

**GDPR history caveat (documented in full in README.md "GDPR notes"):**
git commit history is immutable by default -- a snapshot committed before
a guest's erasure still contains their real name/email in that OLD
commit, forever, unless the history is manually pruned/rewritten. This
module deliberately does NOT do that automatically; see README.md for the
operator-decided tradeoff.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RunFunc = Callable[..., subprocess.CompletedProcess]


def _default_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


@dataclass(frozen=True)
class SnapshotResult:
    status: str  # "committed" | "no_changes" | "not_a_repo"
    detail: str


def snapshot(
    data_dir: str | Path,
    *,
    run: RunFunc = _default_run,
    now: datetime | None = None,
    message: str | None = None,
) -> SnapshotResult:
    """Stages everything under `data_dir` (`git add -A`) and commits only
    if something actually changed -- no empty commits. `data_dir` must
    already be a git repo (`.git` present); if not, this is reported as
    `"not_a_repo"` rather than auto-initializing one (see this module's
    docstring for why that's `my-bt setup -i`'s job instead).

    `-c user.email=... -c user.name=...` is passed on the commit
    invocation itself (belt-and-suspenders alongside the local repo config
    `my-bt setup -i` sets at init time -- see app/cli_setup.py) so this
    still works non-interactively even if the repo was initialized by
    hand without ever setting those, which would otherwise make `git
    commit` fail (no identity configured).

    `message`, if given, is used verbatim as the commit message instead of
    the auto-generated `"automatic snapshot: <timestamp>"` text -- for
    `my-bt admin git-snapshot -m "..."` right before a risky manual edit,
    so the rollback point in `git log` is actually named instead of just
    timestamped (git already records a real commit timestamp on its own,
    so there's no need to also fold one into a custom message)."""
    data_dir = Path(data_dir)
    if not (data_dir / ".git").exists():
        return SnapshotResult("not_a_repo", f"{data_dir} is not yet a git repo -- run `my-bt setup -i` to initialize it")

    run(["git", "add", "-A"], cwd=str(data_dir))

    # `git diff --cached --quiet` exits 0 if there's NO staged difference,
    # and 1 (nonzero) if there IS one -- the inverse of what "quiet" might
    # suggest at a glance, so get this the right way round: nonzero ==
    # something IS staged == worth committing.
    diff = run(["git", "diff", "--cached", "--quiet"], cwd=str(data_dir))
    if diff.returncode == 0:
        return SnapshotResult("no_changes", "nothing changed since the last snapshot -- no commit made")

    if message is not None:
        commit_message = message
    else:
        moment = now if now is not None else datetime.now(timezone.utc)
        commit_message = f"automatic snapshot: {moment.isoformat()}"
    run(
        [
            "git",
            "-c", "user.email=my-booking-tool <noreply@localhost>",
            "-c", "user.name=my-booking-tool",
            "commit", "-m", commit_message,
        ],
        cwd=str(data_dir),
    )
    return SnapshotResult("committed", commit_message)


def main() -> None:  # pragma: no cover - exercised via systemd, not tests
    import argparse
    import logging

    from .config import load_settings
    from .logutil import configure_logging

    parser = argparse.ArgumentParser(description="Commit any changes in the data directory to its own git repo")
    parser.add_argument("--settings", default="/etc/my-booking/settings.toml")
    parser.add_argument("--data-dir", default="/var/lib/my-booking")
    args = parser.parse_args()

    # Load settings first so a configured [logging].log_file is honored --
    # see app/retention.py's main() for the same reasoning.
    settings = load_settings(args.settings)
    configure_logging(settings.log_file)
    log = logging.getLogger("my_booking.git_snapshot")

    result = snapshot(args.data_dir)
    # WARNING, not INFO: same reasoning as retention.py -- runs once an
    # hour via the systemd timer, cheap, and this is the only confirmation
    # it actually ran and what it did, worth seeing in `journalctl -u
    # my-booking-git-snapshot.service` at the default (non-debug) log
    # level. One line regardless of outcome -- result.detail already says
    # which of committed/no_changes/not_a_repo happened.
    log.warning("git snapshot: %s", result.detail)


if __name__ == "__main__":  # pragma: no cover
    main()
