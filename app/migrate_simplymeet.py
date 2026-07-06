"""One-off migration: import SimplyMeet.me's booking history export into
my-booking-tool's own Store, for the 2026-07 cutover away from
SimplyMeet.me (see scripts/migrate-simplymeet-history.py and
SOLUTION-DESIGN.md's migration entry for the standing decisions recorded
below).

This module holds all the parsing/mapping/decision logic; the script
itself (scripts/migrate-simplymeet-history.py) is a thin CLI wrapper --
same "testable app/ module + thin script" split as every other my-bt
subcommand (see app/cli_history.py's docstring), and for the same reason:
unittest needs an importable module, and the script lives outside `app/`
with argument parsing that isn't worth unit-testing directly.

This is deliberately NOT a `my-bt` subcommand -- see SOLUTION-DESIGN.md: a
permanent `--import-from-simplymeet.me` flag isn't worth the added surface
area for something run exactly once during the cutover.

**Decisions baked into plan_import() below -- flagged here since none of
them are things the SimplyMeet.me export can actually tell us, and the operator
should sanity-check them against the dry-run report before passing
--commit:**

  - "Past" means the exact same thing it means everywhere else in this
    codebase: occurrence_date < today (see app/cli_list.py::filter_by_date
    and app/webapp.py::admin_overview's own today-or-later default view).
    An occurrence dated today or later is skipped as "future" -- the operator's
    own words were "migrate the HISTORY of all bookings ... (all except
    future bookings)".

  - SimplyMeet.me's export records THAT a booking was canceled and WHEN,
    but never WHO canceled it (guest vs. host). Every canceled row is
    imported as STATUS_CANCELED_BY_GUEST -- the overwhelmingly common real
    case for a one-off class booking -- rather than guessed per-row. If
    that's wrong for a specific booking, it's a one-field CSV edit after
    the fact, not a reason to block the whole import.

  - SimplyMeet.me's export has no "booking created at" timestamp at all --
    only the occurrence's own date/time and (if canceled) the cancellation
    time. `registered_at` is therefore set to a placeholder
    ("<occurrence_date>T00:00:00", no real time-of-day), not a real
    original signup time. This only matters for FIFO waitlist ordering and
    admin display -- these are closed historical rows, never live
    capacity/waitlist candidates again, so the placeholder has no
    functional effect on anything going forward.

  - SimplyMeet.me's "Other participants" column (extra semicolon-separated
    emails CC'd onto someone else's booking) IS imported now (2026-07-06,
    once my-booking-tool grew its own guest-booking model -- see
    SOLUTION-DESIGN.md's guest-booking entry) as linked guest
    registrations: the row's own "Client email" becomes the party leader,
    each "Other participants" address becomes a guest sharing the same
    party_id, invited_by_user_id pointing at the leader -- exactly the
    same shape a live guest booking produces (see
    Store.add_party_registrations_checking_capacity), just written one row
    at a time via Store.import_historical_registration() instead of
    atomically (see that method's docstring for why: erasure-safety and
    idempotency are checked per PERSON here, not per party, so one bad
    guest email never blocks the leader's own otherwise-valid row). A
    guest's status/cancellation always matches the leader's row (SimplyMeet.me's
    export has no PER-PARTICIPANT status at all -- "Is canceled" is a
    property of the booking, not of any one attendee) and their name is
    never known from this export, so it's resolved the same way a live
    guest booking resolves a blank name (see plan_import(): an existing
    account's real name if there is one, else the placeholder "Guest").
    A malformed, duplicate (matching the leader or another guest on the
    same row), or already-erased guest email is simply skipped and
    counted -- never blocks the leader's own row from importing.

  - If an email's only my-booking-tool history is an ERASED (archived,
    hashed) account, that person's row is skipped rather than silently
    creating a fresh live account for them from old SimplyMeet.me data --
    bulk-importing history is not the same thing as that person choosing
    to re-register, and this migration shouldn't be the thing that
    decides that for them. Applies independently to the leader AND to
    each "Other participants" guest. See find_archived_user_ids_for_email().
"""
from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Course, Settings
from .erasure import find_archived_user_ids_for_email
from .storage import STATUS_CANCELED_BY_GUEST, STATUS_CONFIRMED, Store

REGISTRATION_ID_PREFIX = "simplymeet-"

# Below this difflib.SequenceMatcher ratio, two titles are treated as
# genuinely different courses, not a rename/typo -- see _match_course().
# 0.72 was picked empirically against the 2026-07 export: it's loose
# enough to catch a punctuation/wording tweak (e.g. a course renamed
# slightly since the export was taken) but still rejects two distinct
# courses that happen to share a few words (e.g. two different "DBG-only
# ... Yoga" titles for different weekdays).
FUZZY_MATCH_CUTOFF = 0.72


def _normalize_title(s: str) -> str:
    """Case/whitespace-insensitive comparison key -- catches the common,
    harmless case where a title differs only by capitalization or extra/
    collapsed whitespace, before falling back to full fuzzy matching."""
    return " ".join(s.lower().split())


def _match_course(meeting_type: str, courses: tuple[Course, ...]) -> tuple[Course | None, str | None]:
    """Matches a SimplyMeet.me "Meeting type" string against configured
    `[[course]]` titles, in three tiers -- returns (course, note); `note`
    is None for an exact match (nothing to flag), and a human-readable
    explanation whenever a looser tier was needed, so plan_import() can
    surface it in the report for the operator to double-check before --commit
    (see 2026-07-06: "Please allow to map the Mindfulness bookings as
    well ... maybe the title is now slightly different, but still largely
    the same" -- a course was renamed in settings.toml since the export
    was taken, so a strict exact-match-only policy started silently
    dropping real history):

    1. Exact string match -- the common, safe case; no note.
    2. Normalized match (case/whitespace differences only) -- a near-
       certain match, still flagged so it's visible in the report.
    3. Fuzzy match (difflib, cutoff=FUZZY_MATCH_CUTOFF) -- ONLY when
       exactly one course clears the cutoff; if zero or more than one do,
       this is deliberately treated as no match at all rather than
       guessing between two plausible candidates -- see the "ambiguous"
       branch below."""
    for c in courses:
        if c.title == meeting_type:
            return c, None

    normalized = _normalize_title(meeting_type)
    for c in courses:
        if _normalize_title(c.title) == normalized:
            return c, (
                f"{meeting_type!r} matched configured course {c.title!r} (shortname {c.shortname!r}) "
                "-- only a capitalization/whitespace difference, treated as the same course"
            )

    titles = [c.title for c in courses]
    close = difflib.get_close_matches(meeting_type, titles, n=2, cutoff=FUZZY_MATCH_CUTOFF)
    if len(close) == 1:
        c = next(c for c in courses if c.title == close[0])
        return c, (
            f"{meeting_type!r} fuzzy-matched to configured course {c.title!r} (shortname {c.shortname!r}) "
            "-- VERIFY this is the same course before trusting --commit for these rows"
        )
    if len(close) > 1:
        return None, (
            f"{meeting_type!r} fuzzy-matched MORE THAN ONE configured course ({', '.join(close)}) -- "
            "too ambiguous to guess; add/rename a [[course]] title so exactly one matches, or ignore "
            "if this meeting type genuinely no longer exists"
        )
    return None, None


@dataclass(frozen=True)
class ImportPlan:
    """One person's worth of decided action, before anything is written --
    kept separate from execution so a dry run can print exactly what would
    happen without touching the store at all (see run_migration()). A row
    with SimplyMeet.me "Other participants" produces one leader ImportPlan
    plus one more per valid guest, all sharing `party_id` -- see this
    module's own docstring."""
    simplymeet_id: str  # the SOURCE ROW's id -- shared by a leader and all its guests
    registration_id: str  # this PERSON's own registration_id (leader vs. guest differ)
    email: str
    name: str
    course_shortname: str
    occurrence_date: str
    status: str
    registered_at: str
    canceled_at: str
    canceled_by: str
    party_id: str = ""
    invited_by_email: str = ""  # "" for the leader; the leader's email for a guest


@dataclass
class MigrationReport:
    planned: list[ImportPlan]
    skipped_future: int = 0
    skipped_unmatched_course: list[str] = field(default_factory=list)
    skipped_already_imported: int = 0
    skipped_erased_email: int = 0
    skipped_missing_email: int = 0
    guests_imported: int = 0
    # Course-title matching that wasn't an exact string match (see
    # _match_course()) -- these rows DID get imported (fuzzy_matched_courses)
    # or DIDN'T because more than one course was equally plausible
    # (ambiguous_course_matches, folded into skipped_unmatched_course too).
    # Surfaced separately so the CLI report can call them out distinctly --
    # the operator should read every line here before trusting --commit for those
    # rows.
    fuzzy_matched_courses: list[str] = field(default_factory=list)
    ambiguous_course_matches: list[str] = field(default_factory=list)
    skipped_guest_duplicate: int = 0
    skipped_guest_malformed: int = 0
    skipped_guest_erased: int = 0


def parse_simplymeet_export(path: str | Path) -> list[dict]:
    """Reads a SimplyMeet.me "List view" export as-is: header row `id,
    "Date and time", Duration, Client, "Client phone number", "Client
    email", "Meeting type", "Meeting name", Location, "User name ", Notes,
    "Is canceled", "Cancellation time", "Other participants"` plus unused
    Payment columns (this deployment never took payments through
    SimplyMeet.me). `utf-8-sig` handles the leading BOM SimplyMeet.me's
    exporter writes. No transformation here beyond what csv.DictReader
    gives you -- see plan_import() for all the actual decision logic."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _parse_other_participants(raw: str) -> list[str]:
    """SimplyMeet.me writes this column as e.g. "a@b.com; c@d.com; "
    (semicolon-separated, trailing separator, inconsistent spacing) --
    splits, strips, lowercases, and drops anything left blank."""
    return [p.strip().lower() for p in (raw or "").split(";") if p.strip()]


def plan_import(
    rows: list[dict],
    settings: Settings,
    store: Store,
    *,
    today: date | None = None,
) -> MigrationReport:
    """Decides what to do with each exported row without writing anything
    (see run_migration() for the write step) -- see this module's own
    docstring for the assumptions baked in below."""
    today = today or date.today()
    report = MigrationReport(planned=[])

    for row in rows:
        occ_dt = datetime.strptime(row["Date and time"].strip(), "%Y-%m-%d %H:%M")
        occurrence_date = occ_dt.date()
        if occurrence_date >= today:
            report.skipped_future += 1
            continue

        meeting_type = (row.get("Meeting type") or "").strip()
        course, fuzzy_note = _match_course(meeting_type, settings.courses)
        if course is None:
            report.skipped_unmatched_course.append(meeting_type)
            if fuzzy_note:
                report.ambiguous_course_matches.append(fuzzy_note)
            continue
        if fuzzy_note:
            report.fuzzy_matched_courses.append(fuzzy_note)

        leader_email = (row.get("Client email") or "").strip().lower()
        if not leader_email:
            report.skipped_missing_email += 1
            continue

        if find_archived_user_ids_for_email(store, settings, leader_email):
            report.skipped_erased_email += 1
            continue

        simplymeet_id = row["id"].strip()
        leader_registration_id = f"{REGISTRATION_ID_PREFIX}{simplymeet_id}"
        if store.find_by_id(leader_registration_id) is not None:
            report.skipped_already_imported += 1
            continue

        occurrence_iso = occurrence_date.isoformat()
        is_canceled = (row.get("Is canceled") or "").strip().lower() == "yes"
        if is_canceled:
            status = STATUS_CANCELED_BY_GUEST
            cancel_raw = (row.get("Cancellation time") or "").strip()
            canceled_at = (
                datetime.strptime(cancel_raw, "%Y-%m-%d %H:%M").isoformat()
                if cancel_raw else f"{occurrence_iso}T00:00:00"
            )
            canceled_by = "guest"
        else:
            status = STATUS_CONFIRMED
            canceled_at = ""
            canceled_by = ""
        registered_at = f"{occurrence_iso}T00:00:00"

        # -- guests ("Other participants") -----------------------------------
        # Validated independently per guest email -- a bad one is skipped
        # and counted, never blocks the leader's own row (see this module's
        # docstring). party_id only gets set if at least one guest survives
        # validation; a row with an "Other participants" value that's
        # entirely blank/duplicate/erased ends up a perfectly ordinary solo
        # import, same as a row with no "Other participants" at all.
        seen_emails = {leader_email}
        guest_plans: list[ImportPlan] = []
        for i, guest_email in enumerate(_parse_other_participants(row.get("Other participants", ""))):
            if "@" not in guest_email:
                report.skipped_guest_malformed += 1
                continue
            if guest_email in seen_emails:
                report.skipped_guest_duplicate += 1
                continue
            seen_emails.add(guest_email)
            if find_archived_user_ids_for_email(store, settings, guest_email):
                report.skipped_guest_erased += 1
                continue
            guest_registration_id = f"{leader_registration_id}-guest-{i}"
            if store.find_by_id(guest_registration_id) is not None:
                report.skipped_already_imported += 1
                continue
            guest_plans.append(ImportPlan(
                simplymeet_id=simplymeet_id,
                registration_id=guest_registration_id,
                email=guest_email,
                name="",  # resolved in run_migration(): existing account's name, else "Guest"
                course_shortname=course.shortname,
                occurrence_date=occurrence_iso,
                status=status,
                registered_at=registered_at,
                canceled_at=canceled_at,
                canceled_by=canceled_by,
                party_id="",  # filled in below once we know there's >=1 real guest
                invited_by_email=leader_email,
            ))

        party_id = f"simplymeet-party-{simplymeet_id}" if guest_plans else ""
        report.planned.append(ImportPlan(
            simplymeet_id=simplymeet_id,
            registration_id=leader_registration_id,
            email=leader_email,
            name=(row.get("Client") or "").strip(),
            course_shortname=course.shortname,
            occurrence_date=occurrence_iso,
            status=status,
            registered_at=registered_at,
            canceled_at=canceled_at,
            canceled_by=canceled_by,
            party_id=party_id,
            invited_by_email="",
        ))
        for gp in guest_plans:
            report.planned.append(
                ImportPlan(
                    simplymeet_id=gp.simplymeet_id, registration_id=gp.registration_id,
                    email=gp.email, name=gp.name, course_shortname=gp.course_shortname,
                    occurrence_date=gp.occurrence_date, status=gp.status,
                    registered_at=gp.registered_at, canceled_at=gp.canceled_at,
                    canceled_by=gp.canceled_by, party_id=party_id, invited_by_email=gp.invited_by_email,
                )
            )
            report.guests_imported += 1
    return report


def run_migration(plans: list[ImportPlan], store: Store) -> int:
    """Actually writes: find-or-create each user by email (never overwriting
    an existing user's name if the account already exists -- only a
    brand-new account gets its name from the SimplyMeet.me row, or the
    placeholder "Guest" if even that's blank, same fallback a live guest
    booking uses -- see app/webapp.py::App._book_with_guests), then
    Store.import_historical_registration() per plan, resolving each guest's
    invited_by_email to that leader's now-known user_id.

    Order-independent by design: `plan_import()` always places a leader's
    plan before its guests' in the list it returns, but this function
    doesn't rely on that -- a guest's `invited_by_email` is resolved via
    `_user_id_for_email()` below, which checks the in-process cache first
    and falls back to a fresh `store.find_user_by_email()` lookup, so it
    still works correctly even if a caller re-orders or filters `plans`
    before passing them in.

    Returns the number of registrations actually written (a plan can still
    be skipped here if a matching registration_id appeared between
    plan_import() and now -- import_historical_registration() re-checks;
    see its own docstring)."""
    written = 0
    user_id_by_email: dict[str, str] = {}

    def _user_id_for_email(email: str) -> str:
        if email in user_id_by_email:
            return user_id_by_email[email]
        existing = store.find_user_by_email(email)
        if existing is not None:
            user_id_by_email[email] = existing.user_id
            return existing.user_id
        return ""

    for plan in plans:
        existing = store.find_user_by_email(plan.email)
        if existing is not None:
            user = existing
        else:
            resolved_name = plan.name or "Guest"
            user = store.upsert_user_for_booking(plan.email, resolved_name)
        user_id_by_email[plan.email] = user.user_id

        invited_by_user_id = _user_id_for_email(plan.invited_by_email) if plan.invited_by_email else ""
        created = store.import_historical_registration(
            registration_id=plan.registration_id,
            course_shortname=plan.course_shortname,
            occurrence_date=plan.occurrence_date,
            user_id=user.user_id,
            status=plan.status,
            registered_at=plan.registered_at,
            canceled_at=plan.canceled_at,
            canceled_by=plan.canceled_by,
            party_id=plan.party_id,
            invited_by_user_id=invited_by_user_id,
        )
        if created:
            written += 1
    return written
