"""Shared crash-safe file write helper.

2026-07-15: after `app/storage.py`'s CSV writes were audited and
hardened for hard-reboot data safety, the same protection was extended
to every write linked to my-booking-tool/my-bt/the site, not just the
CSV storage layer. This module is the one place that pattern lives, so
every other module
that writes a file to disk that matters (config, secrets, marker/state
files, rendered static pages) uses the same crash-safe primitive instead
of a bare `Path.write_text()`, which can leave a torn/partial file behind
if the process (or the whole server) dies mid-write.

The pattern (same one `storage.py`'s `_LockedCsv._atomic_write` already
used for CSVs, extracted here so it isn't duplicated per module):
write the new content to a temp file in the SAME directory as the target
(so the final rename is on the same filesystem -- required for
os.replace() to be atomic at all), fsync() that temp file (new content
durable on disk), optionally secure it (see secure_data_path below),
os.replace() it over the real path (atomic rename -- a reader, or a
crash, only ever sees the complete old file or the complete new one,
never a torn one), then fsync() the containing directory too (see
fsync_dir's own docstring for why the rename itself needs this).

2026-07-10, second real production incident: `secure_data_path`
(chmod/chgrp/root-only-chown) used to live in app/storage.py, CSV-only --
see its own docstring for the two production PermissionError incidents
that shaped it. Any OTHER file under the same shared
data directory (calendar_sync.py's resync markers, maintenance.py's
maintenance-flag) is written through THIS module's atomic_write_text
instead of _LockedCsv, and is exposed to the exact same root-run-my-bt-
breaks-the-live-service's-own-access problem -- there's nothing
CSV-specific about that failure mode. Moved here, and exposed via
atomic_write_text's own `secure=`/`mode=` params, so both write paths
share one securing implementation instead of two copies of the same
root-vs-non-root chown/chgrp logic drifting apart. app/storage.py now
imports this rather than defining its own copy.
"""
from __future__ import annotations

import grp
import logging
import os
import stat
import pwd
import tempfile
from pathlib import Path

log = logging.getLogger("my_booking.atomic_io")

# Every my-booking systemd unit (my-booking.service, -watchdog, -retention,
# -git-snapshot) runs as this same dedicated user/group -- see systemd/*.service.
SERVICE_GROUP = "my-booking"
SERVICE_USER = "my-booking"


def secure_data_path(path, mode: int = 0o640) -> None:
    """Best-effort: chmod (default 0640 -- owner rw, GROUP read -- not
    owner-only 0600), chgrp to SERVICE_GROUP, and (root only -- see below)
    chown to SERVICE_USER. Applied to every shared data file/directory on
    every write/creation, so permissions self-heal on the next write
    regardless of who performed a previous one.

    Deliberately never raises. Failure modes are logged at two different
    levels, on purpose:
      - SERVICE_GROUP/SERVICE_USER simply don't exist on this machine
        (KeyError from getgrnam/getpwnam) -- entirely normal for a dev
        checkout or this repo's own test suite, not something an operator
        needs to see on every single write, so this logs at DEBUG only.
      - chmod/chown/chgrp fails outright for any OTHER reason (e.g. a real
        deployment where the group exists but the calling process isn't a
        member of it and isn't root) -- an actually actionable problem
        worth surfacing, so this logs at WARNING.
    Either way, the write this is securing must never fail just because
    the permissions touch-up couldn't fully complete.

    2026-07-09, real production incident #1 (running `my-bt cancel`
    directly as root left registrations.csv root:root mode 0600 --
    unreadable by my-booking-watchdog.service, a READ-only consumer):
    chmod-to-0640 + chgrp-to-SERVICE_GROUP fixed that one.

    2026-07-10, real production incident #2: chgrp alone was NOT enough
    for a read-WRITE consumer. `my-bt admin gdpr erase` (root, same as
    every my-bt invocation on this box) writes through this exact
    function -- os.replace() into place makes the NEW file's OWNER
    whoever performed that write, root in this case, regardless of the
    chgrp below. At mode 0640 (owner rw, GROUP READ-ONLY), that leaves
    my-booking.service -- a group MEMBER, never the owner once root has
    written -- able to read but never write its own users.csv again:
    "PermissionError: [Errno 13] ... users.csv" the next time the service
    tried to set a confirm/reset token.

    Fix: a non-root process still only ever chgrp's (POSIX lets an owner
    chgrp to any group they're a member of without root; forcing
    ownership itself would need root universally and isn't necessary
    there). But when the CURRENT process IS root -- the one case that can
    reliably fix this, and also the one case that causes it -- also chown
    the OWNER back to SERVICE_USER, restoring exactly the access the
    service had before that root-run write touched the file."""
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.warning("could not chmod %s to %o (%s)", path, mode, exc)
    if os.geteuid() == 0:
        try:
            uid = pwd.getpwnam(SERVICE_USER).pw_uid
        except KeyError:
            log.debug("service user %r does not exist on this machine -- skipping chown of %s", SERVICE_USER, path)
        else:
            try:
                os.chown(path, uid, -1)
            except OSError as exc:
                log.warning("could not chown %s to %r (%s)", path, SERVICE_USER, exc)
    try:
        gid = grp.getgrnam(SERVICE_GROUP).gr_gid
    except KeyError:
        log.debug("service group %r does not exist on this machine -- skipping chgrp of %s", SERVICE_GROUP, path)
        return
    try:
        os.chown(path, -1, gid)
    except OSError as exc:
        log.warning("could not chgrp %s to %r (%s)", path, SERVICE_GROUP, exc)


# Anything a WEB SERVER has to read. tempfile.mkstemp creates its file
# 0600 and atomic_write_text renames that file into place, so without
# an explicit mode a page written here is unreadable by nginx and the
# visitor gets 403 -- which is exactly what happened to a freshly
# deployed index_embedded.html on 2026-08-27.
PUBLIC_FILE_MODE = 0o644


def _inherit_identity(existing, tmp_path: str) -> None:
    """A replaced file keeps its owner and mode.

    os.replace() puts a BRAND NEW inode in place, carrying the identity
    of whoever wrote it -- so a root-run tool rewriting a file owned by a
    service user silently takes it away from that service. That is
    exactly what happened (2026-08-28): `my-bt admin setup` commented out
    some [[course]] blocks as root, settings.toml came out root:root
    0640, and the my-booking service could no longer read its own config
    -- it crash-looped, with a permission error naming a file that had
    been perfectly readable a second earlier.

    Only meaningful when replacing something that already exists; a new
    file has no identity to keep. chown needs privilege, so it is
    attempted and allowed to fail: a non-root writer cannot have taken
    the file from anyone in the first place."""
    if existing is None:
        return
    try:
        os.chmod(tmp_path, stat.S_IMODE(existing.st_mode))
    except OSError:
        pass
    if existing.st_uid != os.geteuid() or existing.st_gid != os.getegid():
        try:
            os.chown(tmp_path, existing.st_uid, existing.st_gid)
        except (OSError, AttributeError):
            pass


def atomic_write_text(
    path: str | Path, text: str, encoding: str = "utf-8", *, secure: bool = False,
    mode: int = 0o640, public: bool = False,
) -> None:
    """Crash-safe replacement for `Path(path).write_text(text)`: on a
    crash mid-write, the target either still has its old, complete
    content or its new, complete content -- never a truncated/partial
    write. Creates the parent directory if it doesn't exist yet (matches
    every call site this replaces, which all did this themselves before).

    `secure=True` applies secure_data_path(mode=mode) to the temp file
    BEFORE the rename (same order _LockedCsv._atomic_write always used --
    the permissions/ownership need to be right at the instant the new
    file becomes visible at `path`, not as a separate step after). Pass
    this for anything living in the shared data directory alongside the
    CSVs (see secure_data_path's own docstring for why); leave it False
    (the default) for anything else -- e.g. secrets, or files nginx/other
    services own -- where this app's my-booking group model doesn't
    apply. Callers that need some OTHER chmod/chown scheme entirely
    should still do that on `path` themselves AFTER this returns.

    `public=True` writes PUBLIC_FILE_MODE (0644) instead -- for the pages
    that get deployed into a web root, where the reader is nginx, not
    this app. It is applied to the temp file BEFORE the rename for the
    same reason `secure` is: the mode has to be right at the instant the
    file becomes visible, not a moment later. Mutually exclusive with
    `secure`, which is about the my-booking group model and would leave a
    web page unreadable by the web server."""
    if secure and public:
        raise ValueError("atomic_write_text: secure and public are mutually exclusive")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Whose file is this ALREADY? Read before writing, because the temp
    # file replaces it and inherits nothing by itself.
    try:
        existing = path.stat()
    except OSError:
        existing = None
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        if secure:
            secure_data_path(tmp_path, mode=mode)
        elif public:
            os.chmod(tmp_path, PUBLIC_FILE_MODE)
        else:
            _inherit_identity(existing, tmp_path)
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

    2026-07-15: fsync_dir()'s best-effort/never-raises design is correct
    for availability, but it's also the kind of failure that's invisible
    until the one time it matters -- if the actual production mount
    silently doesn't support directory fsync, every write since deploy
    has been getting the weaker guarantee with nobody the wiser. Worth a
    one-time capability probe at startup that logs loudly, rather than
    relying on someone noticing a warning line in a log nobody tails.

    Same underlying operation as fsync_dir() (open the directory
    read-only, fsync it, close it) -- the difference is entirely in what
    CALLERS do with a False result: fsync_dir()'s own per-write callers
    ignore it and move on (an unsupported mount must never turn a
    successful write into a crash); this function's callers are expected
    to react loudly -- see app.cli_checks.check_directory_fsync_support
    (surfaced through `my-bt admin setup`/`admin health`, following this
    project's standing "any warning -> exit 1" policy) and app.serve.main's
    startup check (logs at ERROR level and emails admin_email once per
    service start, not just a routine WARNING)."""
    return fsync_dir(dir_path)
