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
    "confirm_token_hash", "confirm_token_created_at", "prev_confirm_token_hash",
    "created_at", "last_login_at",
    "pending_email", "pending_email_token_hash", "pending_email_token_created_at",
    "prev_pending_email_token_hash",
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

    def set_name(self, user_id: str, name: str) -> None:
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["name"] = name
                    write(rows)
                    return

    def set_pending_email(self, user_id: str, pending_email: str, token_hash: str, created_at: str) -> None:
        """Requests an email change -- see User.pending_email. Shifts any
        previously-outstanding pending-email token into
        prev_pending_email_token_hash first (same "tell them a newer link
        superseded this one" pattern as set_confirm_token /
        prev_confirm_token_hash), and OVERWRITES any earlier pending_email
        outright: only one pending change can ever be outstanding at a
        time ("one active + one pending max"), so requesting a second
        change -- even to a different address -- simply replaces the
        first rather than queuing alongside it."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["prev_pending_email_token_hash"] = row.get("pending_email_token_hash", "")
                    row["pending_email"] = pending_email
                    row["pending_email_token_hash"] = token_hash
                    row["pending_email_token_created_at"] = created_at
                    write(rows)
                    return

    def clear_pending_email(self, user_id: str) -> None:
        """Aborts a pending email change (guest-initiated cancel from
        /my/settings). Deliberately does NOT touch prev_pending_email_token_hash
        -- an aborted change's token should read as a plain "invalid/already
        used" link if somehow clicked afterward, not the friendlier
        "superseded by a newer one" message, since nothing newer was ever
        sent."""
        with _LockedCsv(self.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user_id:
                    row["pending_email"] = ""
                    row["pending_email_token_hash"] = ""
                    row["pending_email_token_created_at"] = ""
                    write(rows)
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
                    write(rows)
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
        and can later set a password to manage their own booking via /my."""
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
                reg = Registration(
                    registration_id=str(uuid.uuid4()),
                    course_shortname=course_shortname,
                    occurrence_date=occurrence_date,
                    user_id=user_id,
                    status=status,
                    registered_at=now_iso(),
                    guest_cancel_token_hash=cancel_token_hash,
                    party_id=party_id,
                    invited_by_user_id="" if i == 0 else leader_user_id,
                )
                rows.append(asdict(reg))
                created.append(reg)
            write(rows)
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
            write(rows)
            return Registration(**target)

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
            write(rows)
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
            write(rows)
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
