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

  - my-booking-tool has no multi-guest-per-registration model (one
    registration = one person). SimplyMeet.me's "Other participants"
    column (extra semicolon-separated emails CC'd onto someone else's
    booking) is NOT imported as separate registrations -- rows that had
    one are counted and flagged in the report so the operator can review them
    manually if that history matters.

  - If an email's only my-booking-tool history is an ERASED (archived,
    hashed) account, the row is skipped rather than silently creating a
    fresh live account for them from old SimplyMeet.me data -- bulk-
    importing history is not the same thing as that person choosing to
    re-register, and this migration shouldn't be the thing that decides
    that for them. See find_archived_user_ids_for_email().
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Settings
from .erasure import find_archived_user_ids_for_email
from .storage import STATUS_CANCELED_BY_GUEST, STATUS_CONFIRMED, Store

REGISTRATION_ID_PREFIX = "simplymeet-"


@dataclass(frozen=True)
class ImportPlan:
    """One row's worth of decided action, before anything is written -- kept
    separate from execution so a dry run can print exactly what would
    happen without touching the store at all (see run_migration())."""
    simplymeet_id: str
    email: str
    name: str
    course_shortname: str
    occurrence_date: str
    status: str
    registered_at: str
    canceled_at: str
    canceled_by: str


@dataclass
class MigrationReport:
    planned: list[ImportPlan]
    skipped_future: int = 0
    skipped_unmatched_course: list[str] = field(default_factory=list)
    skipped_already_imported: int = 0
    skipped_erased_email: int = 0
    skipped_missing_email: int = 0
    rows_with_other_participants: int = 0


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
    title_to_course = {c.title: c for c in settings.courses}
    report = MigrationReport(planned=[])

    for row in rows:
        occ_dt = datetime.strptime(row["Date and time"].strip(), "%Y-%m-%d %H:%M")
        occurrence_date = occ_dt.date()
        if occurrence_date >= today:
            report.skipped_future += 1
            continue

        meeting_type = (row.get("Meeting type") or "").strip()
        course = title_to_course.get(meeting_type)
        if course is None:
            report.skipped_unmatched_course.append(meeting_type)
            continue

        email = (row.get("Client email") or "").strip().lower()
        if not email:
            report.skipped_missing_email += 1
            continue

        if find_archived_user_ids_for_email(store, settings, email):
            report.skipped_erased_email += 1
            continue

        simplymeet_id = row["id"].strip()
        registration_id = f"{REGISTRATION_ID_PREFIX}{simplymeet_id}"
        if store.find_by_id(registration_id) is not None:
            report.skipped_already_imported += 1
            continue

        if (row.get("Other participants") or "").strip():
            report.rows_with_other_participants += 1

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

        report.planned.append(ImportPlan(
            simplymeet_id=simplymeet_id,
            email=email,
            name=(row.get("Client") or "").strip(),
            course_shortname=course.shortname,
            occurrence_date=occurrence_iso,
            status=status,
            registered_at=f"{occurrence_iso}T00:00:00",
            canceled_at=canceled_at,
            canceled_by=canceled_by,
        ))
    return report


def run_migration(plans: list[ImportPlan], store: Store) -> int:
    """Actually writes: find-or-create each user by email (never overwriting
    an existing user's name if the account already exists -- only a
    brand-new account gets its name from the SimplyMeet.me row), then
    Store.import_historical_registration() per plan. Returns the number of
    registrations actually written (a plan can still be skipped here if a
    matching registration_id appeared between plan_import() and now --
    import_historical_registration() re-checks; see its own docstring)."""
    written = 0
    for plan in plans:
        user = store.find_user_by_email(plan.email)
        if user is None:
            user = store.upsert_user_for_booking(plan.email, plan.name)
        created = store.import_historical_registration(
            registration_id=f"{REGISTRATION_ID_PREFIX}{plan.simplymeet_id}",
            course_shortname=plan.course_shortname,
            occurrence_date=plan.occurrence_date,
            user_id=user.user_id,
            status=plan.status,
            registered_at=plan.registered_at,
            canceled_at=plan.canceled_at,
            canceled_by=plan.canceled_by,
        )
        if created:
            written += 1
    return written
