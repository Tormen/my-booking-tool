"""CSV-backed storage for users and registrations.

Design choices:
- Whole-file lock (fcntl.flock) around read-modify-write cycles: this app is
  small/low-traffic, so simplicity beats row-level locking.
- Atomic write: write to a temp file in the same directory, then os.replace()
  -- never leaves a torn/partial CSV on crash or concurrent read.
- CSV injection guard applied to every field on write (see security.py).
"""
from __future__ import annotations

import csv
import fcntl
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, asdict, fields
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Iterable

from .security import sanitize_csv_field

log = logging.getLogger("my_booking.storage")

USER_FIELDS = [
    "user_id", "email", "name", "password_hash", "password_salt",
    "confirm_token_hash", "confirm_token_created_at", "prev_confirm_token_hash",
    "created_at", "last_login_at",
    "pending_email", "pending_email_token_hash", "pending_email_token_created_at",
    "prev_pending_email_token_hash", "pending_email_cancel_token_hash",
]
REG_FIELDS = [
    "registration_id", "course_shortname", "occurrence_date", "user_id", "status",
    "registered_at", "guest_cancel_token_hash", "canceled_at", "canceled_by", "host_message",
    "party_id", "invited_by_user_id",
]

STATUS_CONFIRMED = "confirmed"
STATUS_WAITLISTED = "waitlisted"
STATUS_CANCELED_BY_GUEST = "canceled_by_guest"
STATUS_CANCELED_BY_HOST = "canceled_by_host"
# A booking made under an email that hasn't confirmed account ownership yet
# (see app/webapp.py::book) -- deliberately excluded from every
# capacity/waitlist/calendar-sync code path below (none of them match this
# status), so an unconfirmed signup can never hold a real spot or occupy the
# calendar. Promoted to CONFIRMED/WAITLISTED (re-checking capacity fresh at
# that moment) only once the guest clicks the emailed confirmation link --
# see Store.confirm_pending_registration.
STATUS_PENDING_CONFIRMATION = "pending_confirmation"

# 2026-07-08, the operator (screenshot of /admin?past=1's Status column reading
# raw "confirmed"/"canceled_by_guest" etc.): "I prefer Host and Guest and
# then also 'Confirmed' for the status" -- same round as the Guests
# column's own Host/Guest capitalization (see app/cli_list.py's
# annotate_admin_party_label). Display-only: the underlying STATUS_*
# values above are never touched. Falls back to a generic "Title Case,
# underscores->spaces" humanization for anything not listed (there is
# currently no such status, but this keeps a future one from rendering as
# a raw "some_new_status" instead of failing loudly).
#
# 2026-07-13: moved here from app/webapp.py (as _STATUS_LABELS/
# _status_label) so app/cli_list.py -- and therefore `my-bt list`'s own
# default clean view -- can show the IDENTICAL label webapp.py's
# admin_overview()/my() already do, rather than a second copy that could
# drift. webapp.py now imports status_label from here instead of defining
# its own.
STATUS_LABELS = {
    STATUS_CONFIRMED: "Confirmed",
    STATUS_WAITLISTED: "Waitlisted",
    STATUS_PENDING_CONFIRMATION: "Pending confirmation",
    STATUS_CANCELED_BY_GUEST: "Canceled by guest",
    STATUS_CANCELED_BY_HOST: "Canceled by host",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_display_timestamp(iso_str: str) -> str:
    """Renders a now_iso()-produced (or any ISO-8601) timestamp for a human
    to read, e.g. in the operator's own CalDAV event description
    (calendar_sync.py's Participants table) or /admin's overview table.
    2026-07-07, the operator (screenshot of that Participants table showing
    "registered 2026-07-07T00:47:57+00:00"): "please use for TIMESTAMPS
    wherever you currently have this format ... YYYY-MM-DD_HHMM.SS".

    Deliberately display-only: the underlying CSV/storage value stays the
    real ISO-8601 string untouched (registered_at/canceled_at as written by
    now_iso() above) -- retention.py, watchdog.py, and migrate_simplymeet.py
    all still parse that with datetime.fromisoformat() same as before. Falls
    back to the raw string unchanged if it isn't valid ISO-8601 (e.g. an
    empty canceled_at on a still-active registration), same "don't blow up
    on a blank/legacy value" spirit as the rest of this module.

    2026-07-08, the operator (screenshot of /admin?past=1's Registered column
    showing "2025-10-18_0000.00" for SimplyMeet.me-imported rows): "if we
    have no time, then please display just the date" -- app/migrate_
    simplymeet.py's own docstring documents exactly why these are all
    midnight: the export has no real "booking created at" timestamp, so
    `registered_at` is set to a placeholder ("<occurrence_date>T00:00:00",
    no real time-of-day) for every imported row. A real now_iso()-stamped
    registration is for all practical purposes never exactly midnight, so
    exact 00:00:00 is treated as "no real time recorded" and rendered as
    just the date, rather than a misleadingly precise-looking "_0000.00"."""
    if not iso_str:
        return iso_str
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.time() == time(0, 0, 0):
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d_%H%M.%S")


@dataclass
class User:
    user_id: str
    email: str
    name: str
    # Empty password_hash/salt means this account has never been confirmed
    # (no password set yet) -- there is no separate boolean flag, since
    # "has a password" and "confirmed their email" happen in the exact same
    # step (see app/webapp.py's /my/confirm/<token> handler).
    password_hash: str
    password_salt: str
    # Hash of a pending confirm-or-reset token (see security.hash_token),
    # blank when none is outstanding. Reused for BOTH the very first
    # account confirmation and a later "forgot password" reset -- both
    # reduce to "prove you own this inbox via a one-time link, then set a
    # password" (see app/webapp.py's unified /my/reset).
    confirm_token_hash: str = ""
    confirm_token_created_at: str = ""
    # The hash that confirm_token_hash held just before its last overwrite
    # (2026-07-07, the operator: "a new email should invalidate the pending link
    # ... and clicking the invalidated link should inform the user that
    # there should be a NEW link coming to him"). set_confirm_token() always
    # ALREADY invalidated the old link (find_user_by_confirm_token_hash
    # simply stops matching it), but the guest just saw "invalid or already
    # used" with no clue a fresher one is on its way. Keeping just the ONE
    # immediately-preceding hash (not a full history) lets my_confirm() show
    # that specific, friendlier message for the common case -- clicking an
    # email 2+ generations stale still falls back to the generic message,
    # a deliberate simplicity/CSV-row-growth tradeoff, not an oversight.
    prev_confirm_token_hash: str = ""
    created_at: str = ""
    last_login_at: str = ""
    # Email-change (2026-07-10, /my/settings): a REQUESTED-but-not-yet-
    # confirmed new login email, plus its own token pair -- deliberately
    # separate fields from confirm_token_hash/prev_confirm_token_hash
    # above (which are about proving ownership of the EXISTING email to
    # set/reset a password), not a reuse of them, since a guest could in
    # principle have both a password-reset link AND an email-change link
    # outstanding at once without either interfering with the other.
    # Same "shift the old hash into a prev_ field first" pattern as
    # prev_confirm_token_hash (see set_pending_email) so my_confirm_email()
    # can show the same friendlier "a newer link was already sent"
    # message. Only ONE pending_email can ever be outstanding at a time
    # ("one active + one pending max") -- a second request overwrites the
    # first outright rather than queuing.
    pending_email: str = ""
    pending_email_token_hash: str = ""
    pending_email_token_created_at: str = ""
    prev_pending_email_token_hash: str = ""
    # A SEPARATE token from pending_email_token_hash above (2026-07-11,
    # the operator: "Please provide a link without login" -- the "notify the old
    # address" email's cancel link pointed at /my/settings, login
    # required). Deliberately its own field, not a reuse of the confirm
    # token: the confirm token is only ever emailed to the NEW address and
    # is good for COMPLETING the change, so handing that same secret to
    # the OLD address's "cancel this" link would let whoever holds it
    # complete the change instead of cancel it -- the exact opposite of
    # what a cancel link is for. This one can only ever abort the pending
    # change (see clear_pending_email/find_user_by_pending_email_cancel_token_hash),
    # never confirm it.
    pending_email_cancel_token_hash: str = ""


@dataclass
class Registration:
    registration_id: str
    course_shortname: str
    occurrence_date: str  # ISO date, e.g. "2026-07-08"
    user_id: str
    status: str
    registered_at: str
    guest_cancel_token_hash: str
    canceled_at: str = ""
    canceled_by: str = ""
    host_message: str = ""
    # Guest-booking (2026-07): party_id is shared by every row created in
    # one booking submission (the person who filled out the form, plus any
    # "+ Add participant" guests they added) -- see
    # Store.add_party_registrations_checking_capacity. Blank for any
    # registration made without guests (including everything booked before
    # this feature existed) -- treated as its own solo party of one
    # wherever party membership matters (see promote_next_waitlisted).
    # invited_by_user_id is blank for the person who actually filled out
    # the form ("the leader") and set to the leader's user_id for every
    # guest they added -- this is the only place "who booked together, and
    # who was the guest" is recorded. Cancellation is always per-row/
    # per-person regardless of party membership -- a guest (or the leader)
    # can cancel their own spot without affecting anyone else's; only the
    # ORIGINAL admission decision (confirmed vs. waitlisted) is atomic
    # across the party.
    party_id: str = ""
    invited_by_user_id: str = ""


def _git_commit_data_file(path: Path, message: str) -> None:
    """Best-effort git commit of ONE CSV file (users.csv/registrations.csv/
    an archived/*.csv), right after _LockedCsv writes it -- a per-write
    companion to app/git_snapshot.py's existing HOURLY, whole-directory
    snapshot (see that module's docstring and README.md "GDPR notes" ->
    "Data dir git snapshot"). 2026-07-07, the operator: "after any change to any
    of the CSV files: CUD ... please directly do a git commit ... Commit
    message should state what changed without revealing personal data ...
    as a safety net in case of ANY bugs" -- the hourly snapshot alone could
    leave up to an hour of changes unrecovered if something went wrong
    right after; this closes that gap with an immediate, per-operation
    commit, while the hourly one keeps covering what this can't (anything
    changed OUTSIDE the app, e.g. a manual CSV edit -- see
    git_snapshot.py's own docstring).

    Deliberately does NOT `git init` -- same reasoning as
    app/git_snapshot.py's snapshot(): a fresh/uninitialized data dir must
    never silently become a git repo just because a booking happened to
    come in; that's `my-bt setup -i`'s job alone (see app/cli_setup.py's
    "Data dir git snapshot" step), one single place that owns "how/when
    this repo gets created". If `.git` isn't there yet, this is a silent
    no-op -- not a failure, just "not opted in yet".

    ONE repo covers the whole data directory (e.g. /var/lib/my-bookings):
    users.csv/registrations.csv sit directly in it, and the two
    archived/*.csv files (see Store.erase_user) sit one level below in
    archived/ -- so this walks UP from `path` looking for that repo root
    rather than assuming `path.parent` is always it.

    The `-c user.email=...`/`-c user.name=...` passed inline on the commit
    itself matches app/git_snapshot.py's own commit call exactly (same
    identity string) -- belt-and-suspenders alongside whatever local repo
    config `my-bt setup -i` already set, so this still works even if the
    repo was somehow initialized by hand without ever setting those.

    Every Store method that mutates a CSV passes its own short, generic
    description (e.g. "cancel registration", "set password") as the
    message -- never an email, name, or other guest-supplied value, per
    the operator's own "without revealing personal data" instruction.

    Deliberately swallows every failure (git not installed, repo somehow
    unwritable, nothing staged because the rewrite was byte-identical,
    etc.) -- this is an audit trail on top of the real write that already
    succeeded via os.replace() above, not a required step; a booking or
    cancellation must never fail just because this best-effort commit
    couldn't complete. Genuine failures (git IS set up but errors out
    anyway) are logged (WARNING) so they're still visible in `journalctl
    -u my-booking.service`, same as any other unexpected condition in this
    app -- but "no repo yet" is expected/normal and logs nothing."""
    repo_dir = None
    for ancestor in (path.parent, *path.parent.parents):
        if (ancestor / ".git").is_dir():
            repo_dir = ancestor
            break
    if repo_dir is None:
        return  # not opted in yet (see my-bt setup -i) -- silent no-op, not a failure
    rel_path = str(path.relative_to(repo_dir))
    try:
        subprocess.run(["git", "add", "--", rel_path], cwd=repo_dir, check=True, capture_output=True)
        # Nothing actually changed (e.g. a write() that rewrote
        # byte-identical content) -- skip rather than let `git commit`
        # fail loudly with "nothing to commit".
        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", rel_path],
            cwd=repo_dir, capture_output=True,
        )
        if staged_diff.returncode == 0:
            return
        subprocess.run(
            [
                "git",
                "-c", "user.email=my-booking-tool <noreply@localhost>",
                "-c", "user.name=my-booking-tool",
                "commit", "-q", "-m", message, "--", rel_path,
            ],
            cwd=repo_dir, check=True, capture_output=True,
        )
    except Exception:
        log.warning("git auto-commit failed for %s", rel_path, exc_info=True)


class _LockedCsv:
    """Context manager: opens `path` for locked read-modify-write, creating it
    with a header if missing. Yields (rows: list[dict], write(rows)) where
    write() only takes effect if called before the `with` block exits.

    Pass `readonly=True` for call sites that never call write(): this opens
    the file "r" instead of "r+" (so it works even when the file/mount is
    genuinely read-only -- e.g. under systemd's ReadOnlyPaths=, as the
    watchdog unit uses) and takes a SHARED flock (LOCK_SH) instead of an
    exclusive one, so concurrent reads don't block each other while still
    being consistent with (blocking on, and blocked by) a concurrent
    read-modify-write cycle. Calling the yielded write() in readonly mode
    is a programming error and raises."""

    def __init__(self, path: Path, fieldnames: list[str], readonly: bool = False):
        self.path = path
        self.fieldnames = fieldnames
        self.readonly = readonly
        self._fh = None
        self._to_write: list[dict] | None = None
        self._commit_message: str | None = None

    def __enter__(self):
        if self.readonly:
            if not self.path.exists():
                return [], self._set_rows_to_write
            self._fh = open(self.path, "r", newline="", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_SH)
            reader = csv.DictReader(self._fh)
            rows = list(reader)
            return rows, self._set_rows_to_write

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
            os.chmod(self.path, 0o600)
        self._fh = open(self.path, "r+", newline="", encoding="utf-8")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        self._fh.seek(0)
        reader = csv.DictReader(self._fh)
        rows = list(reader)
        return rows, self._set_rows_to_write

    def _set_rows_to_write(self, rows: list[dict], message: str | None = None) -> None:
        if self.readonly:
            raise RuntimeError("_LockedCsv(readonly=True) must never call write()")
        self._to_write = rows
        self._commit_message = message

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self._to_write is not None:
                self._atomic_write(self._to_write)
                # 2026-07-07, the operator: "after any change to any of the CSV
                # files: CUD ... please directly do a git commit ...
                # Commit message should state what changed without
                # revealing personal data ... as a safety net in case of
                # ANY bugs" -- every successful write, from every Store
                # method, gets committed here in ONE place rather than
                # relying on each call site to remember to. Best-effort:
                # see _git_commit_data_file's own docstring for why a git
                # failure here must never surface as an app-breaking error.
                _git_commit_data_file(self.path, self._commit_message or f"update {self.path.name}")
        finally:
            if self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        return False

    def _atomic_write(self, rows: list[dict]) -> None:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as tmp:
                writer = csv.DictWriter(tmp, fieldnames=self.fieldnames)
                writer.writeheader()
                for row in rows:
                    clean = {k: sanitize_csv_field(str(row.get(k, ""))) for k in self.fieldnames}
                    writer.writerow(clean)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


def _read_csv_plain(path: Path, fieldnames: list[str]) -> list[dict]:
    """Read-only, unlocked (reporting/CLI use) -- creates nothing, returns []
    if the file doesn't exist yet."""
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Store:
    def __init__(self, data_dir: str | Path, archive_dir: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self.users_path = self.data_dir / "users.csv"
        self.registrations_path = self.data_dir / "registrations.csv"
        self.archive_dir = Path(archive_dir) if archive_dir else self.data_dir / "archived"
        self.archived_users_path = self.archive_dir / "users.csv"
        self.archived_registrations_path = self.archive_dir / "registrations.csv"

    # -- users ---------------------------------------------------------------

    def find_user_by_email(self, email: str) -> User | None:
        email = email.strip().lower()
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row["email"].strip().lower() == email:
                    return User(**row)
        return None

    def find_user_by_id(self, user_id: str) -> User | None:
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row["user_id"] == user_id:
                    return User(**row)
        return None

    def upsert_user_for_booking(self, email: str, name: str) -> User:
        """Called from the booking form, which no longer collects a
        password at all -- this only ever touches `name`. An existing
        user's password_hash/salt (confirmed or still empty/unconfirmed)
        is left completely alone; a brand-new email gets a fresh row with
        both blank (unconfirmed) until they go through /my/confirm/<token>.
        This is also what closes the old account-hijack hole: nothing
        reachable from the booking form can ever change another email's
        password."""
        email_norm = email.strip().lower()
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["email"].strip().lower() == email_norm:
                    row["name"] = name
                    write(rows, "update user name")
                    return User(**row)
            user = User(
                user_id=str(uuid.uuid4()),
                email=email_norm,
                name=name,
                password_hash="",
                password_salt="",
                created_at=now_iso(),
            )
            rows.append(asdict(user))
            write(rows, "create user")
            return user

    def set_confirm_token(self, user_id: str, token_hash: str, created_at: str) -> None:
        """Stores a pending confirm-or-reset token for this user -- see
        User.confirm_token_hash's docstring for why the same field covers
        both the first-ever confirmation and a later password reset.

        Shifts whatever was previously in confirm_token_hash into
        prev_confirm_token_hash first (2026-07-07) -- see that field's own
        docstring -- so my_confirm() can later tell "this exact link was
        just superseded by a newer one" apart from "this link never
        existed"/"already used", even though both already stop matching
        confirm_token_hash the instant this runs."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["prev_confirm_token_hash"] = row["confirm_token_hash"]
                    row["confirm_token_hash"] = token_hash
                    row["confirm_token_created_at"] = created_at
                    write(rows, "set confirm/reset token")
                    return

    def find_user_by_confirm_token_hash(self, token_hash: str) -> User | None:
        if not token_hash:
            return None  # never match on a blank hash (no user has "" stored as a real token)
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row["confirm_token_hash"] and row["confirm_token_hash"] == token_hash:
                    return User(**row)
        return None

    def find_user_by_prev_confirm_token_hash(self, token_hash: str) -> User | None:
        """2026-07-07 -- see User.prev_confirm_token_hash. Used only to
        render a friendlier "a newer link was already sent" message; never
        treated as proof of identity/ownership the way the CURRENT hash is.
        Reads via .get() (not row[...]) since an existing deployment's
        users.csv predates this column -- its on-disk header won't gain
        prev_confirm_token_hash until the next write() to that file rewrites
        it with the full USER_FIELDS header (same lazy-migration pattern
        every earlier column addition here has relied on)."""
        if not token_hash:
            return None
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row.get("prev_confirm_token_hash") == token_hash:
                    return User(**row)
        return None

    def set_password(self, user_id: str, password_hash: str, password_salt: str) -> None:
        """Sets the account's real login password and consumes (clears) any
        pending confirm/reset token -- a used link can't be replayed. Clears
        prev_confirm_token_hash too, so an older superseded link can't keep
        showing "a newer one is coming" after the account is already fully
        set up and confirmed."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["password_hash"] = password_hash
                    row["password_salt"] = password_salt
                    row["confirm_token_hash"] = ""
                    row["confirm_token_created_at"] = ""
                    row["prev_confirm_token_hash"] = ""
                    write(rows, "set password")
                    return

    def touch_login(self, user_id: str) -> None:
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            changed = False
            for row in rows:
                if row["user_id"] == user_id:
                    row["last_login_at"] = now_iso()
                    changed = True
            if changed:
                write(rows, "record login")

    def set_name(self, user_id: str, name: str) -> None:
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["name"] = name
                    write(rows, "update user name")
                    return

    def set_pending_email(
        self, user_id: str, pending_email: str, token_hash: str, created_at: str,
        cancel_token_hash: str = "",
    ) -> None:
        """Requests an email change -- see User.pending_email. Shifts any
        previously-outstanding pending-email token into
        prev_pending_email_token_hash first (same "tell them a newer link
        superseded this one" pattern as set_confirm_token /
        prev_confirm_token_hash), and OVERWRITES any earlier pending_email
        outright: only one pending change can ever be outstanding at a
        time ("one active + one pending max"), so requesting a second
        change -- even to a different address -- simply replaces the
        first rather than queuing alongside it.

        `cancel_token_hash` (2026-07-11) is the hash of a SEPARATE,
        freshly-minted token -- see User.pending_email_cancel_token_hash's
        own docstring for why this can't just reuse `token_hash` -- that
        the OLD address's own notification email uses for its no-login
        "cancel this" link. Blank (the default) leaves it untouched,
        purely so existing callers/tests that don't care about the cancel
        link don't have to pass one."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["prev_pending_email_token_hash"] = row.get("pending_email_token_hash", "")
                    row["pending_email"] = pending_email
                    row["pending_email_token_hash"] = token_hash
                    row["pending_email_token_created_at"] = created_at
                    if cancel_token_hash:
                        row["pending_email_cancel_token_hash"] = cancel_token_hash
                    write(rows, "request email change")
                    return

    def clear_pending_email(self, user_id: str) -> None:
        """Aborts a pending email change (guest-initiated cancel from
        /my/settings, or from the no-login /my/cancel-email-change/<token>
        link -- see app.webapp.App.my_cancel_email_change). Deliberately
        does NOT touch prev_pending_email_token_hash -- an aborted
        change's token should read as a plain "invalid/already used" link
        if somehow clicked afterward, not the friendlier "superseded by a
        newer one" message, since nothing newer was ever sent."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["pending_email"] = ""
                    row["pending_email_token_hash"] = ""
                    row["pending_email_token_created_at"] = ""
                    row["pending_email_cancel_token_hash"] = ""
                    write(rows, "cancel pending email change")
                    return

    def find_user_by_pending_email_token_hash(self, token_hash: str) -> User | None:
        if not token_hash:
            return None
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row.get("pending_email_token_hash") and row.get("pending_email_token_hash") == token_hash:
                    return User(**row)
        return None

    def find_user_by_prev_pending_email_token_hash(self, token_hash: str) -> User | None:
        """See User.prev_pending_email_token_hash -- same "was this
        superseded, not just invalid" nicety as
        find_user_by_prev_confirm_token_hash, for the same reason."""
        if not token_hash:
            return None
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row.get("prev_pending_email_token_hash") == token_hash:
                    return User(**row)
        return None

    def find_user_by_pending_email_cancel_token_hash(self, token_hash: str) -> User | None:
        """See User.pending_email_cancel_token_hash's own docstring --
        the no-login "cancel this pending email change" link's lookup,
        deliberately separate from find_user_by_pending_email_token_hash
        (the CONFIRM lookup) so possessing one token can never be used to
        perform the other action."""
        if not token_hash:
            return None
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row.get("pending_email_cancel_token_hash") and row.get("pending_email_cancel_token_hash") == token_hash:
                    return User(**row)
        return None

    def apply_pending_email(self, user_id: str) -> User | None:
        """Finalizes a confirmed email change: copies pending_email into
        the real email field and clears every pending_email_* field in
        the SAME locked read-modify-write cycle (never a separate
        clear_pending_email() call after the fact), so a concurrent
        request can never observe a half-applied state. Returns None (a
        no-op, not an error) if this user has no pending_email outstanding
        -- e.g. the change was already applied or aborted by the time
        this runs; the caller (my_confirm_email) should treat that the
        same as any other "already used" token."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    if not row.get("pending_email"):
                        return None
                    row["email"] = row["pending_email"]
                    row["pending_email"] = ""
                    row["pending_email_token_hash"] = ""
                    row["pending_email_token_created_at"] = ""
                    row["pending_email_cancel_token_hash"] = ""
                    write(rows, "apply email change")
                    return User(**row)
        return None

    # -- registrations ---------------------------------------------------------

    def count_confirmed(self, course_shortname: str, occurrence_date: str) -> int:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )

    def times_registered(self, user_id: str) -> int:
        """Total confirmed-or-was-confirmed bookings by this user, ever --
        used for the admin "how often have they registered" column. Computed
        on read, not stored, so it can never drift out of sync."""
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return sum(1 for r in rows if r["user_id"] == user_id)

    def add_registration(
        self,
        course_shortname: str,
        occurrence_date: str,
        user_id: str,
        cancel_token_hash: str,
        status: str = STATUS_CONFIRMED,
    ) -> Registration:
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            reg = Registration(
                registration_id=str(uuid.uuid4()),
                course_shortname=course_shortname,
                occurrence_date=occurrence_date,
                user_id=user_id,
                status=status,
                registered_at=now_iso(),
                guest_cancel_token_hash=cancel_token_hash,
            )
            rows.append(asdict(reg))
            write(rows, "add registration")
            return reg

    def add_registration_checking_capacity(
        self,
        course_shortname: str,
        occurrence_date: str,
        user_id: str,
        cancel_token_hash: str,
        capacity: int,
    ) -> Registration:
        """Same as add_registration, but decides confirmed-vs-waitlisted and
        inserts the row in a single locked read-modify-write cycle -- unlike
        calling count_confirmed() then add_registration() as two separate
        operations, this closes the race where two people booking the last
        spot at the same moment could both land as confirmed past capacity.

        2026-07-10, the operator: "it should not be possible to get 2 rows for the
        same course, same email and same slot/date... If I cancel: This is
        canceled. If then I rebook, then it should update the canceled
        booking, NOT add a 2nd one." -- app.webapp.App.book() already
        rejects a rebooking attempt outright while an existing row is still
        ACTIVE (see Store.has_active_registration), so by the time this is
        called from there, any existing row for this exact
        (course_shortname, occurrence_date, user_id) is guaranteed to be a
        non-active (canceled) one -- but this method enforces the
        invariant itself, unconditionally, rather than only relying on
        that caller-side guard: if ANY row already exists for this exact
        triple, it's updated in place (fresh status/registered_at/token,
        cancellation fields cleared, party linkage cleared -- this method
        is solo-booking only) instead of a new row ever being appended."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )
            status = STATUS_WAITLISTED if confirmed >= capacity else STATUS_CONFIRMED
            existing = next(
                (r for r in rows
                 if r["course_shortname"] == course_shortname
                 and r["occurrence_date"] == occurrence_date
                 and r["user_id"] == user_id),
                None,
            )
            if existing is not None:
                existing.update(
                    status=status, registered_at=now_iso(), guest_cancel_token_hash=cancel_token_hash,
                    canceled_at="", canceled_by="", host_message="",
                    party_id="", invited_by_user_id="",
                )
                write(rows, "rebook registration")
                return Registration(**existing)
            reg = Registration(
                registration_id=str(uuid.uuid4()),
                course_shortname=course_shortname,
                occurrence_date=occurrence_date,
                user_id=user_id,
                status=status,
                registered_at=now_iso(),
                guest_cancel_token_hash=cancel_token_hash,
            )
            rows.append(asdict(reg))
            write(rows, "add registration")
            return reg

    def add_party_registrations_checking_capacity(
        self,
        course_shortname: str,
        occurrence_date: str,
        entries: list[tuple[str, str]],
        capacity: int,
    ) -> list[Registration]:
        """Books an entire party -- the person who filled out the booking
        form (`entries[0]`, "the leader") plus any guests they added via
        "+ Add participant" -- as ONE atomic admission decision: either
        everyone is CONFIRMED or everyone is WAITLISTED, never split, in the
        same single locked read-modify-write cycle
        add_registration_checking_capacity uses for one row (see that
        method's own docstring for the race this closes). `entries` is a
        list of (user_id, guest_cancel_token_hash) pairs, one per party
        member; every row this creates shares one fresh `party_id`, and
        every row except the leader's (`entries[0]`) gets
        `invited_by_user_id` set to the leader's user_id -- see
        Registration's own docstring for what that records and why
        cancellation is deliberately NOT part of this atomicity (each party
        member can still cancel independently once booked).

        Deliberately NOT gated behind app.webapp's usual
        STATUS_PENDING_CONFIRMATION step for a brand-new guest email (see
        app/webapp.py::book()'s guest-booking branch and
        SOLUTION-DESIGN.md's guest-booking entry): an admission decision
        that can only be reached once every brand-new guest has separately
        clicked a confirmation email, possibly hours apart, doesn't match
        "admitted all together, decided now" -- the leader vouches for
        every guest they add by adding them, same trust model SimplyMeet.me
        used. Guests still get a real account (via
        Store.upsert_user_for_booking, called by the caller before this)
        and can later set a password to manage their own booking via /my.

        2026-07-10, the operator: "it should not be possible to get 2 rows for the
        same course, same email and same slot/date" -- same invariant
        add_registration_checking_capacity now enforces, applied per party
        member here too: if a member already has ANY row (typically a
        canceled one, from booking-then-canceling-then-being-added-to-a-
        new-party) for this exact course+date, that row is updated in
        place (fresh status/registered_at/token, cancellation fields
        cleared, party_id/invited_by_user_id updated to reflect THIS
        party) instead of a second row ever being appended for them."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )
            status = STATUS_CONFIRMED if confirmed + len(entries) <= capacity else STATUS_WAITLISTED
            party_id = str(uuid.uuid4())
            leader_user_id = entries[0][0]
            created = []
            for i, (user_id, cancel_token_hash) in enumerate(entries):
                invited_by_user_id = "" if i == 0 else leader_user_id
                existing = next(
                    (r for r in rows
                     if r["course_shortname"] == course_shortname
                     and r["occurrence_date"] == occurrence_date
                     and r["user_id"] == user_id),
                    None,
                )
                if existing is not None:
                    existing.update(
                        status=status, registered_at=now_iso(), guest_cancel_token_hash=cancel_token_hash,
                        canceled_at="", canceled_by="", host_message="",
                        party_id=party_id, invited_by_user_id=invited_by_user_id,
                    )
                    created.append(Registration(**existing))
                    continue
                reg = Registration(
                    registration_id=str(uuid.uuid4()),
                    course_shortname=course_shortname,
                    occurrence_date=occurrence_date,
                    user_id=user_id,
                    status=status,
                    registered_at=now_iso(),
                    guest_cancel_token_hash=cancel_token_hash,
                    party_id=party_id,
                    invited_by_user_id=invited_by_user_id,
                )
                rows.append(asdict(reg))
                created.append(reg)
            write(rows, "add party registrations")
            return created

    def confirm_pending_registration(
        self, registration_id: str, capacity: int, cancel_token_hash: str
    ) -> Registration | None:
        """Promotes ONE STATUS_PENDING_CONFIRMATION row to confirmed-or-
        waitlisted, re-checking capacity NOW (it may have filled up while
        this guest's account was still unconfirmed) in the same single
        locked read-modify-write cycle as add_registration_checking_capacity
        -- just updating an existing row instead of inserting one. Also
        sets a FRESH cancel_token_hash: the plaintext token handed out at
        pending-creation time was never persisted (only hashes ever are),
        so the caller generates a new one and emails it as part of the
        booked/waitlisted email this triggers, same as a normal booking.
        Returns None if the row isn't pending anymore (e.g. this
        confirmation link was already used, or the booking was canceled
        in the meantime) -- the caller should simply skip it, not treat
        that as an error."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            target = None
            for row in rows:
                if row["registration_id"] == registration_id and row["status"] == STATUS_PENDING_CONFIRMATION:
                    target = row
                    break
            if target is None:
                return None
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == target["course_shortname"]
                and r["occurrence_date"] == target["occurrence_date"]
                and r["status"] == STATUS_CONFIRMED
            )
            target["status"] = STATUS_WAITLISTED if confirmed >= capacity else STATUS_CONFIRMED
            target["guest_cancel_token_hash"] = cancel_token_hash
            write(rows, "confirm pending registration")
            return Registration(**target)

    def registrations_for_occurrence(
        self, course_shortname: str, occurrence_date: str
    ) -> list[Registration]:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return [
                Registration(**r) for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
            ]

    def registrations_for_user(self, user_id: str) -> list[Registration]:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return [Registration(**r) for r in rows if r["user_id"] == user_id]

    def has_active_registration(self, course_shortname: str, occurrence_date: str, user_id: str) -> bool:
        """2026-07-10, the operator (screenshot of /my): "double booking possible?"
        -- yes: add_registration_checking_capacity/
        add_party_registrations_checking_capacity only ever checked
        AGGREGATE capacity for a course+date, never whether THIS user
        already holds a spot there. Used by app.webapp.App.book() and
        _book_with_guests() as a pre-check before calling either of those,
        to reject "you're already booked for this session" up front.

        Deliberately CONFIRMED or WAITLISTED only, not
        STATUS_PENDING_CONFIRMATION -- a brand-new guest re-submitting the
        form before clicking their confirmation link is handled separately
        and on purpose (see book()'s own comment on that branch: re-sending
        the confirmation email on every attempt is intentional, not a bug).
        A returning user already WAITLISTED for this session still counts
        as "active" here too -- the operator confirmed he wants a second waitlist
        attempt blocked the same as a second confirmed booking, not treated
        as a way to grab a plus-one spot."""
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return any(
                r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["user_id"] == user_id
                and r["status"] in (STATUS_CONFIRMED, STATUS_WAITLISTED)
                for r in rows
            )

    def has_pending_registration(self, course_shortname: str, occurrence_date: str, user_id: str) -> bool:
        """The STATUS_PENDING_CONFIRMATION twin of has_active_registration()
        above (2026-07-11, the operator: "silent re-registration for unconfirmed
        accounts" -- a still-unconfirmed guest re-submitting the /book form
        for the exact same course+date, e.g. an accidental double-click or
        a "did that even work?" retry, used to insert ANOTHER bare
        add_registration() row every single time, with no dedup at all
        (unlike add_registration_checking_capacity's explicit "one row per
        course+date+user" upsert invariant for CONFIRMED bookings -- see
        that method's own 2026-07-10 docstring). Every one of those extra
        pending rows then got silently promoted together the moment the
        guest finally clicked ANY ONE confirmation link and set a password
        (see app.webapp.App.my_confirm's `pending` list, which has no
        per-course+date dedup either) -- so a guest who retried 3 times
        ended up CONFIRMED (or WAITLISTED) 3 times over for the same single
        class, each a separate row/cancel-token/calendar invite, without
        ever intending or noticing it.

        Used by app.webapp.App.book()'s pending-confirmation branch as a
        pre-check before add_registration(): if this returns True, the
        confirmation email is still resent (that part was always the
        deliberate, correct behavior -- see book()'s own comment) but no
        new row is inserted, so re-submitting settles down to exactly one
        pending row per course+date+user, same invariant confirmed
        bookings already had."""
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return any(
                r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["user_id"] == user_id
                and r["status"] == STATUS_PENDING_CONFIRMATION
                for r in rows
            )

    def find_by_guest_token_hash(self, token_hash: str) -> Registration | None:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            for r in rows:
                if r["guest_cancel_token_hash"] == token_hash and r["status"] in (
                    STATUS_CONFIRMED, STATUS_WAITLISTED,
                ):
                    return Registration(**r)
        return None

    def find_canceled_by_guest_token_hash(self, token_hash: str) -> Registration | None:
        """The reinstate twin of find_by_guest_token_hash() above (2026-07-10:
        the operator wants a no-login "magic link" reinstate page reachable from
        the cancellation email, same trust model as /cancel/<token>'s own
        link) -- matches CANCELED_BY_GUEST/CANCELED_BY_HOST instead of
        CONFIRMED/WAITLISTED, everything else identical. The hash this
        looks up is a FRESH token minted at cancellation time (see
        cancel()'s own `reinstate_token_hash` param for why the original
        booking's cancel token can't be reused here), not the guest's
        original cancel-link token. A blank token_hash never matches
        (every row's default is also ""), same guard as the sibling
        lookup."""
        if not token_hash:
            return None
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            for r in rows:
                if r["guest_cancel_token_hash"] == token_hash and r["status"] in (
                    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST,
                ):
                    return Registration(**r)
        return None

    def find_by_id(self, registration_id: str) -> Registration | None:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            for r in rows:
                if r["registration_id"] == registration_id:
                    return Registration(**r)
        return None

    def cancel(
        self, registration_id: str, canceled_by: str, host_message: str = "", reinstate_token_hash: str = "",
    ) -> bool:
        """canceled_by is 'guest' or 'host'. Works on confirmed, waitlisted,
        OR pending-confirmation rows (leaving the waitlist is just a
        cancel). Idempotent: canceling an already canceled registration is
        a no-op returning False.

        2026-07-13, the operator: a guest who registered but hasn't yet clicked
        their account-confirmation email link (STATUS_PENDING_CONFIRMATION
        -- see this status's own docstring) previously couldn't be
        canceled by ANY path at all, host or guest -- a real gap, since
        that guest is still fully expecting to attend. A pending row never
        held a real capacity slot or touched the calendar (same docstring),
        so canceling one here is just a status flip -- no promotion/
        calendar-sync consequence, same as it never having reserved
        anything in the first place.

        `reinstate_token_hash` (2026-07-10, the operator: a no-login "magic link"
        reinstate page reachable straight from the cancellation email,
        "like for cancel link") -- when given AND this call actually
        changes the row, OVERWRITES `guest_cancel_token_hash` with it in
        this same locked write. This is deliberate, not incidental: the
        ORIGINAL cancel token's plaintext was never persisted (only its
        hash was, same as every other token in this app -- see
        confirm_pending_registration's own docstring on why), so by the
        time a cancellation happens there's no way to hand the guest a
        working `/reinstate/<token>` link built from that original token
        even though its hash is still sitting right here. The caller
        (app/webapp.py's four cancel routes, app/cli_cancel.py) mints a
        FRESH token right before calling this, keeps the plaintext to put
        in the cancellation email's reinstate link, and passes this
        parameter as that new token's hash. Once overwritten, the OLD
        token's plaintext (from the guest's original booking-confirmation
        email) stops matching -- harmless, since that link was only ever
        good for canceling an active booking, and this one no longer is;
        find_by_guest_token_hash's own status filter would already show
        "invalid" for it regardless of whether the hash still matched.
        Omitted (the default), the hash is left exactly as it was, e.g.
        for a caller (or a test) that doesn't need this feature."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            changed = False
            for row in rows:
                if row["registration_id"] == registration_id and row["status"] in (
                    STATUS_CONFIRMED, STATUS_WAITLISTED, STATUS_PENDING_CONFIRMATION,
                ):
                    row["status"] = (
                        STATUS_CANCELED_BY_GUEST if canceled_by == "guest" else STATUS_CANCELED_BY_HOST
                    )
                    row["canceled_at"] = now_iso()
                    row["canceled_by"] = canceled_by
                    row["host_message"] = host_message
                    if reinstate_token_hash:
                        row["guest_cancel_token_hash"] = reinstate_token_hash
                    changed = True
            if changed:
                write(rows, "cancel registration")
            return changed

    def reinstate(self, registration_id: str, capacity: int) -> Registration | None:
        """Undo a cancellation (2026-07-10, the operator: "there should be then a
        reschedule button for canceled meetings which time (WHEN) is in the
        future" -- clarified in discussion to mean "undo the cancel", not
        move to a different occurrence). Only acts on a currently
        CANCELED_BY_GUEST/CANCELED_BY_HOST row -- a no-op (returns None)
        for anything else, e.g. a double-click/resubmit after the first
        call already flipped it back to confirmed/waitlisted, or a stale
        page showing a row someone else already reinstated/rebooked over.

        Re-decides confirmed-vs-waitlisted from CURRENT capacity, in the
        same single locked read-modify-write cycle as
        add_registration_checking_capacity/confirm_pending_registration --
        the class may have filled up (or emptied out) in the time since
        this row was canceled, so this is a fresh admission decision, not
        just flipping a flag back.

        Deliberately does NOT touch `guest_cancel_token_hash`: cancel()
        never clears it, so the ORIGINAL cancel link from this guest's very
        first booking-confirmation email still works to cancel this
        registration again after being reinstated -- no need to mint and
        email out a new one. Also deliberately does NOT re-parent or affect
        any other row sharing this row's party_id: reinstating, like
        canceling, is a per-registration action (a party's members can
        cancel independently; the same is true in reverse)."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            target = None
            for row in rows:
                if row["registration_id"] == registration_id and row["status"] in (
                    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST,
                ):
                    target = row
                    break
            if target is None:
                return None
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == target["course_shortname"]
                and r["occurrence_date"] == target["occurrence_date"]
                and r["status"] == STATUS_CONFIRMED
            )
            target["status"] = STATUS_WAITLISTED if confirmed >= capacity else STATUS_CONFIRMED
            target["canceled_at"] = ""
            target["canceled_by"] = ""
            target["host_message"] = ""
            write(rows, "reinstate registration")
            return Registration(**target)

    def all_registrations(self) -> list[Registration]:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return [Registration(**r) for r in rows]

    def replace_all_registrations(self, registrations: Iterable[Registration]) -> None:
        """Used by the retention job to rewrite the file after purging rows."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (_rows, write):
            write([asdict(r) for r in registrations], "purge retained registrations")

    # -- waitlist -------------------------------------------------------------

    def promote_next_waitlisted(
        self, course_shortname: str, occurrence_date: str, capacity: int
    ) -> list[Registration] | None:
        """Call this after any cancellation. If there's at least one free
        confirmed spot (confirmed count < capacity -- NOT assumed, checked
        here), promotes the longest-waiting waitlisted PARTY for this
        occurrence to confirmed, FIFO by the party's earliest
        registered_at. Returns the promoted Registration rows (one or more
        -- every row in that party), or None if nobody was waiting or
        there's no free spot at all (e.g. the cancellation was itself a
        waitlisted person leaving).

        Party-aware (2026-07, guest bookings): rows sharing a `party_id`
        (see add_party_registrations_checking_capacity) are promoted
        together, all or nothing, never split -- the same "admitted all or
        nothing" rule that decided their original confirmed-vs-waitlisted
        status applies again here. A row with a blank party_id (a solo
        booking made without guests -- including everything booked before
        this feature existed) is its own party of one, keyed by its
        registration_id instead. If the front-of-line party is bigger than
        the number of spots that just freed up, nothing is promoted this
        call -- a smaller party further back is never promoted ahead of it
        just because it happens to fit; same first-come-first-served
        guarantee as before, just applied per-party instead of per-row."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )
            free = capacity - confirmed
            if free <= 0:
                return None
            waitlisted = [
                r for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_WAITLISTED
            ]
            if not waitlisted:
                return None
            parties: dict[str, list[dict]] = {}
            for r in waitlisted:
                key = r.get("party_id") or r["registration_id"]
                parties.setdefault(key, []).append(r)
            ordered = sorted(parties.values(), key=lambda p: min(x["registered_at"] for x in p))
            front = ordered[0]
            if len(front) > free:
                return None
            for row in front:
                row["status"] = STATUS_CONFIRMED
            write(rows, "promote from waitlist")
            return [Registration(**r) for r in front]

    def import_historical_registration(
        self,
        registration_id: str,
        course_shortname: str,
        occurrence_date: str,
        user_id: str,
        status: str,
        registered_at: str,
        canceled_at: str = "",
        canceled_by: str = "",
        host_message: str = "",
        party_id: str = "",
        invited_by_user_id: str = "",
    ) -> bool:
        """One-off historical import (see app/migrate_simplymeet.py /
        scripts/migrate-simplymeet-history.py): unlike add_registration() /
        add_registration_checking_capacity(), the caller supplies
        registration_id/status/registered_at directly instead of generating
        or deriving them -- these rows describe bookings that already
        happened in a since-retired external tool, so there's no "now" to
        stamp them with and no live capacity to check (the session already
        ran). The migration script reuses that external tool's own numeric
        booking id to build registration_id, which is what makes this
        idempotent: re-running the same import after a partial run (or just
        to pick up newly-added rows) never creates a duplicate.

        `party_id`/`invited_by_user_id` (both blank by default, i.e. a solo
        historical row) let the migration script reconstruct a
        SimplyMeet.me booking's "Other participants" as linked guest
        registrations, same party_id/invited_by_user_id relationship a live
        guest booking gets from add_party_registrations_checking_capacity
        -- see Registration's own docstring. Deliberately NOT atomic across
        a party the way add_party_registrations_checking_capacity is: each
        call here writes exactly one row, since the migration script needs
        to check erasure-safety and idempotency per person, independently,
        before deciding whether that particular person's row gets written
        at all (see app/migrate_simplymeet.py's plan_import()).

        Returns False (a no-op, not an error) if a registration with this
        registration_id already exists -- callers should treat that as
        "already imported, nothing to do", not a failure."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            if any(r["registration_id"] == registration_id for r in rows):
                return False
            reg = Registration(
                registration_id=registration_id,
                course_shortname=course_shortname,
                occurrence_date=occurrence_date,
                user_id=user_id,
                status=status,
                party_id=party_id,
                invited_by_user_id=invited_by_user_id,
                registered_at=registered_at,
                guest_cancel_token_hash="",
                canceled_at=canceled_at,
                canceled_by=canceled_by,
                host_message=host_message,
            )
            rows.append(asdict(reg))
            write(rows, "import historical registration")
            return True

    # -- right to erasure (Art. 17 GDPR) -------------------------------------
    #
    # "Erasing" a user does not shred their booking history outright: it moves
    # their user row + every one of their registration rows out of the live
    # CSVs and into data/archived/*.csv, with the email replaced by a keyed
    # HMAC hash (see security.hash_email_for_erasure) and the name redacted.
    # Because the hash is keyed with a secret pepper that lives only in
    # secrets/erasure_pepper (never in the archive itself), it cannot be
    # reversed by guessing/dictionary-attacking email addresses the way a
    # bare sha256(email) could -- this is what makes it a defensible
    # pseudonymization rather than security theatre. Registration rows (which
    # never held name/email to begin with, only user_id) move as-is.

    def erase_user(self, user_id: str, hashed_email: str) -> bool:
        """Returns False if the user_id doesn't exist (already erased/never existed)."""
        archived_user_row = None
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            keep = []
            for row in rows:
                if row["user_id"] == user_id:
                    archived_user_row = dict(row)
                    archived_user_row["email"] = hashed_email
                    archived_user_row["name"] = "[erased]"
                else:
                    keep.append(row)
            if archived_user_row is None:
                return False
            write(keep, "erase user")

        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            keep, moving = [], []
            for row in rows:
                (moving if row["user_id"] == user_id else keep).append(row)
            write(keep, "erase user registrations")

        with _LockedCsv(self.archived_users_path, USER_FIELDS) as (rows, write):
            rows.append(archived_user_row)
            write(rows, "archive erased user")

        if moving:
            with _LockedCsv(self.archived_registrations_path, REG_FIELDS) as (rows, write):
                rows.extend(moving)
                write(rows, "archive erased user registrations")
        return True

    def merge_archived_registrations(self, archived_user_ids: list[str], into_user_id: str) -> int:
        """Explicit, admin-invoked history merge -- NOT part of erase_user's
        own archival, and NOT any kind of un-erasure. Moves every
        registration row currently archived under one of `archived_user_ids`
        into the LIVE registrations CSV, rewriting its `user_id` to
        `into_user_id` (the live account the admin is re-attaching this
        history to). `registration_id` is preserved unchanged, same as
        erase_user() preserves it across the live/archived boundary.

        What this does NOT touch:
          - The archived user row(s) themselves (data/archived/users.csv):
            name stays "[erased]", email stays the hashed value, forever.
            This function never reads or writes archived_users_path.
          - Any registration NOT belonging to one of archived_user_ids.
          - Capacity/waitlist for the occurrences involved -- these rows
            were already force-canceled (see erase_user_by_email) before
            being archived, so a moved-back row is never CONFIRMED/
            WAITLISTED and can never double-book a live slot. Anything that
            somehow isn't canceled is left that way rather than silently
            changed here (this method reattaches history, it doesn't
            re-evaluate booking state).

        2026-07-10, the operator: this was the actual root cause of a real
        duplicate-row report ("it should not be possible to get 2 rows for
        the same course, same email and same slot/date... here the
        problem might be the ARCHIVE as the 2nd row was archived!") -- this
        method used to move every matching archived row into the live CSV
        unconditionally, with no check for whether `into_user_id` ALREADY
        had its own live row for that exact (course_shortname,
        occurrence_date) -- e.g. an account gets erased with a canceled
        booking for some date, a fresh account under the same email books
        (and maybe cancels) that SAME date again, and later an admin merges
        the old archived history back in: two rows for the same course+date
        would land side by side. Fixed: any archived row whose
        (course_shortname, occurrence_date) already has a live row under
        `into_user_id` is simply DROPPED (never written to live, and
        already removed from the archive in the first pass below) rather
        than moved in -- the operator, asked to confirm: "for a FUTURE date its ok
        to remove the canceled archive row then when it is activated
        again" -- applied uniformly regardless of past/future, matching the
        same unconditional invariant the two add_*_checking_capacity()
        methods now enforce, rather than adding a date-dependent special
        case for what's already a rare, admin-invoked operation.

        Returns the number of registration rows actually moved into the
        live CSV (0 if none matched, or if every match was dropped as a
        duplicate -- the caller should treat either as "nothing to merge",
        not an error)."""
        with _LockedCsv(self.archived_registrations_path, REG_FIELDS) as (rows, write):
            keep, moving = [], []
            for row in rows:
                (moving if row["user_id"] in archived_user_ids else keep).append(row)
            if not moving:
                return 0
            write(keep, "merge archived registrations (remove)")

        for row in moving:
            row["user_id"] = into_user_id

        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            live_course_dates = {
                (r["course_shortname"], r["occurrence_date"])
                for r in rows if r["user_id"] == into_user_id
            }
            to_move = [
                row for row in moving
                if (row["course_shortname"], row["occurrence_date"]) not in live_course_dates
            ]
            rows.extend(to_move)
            write(rows, "merge archived registrations (restore)")
        return len(to_move)

    def rename_course_shortname(self, old_shortname: str, new_shortname: str) -> int:
        """Rewrites `course_shortname` from `old_shortname` to
        `new_shortname` on every registration row -- live AND archived --
        e.g. after renaming a course in settings.toml (2026-07-08, the operator:
        "rename lux-wed-mindfulness to lux-wed-mind ... provide a command
        to migrate the existing data AFTER I installed this change").

        Deliberately does NOT touch settings.toml itself (a one-line
        manual edit, not a bulk data operation) and does NOT touch the
        calendar -- see app.calendar_sync.resync_after_course_rename for
        that, a genuinely separate concern: renaming changes this
        course's calendar event UID (see event_uid's own docstring, the
        shortname is baked directly into it), so any already-synced
        future occurrence needs its OLD-uid event explicitly cleaned up
        too, or it's left orphaned on the calendar forever alongside a
        fresh one under the new uid.

        Returns the total number of rows changed, live + archived
        combined (0 if `old_shortname` matched nothing in either file --
        the caller should treat that as "nothing to migrate", not an
        error, e.g. if this is re-run after already succeeding once)."""
        changed = 0
        for path in (self.registrations_path, self.archived_registrations_path):
            with _LockedCsv(path, REG_FIELDS) as (rows, write):
                n = 0
                for row in rows:
                    if row["course_shortname"] == old_shortname:
                        row["course_shortname"] = new_shortname
                        n += 1
                if n:
                    write(rows, f"rename course {old_shortname!r} -> {new_shortname!r}")
                changed += n
        return changed

    # -- reporting: live + archived, for the my-bt CLI -----------------------

    def read_users(self, scope: str = "all") -> list[dict]:
        """scope: 'live' | 'archived' | 'all'."""
        out = []
        if scope in ("live", "all"):
            out += _read_csv_plain(self.users_path, USER_FIELDS)
        if scope in ("archived", "all"):
            out += _read_csv_plain(self.archived_users_path, USER_FIELDS)
        return out

    def read_registrations(self, scope: str = "all") -> list[dict]:
        """scope: 'live' | 'archived' | 'all'."""
        out = []
        if scope in ("live", "all"):
            out += _read_csv_plain(self.registrations_path, REG_FIELDS)
        if scope in ("archived", "all"):
            out += _read_csv_plain(self.archived_registrations_path, REG_FIELDS)
        return out
