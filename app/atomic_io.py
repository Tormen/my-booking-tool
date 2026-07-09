"""Shared crash-safe file write helper.

2026-07-15, the operator, on hard-reboot data safety, after `app/storage.py`'s
CSV writes were audited and hardened: "yes please ALL writes linked to
my-booking-tool, my-bt and the site" -- not just the CSV storage layer.
This module is the one place that pattern lives, so every other module
that writes a file to disk that matters (config, secrets, marker/state
files, rendered static pages) uses the same crash-safe primitive instead
of a bare `Path.write_text()`, which can leave a torn/partial file behind
if the process (or the whole server) dies mid-write.

The pattern (same one `storage.py`'s `_LockedCsv._atomic_write` already
used for CSVs, extracted here so it isn't duplicated per module):
write the new content to a temp file in the SAME directory as the target
(so the final rename is on the same filesystem -- required for
os.replace() to be atomic at all), fsync() that temp file (new content
durable on disk), os.replace() it over the real path (atomic rename --
a reader, or a crash, only ever sees the complete old file or the
complete new one, never a torn one), then fsync() the containing
directory too (see fsync_dir's own docstring for why the rename itself
needs this).

Deliberately does NOT do CSV-specific things (row sanitization, csv.
DictWriter, chmod/chgrp via app.storage._secure_data_path) -- callers
that need those still go through storage.py's own _LockedCsv. This is
just the shared temp-file+fsync+rename+dir-fsync mechanics underneath
both.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("my_booking.atomic_io")


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Crash-safe replacement for `Path(path).write_text(text)`: on a
    crash mid-write, the target either still has its old, complete
    content or its new, complete content -- never a truncated/partial
    write. Creates the parent directory if it doesn't exist yet (matches
    every call site this replaces, which all did this themselves before).

    Callers that need to chmod/chown the result (e.g. a secret file)
    should still do that on `path` AFTER this returns -- os.replace()
    takes the temp file's own permissions (mkstemp's default: 0600,
    owner-only), so an existing file's more permissive mode is not
    preserved across a rewrite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        fsync_dir(path.parent)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def fsync_dir(dir_path: str | Path) -> bool:
    """fsync a directory's own inode, e.g. right after an os.replace()
    into it. fsyncing the temp file (above) only guarantees the new
    CONTENT is durable -- on Linux, the rename() itself isn't guaranteed
    durable until the containing directory's own inode is fsynced too.
    Without this, a hard power cut in the narrow window right after
    os.replace() returns could, on some filesystems/mount options, leave
    the rename uncommitted, so a reboot shows the file as it was before
    that write instead of after. Not corruption (the old file is never
    torn), just a possible lost last write in that window.

    Best-effort, same spirit as app.storage._secure_data_path: a
    directory fd can be opened read-only on every real POSIX filesystem
    this app targets, but this must never turn a successful write into a
    hard failure just because fsync-the-directory isn't supported on
    some unusual mount (e.g. certain network filesystems) -- every
    per-write call site here IGNORES the return value for exactly that
    reason. Returns True/False (fsync actually succeeded or not) so it
    can ALSO serve as the basis of a one-time startup capability probe --
    see probe_dir_fsync_support() below, which is the one place this
    return value is meant to be looked at."""
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError as exc:
        log.warning("could not open %s to fsync it after a write: %s", dir_path, exc)
        return False
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        log.warning("could not fsync directory %s after a write: %s", dir_path, exc)
        return False
    finally:
        os.close(dir_fd)
    return True


def probe_dir_fsync_support(dir_path: str | Path) -> bool:
    """One-time capability probe, meant to be called ONCE (at process
    startup, or from a `my-bt admin setup`/`admin health` check) rather
    than trusted to a routine per-write log line.

    2026-07-15, the operator, reviewing fsync_dir()'s best-effort/never-raises
    design: "that's the correct call for availability ... but it's also
    the kind of failure that's invisible until the one time it matters
    -- if the actual production mount silently doesn't support directory
    fsync, every write since deploy has been getting the weaker
    guarantee with nobody the wiser. Worth a one-time capability probe
    at startup ... log loudly ... rather than relying on someone
    noticing a warning line in a log nobody tails."

    Same underlying operation as fsync_dir() (open the directory
    read-only, fsync it, close it) -- the difference is entirely in what
    CALLERS do with a False result: fsync_dir()'s own per-write callers
    ignore it and move on (an unsupported mount must never turn a
    successful write into a crash); this function's callers are expected
    to react loudly -- see app.cli_checks.check_directory_fsync_support
    (surfaced through `my-bt admin setup`/`admin health`, with the operator's
    own standing "any warning -> exit 1" policy) and app.serve.main's
    startup check (logs at ERROR level and emails admin_email once per
    service start, not just a routine WARNING)."""
    return fsync_dir(dir_path)
