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
import os
import tempfile
import uuid
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .security import sanitize_csv_field

USER_FIELDS = [
    "user_id", "email", "name", "password_hash", "password_salt",
    "confirm_token_hash", "confirm_token_created_at", "created_at", "last_login_at",
]
REG_FIELDS = [
    "registration_id", "course_shortname", "occurrence_date", "user_id", "status",
    "registered_at", "guest_cancel_token_hash", "canceled_at", "canceled_by", "host_message",
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    created_at: str = ""
    last_login_at: str = ""


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

    def _set_rows_to_write(self, rows: list[dict]) -> None:
        if self.readonly:
            raise RuntimeError("_LockedCsv(readonly=True) must never call write()")
        self._to_write = rows

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self._to_write is not None:
                self._atomic_write(self._to_write)
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
                    write(rows)
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
            write(rows)
            return user

    def set_confirm_token(self, user_id: str, token_hash: str, created_at: str) -> None:
        """Stores a pending confirm-or-reset token for this user -- see
        User.confirm_token_hash's docstring for why the same field covers
        both the first-ever confirmation and a later password reset."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["confirm_token_hash"] = token_hash
                    row["confirm_token_created_at"] = created_at
                    write(rows)
                    return

    def find_user_by_confirm_token_hash(self, token_hash: str) -> User | None:
        if not token_hash:
            return None  # never match on a blank hash (no user has "" stored as a real token)
        with _LockedCsv(self.users_path, USER_FIELDS, readonly=True) as (rows, _write):
            for row in rows:
                if row["confirm_token_hash"] and row["confirm_token_hash"] == token_hash:
                    return User(**row)
        return None

    def set_password(self, user_id: str, password_hash: str, password_salt: str) -> None:
        """Sets the account's real login password and consumes (clears) any
        pending confirm/reset token -- a used link can't be replayed."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["password_hash"] = password_hash
                    row["password_salt"] = password_salt
                    row["confirm_token_hash"] = ""
                    row["confirm_token_created_at"] = ""
                    write(rows)
                    return

    def touch_login(self, user_id: str) -> None:
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            changed = False
            for row in rows:
                if row["user_id"] == user_id:
                    row["last_login_at"] = now_iso()
                    changed = True
            if changed:
                write(rows)

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
            write(rows)
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
        spot at the same moment could both land as confirmed past capacity."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )
            status = STATUS_WAITLISTED if confirmed >= capacity else STATUS_CONFIRMED
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
            write(rows)
            return reg

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
            write(rows)
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

    def find_by_guest_token_hash(self, token_hash: str) -> Registration | None:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            for r in rows:
                if r["guest_cancel_token_hash"] == token_hash and r["status"] in (
                    STATUS_CONFIRMED, STATUS_WAITLISTED,
                ):
                    return Registration(**r)
        return None

    def find_by_id(self, registration_id: str) -> Registration | None:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            for r in rows:
                if r["registration_id"] == registration_id:
                    return Registration(**r)
        return None

    def cancel(self, registration_id: str, canceled_by: str, host_message: str = "") -> bool:
        """canceled_by is 'guest' or 'host'. Works on confirmed OR waitlisted
        rows (leaving the waitlist is just a cancel). Idempotent: canceling
        an already canceled registration is a no-op returning False."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            changed = False
            for row in rows:
                if row["registration_id"] == registration_id and row["status"] in (
                    STATUS_CONFIRMED, STATUS_WAITLISTED,
                ):
                    row["status"] = (
                        STATUS_CANCELED_BY_GUEST if canceled_by == "guest" else STATUS_CANCELED_BY_HOST
                    )
                    row["canceled_at"] = now_iso()
                    row["canceled_by"] = canceled_by
                    row["host_message"] = host_message
                    changed = True
            if changed:
                write(rows)
            return changed

    def all_registrations(self) -> list[Registration]:
        with _LockedCsv(self.registrations_path, REG_FIELDS, readonly=True) as (rows, _write):
            return [Registration(**r) for r in rows]

    def replace_all_registrations(self, registrations: Iterable[Registration]) -> None:
        """Used by the retention job to rewrite the file after purging rows."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (_rows, write):
            write([asdict(r) for r in registrations])

    # -- waitlist -------------------------------------------------------------

    def promote_next_waitlisted(
        self, course_shortname: str, occurrence_date: str, capacity: int
    ) -> Registration | None:
        """Call this after any cancellation. If there's a free confirmed
        spot (confirmed count < capacity -- NOT assumed, checked here),
        promotes the longest-waiting waitlisted registration for this
        occurrence to confirmed, FIFO by registered_at. Returns the promoted
        Registration, or None if nobody was waiting or there's no free spot
        (e.g. the cancellation was itself a waitlisted person leaving)."""
        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            confirmed = sum(
                1 for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_CONFIRMED
            )
            if confirmed >= capacity:
                return None
            waitlisted = [
                r for r in rows
                if r["course_shortname"] == course_shortname
                and r["occurrence_date"] == occurrence_date
                and r["status"] == STATUS_WAITLISTED
            ]
            if not waitlisted:
                return None
            waitlisted.sort(key=lambda r: r["registered_at"])
            candidate = waitlisted[0]
            candidate["status"] = STATUS_CONFIRMED
            write(rows)
            return Registration(**candidate)

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
            write(keep)

        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            keep, moving = [], []
            for row in rows:
                (moving if row["user_id"] == user_id else keep).append(row)
            write(keep)

        with _LockedCsv(self.archived_users_path, USER_FIELDS) as (rows, write):
            rows.append(archived_user_row)
            write(rows)

        if moving:
            with _LockedCsv(self.archived_registrations_path, REG_FIELDS) as (rows, write):
                rows.extend(moving)
                write(rows)
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

        Returns the number of registration rows moved (0 if none matched --
        the caller should treat that as "nothing to merge", not an error)."""
        with _LockedCsv(self.archived_registrations_path, REG_FIELDS) as (rows, write):
            keep, moving = [], []
            for row in rows:
                (moving if row["user_id"] in archived_user_ids else keep).append(row)
            if not moving:
                return 0
            write(keep)

        for row in moving:
            row["user_id"] = into_user_id

        with _LockedCsv(self.registrations_path, REG_FIELDS) as (rows, write):
            rows.extend(moving)
            write(rows)
        return len(moving)

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
