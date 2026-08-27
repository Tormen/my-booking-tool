"""The store behind GUI-managed exceptional dates (`[[course.date_override]]`
without settings.toml).

WHY A SEPARATE FILE AND NOT settings.toml: there is no TOML writer in the
stdlib, so writing overrides back would mean parse + re-serialise, which
destroys every comment in a file whose comments are deliberately
load-bearing; and the service parses settings.toml once at startup, so a
write there would need a restart to take effect. See the design notes in
the maintainer's local files for the full reasoning.

APPEND-ONLY, ONE ROW PER ACTION. Nothing is ever edited in place and
nothing is ever deleted: removing an override appends a `remove` row. The
file therefore IS the history of every exceptional date this deployment
ever had, which is the whole point of it existing separately.

ORIGIN IS LOAD-BEARING. A row's `origin` says where the entry came from:

  "admin"/"cli" -- somebody set it here. These rows DECIDE.
  "config"      -- mirrored from a [[course.date_override]] in
                   settings.toml. These rows are HISTORY ONLY and never
                   decide anything.

That asymmetry is what makes "delete it from settings.toml and it is
gone" work without any synchronisation: the effective set is computed as

    every [[course.date_override]] currently in settings.toml
  + every admin/cli entry whose last action is "set"   (these win a clash)

so an entry no longer in the file simply stops being in the set, whether
or not the log has caught up. `reconcile_config_rows()` appends the
matching set/remove rows to keep the HISTORY honest, and because effect
never depends on it, a skipped or failed reconciliation cannot change
what a guest sees.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# _LockedCsv is this package's own CSV primitive: flock, atomic write,
# 0640 + my-booking ownership, and a git commit of the data dir per
# write. Reaching for the private name rather than reimplementing any of
# that is deliberate -- a second, subtly-different CSV writer under
# /var/lib is exactly the kind of drift this project has been bitten by.
from .storage import _LockedCsv, _read_csv_plain

FILENAME = "date_overrides.csv"

FIELDNAMES = [
    "id",
    "created_at",
    "origin",
    "action",
    "course_shortname",
    "occurrence_date",
    "start_time",
    "duration_minutes",
    "message",
]

ORIGIN_CONFIG = "config"
ORIGIN_ADMIN = "admin"
ORIGIN_CLI = "cli"

ACTION_SET = "set"
ACTION_REMOVE = "remove"

# The origins whose rows decide what is actually in effect. `config` is
# deliberately absent -- see this module's own docstring.
_DECIDING_ORIGINS = (ORIGIN_ADMIN, ORIGIN_CLI)


@dataclass(frozen=True)
class OverrideEntry:
    """One effective override, as the rest of the app wants it: the same
    four fields app.config.CourseDateOverride carries, plus where it came
    from and when it was last set (for the console's own display)."""
    course_shortname: str
    date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM"
    duration_minutes: int | None
    message: str
    origin: str
    created_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(row: dict) -> tuple[str, str]:
    return (row.get("course_shortname", ""), row.get("occurrence_date", ""))


def _duration_of(row: dict) -> int | None:
    """`duration_minutes` is optional -- an empty cell means "keep the
    course's own duration", exactly like omitting the key in
    settings.toml. A non-numeric cell is treated the same way rather than
    raising: a corrupt cell must not take the booking page down."""
    raw = (row.get("duration_minutes") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class OverrideStore:
    """Read/append access to <data_dir>/date_overrides.csv.

    Reads never create the file: a deployment that has never set an
    override from the console has no such file, and that is not a state
    worth writing an empty file for (it also keeps `load_settings()`
    genuinely read-only -- see config.overrides_from_data_dir)."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILENAME

    # -- reading ---------------------------------------------------------

    def read_all(self) -> list[dict]:
        """Every row ever appended, in file order = chronological order.
        This is the history."""
        return _read_csv_plain(self.path, FIELDNAMES)

    def history_for(self, course_shortname: str, occurrence_date: str) -> list[dict]:
        """Every row for one (course, date), oldest first -- what the
        console shows when asked "what happened to this date?"."""
        return [
            r for r in self.read_all()
            if _key(r) == (course_shortname, occurrence_date)
        ]

    def effective(self) -> dict[tuple[str, str], OverrideEntry]:
        """The overrides that are IN EFFECT from this file: the last row
        per (course, date) among the deciding origins, kept only if that
        last action was "set".

        Rows with origin="config" are skipped ENTIRELY rather than merely
        losing a tie. If they were considered, the `remove` row that
        reconciliation appends when a block leaves settings.toml (which
        is exactly what happens when the console takes a date over, since
        the takeover comments that block out) would be the newest row for
        the key and would silently delete the admin entry that caused it.
        Config entries reach the effective set from settings.toml itself,
        never from here."""
        last: dict[tuple[str, str], dict] = {}
        for row in self.read_all():
            if row.get("origin") not in _DECIDING_ORIGINS:
                continue
            last[_key(row)] = row

        out: dict[tuple[str, str], OverrideEntry] = {}
        for key, row in last.items():
            if row.get("action") != ACTION_SET:
                continue
            course, date_str = key
            out[key] = OverrideEntry(
                course_shortname=course,
                date=date_str,
                start_time=row.get("start_time", ""),
                duration_minutes=_duration_of(row),
                message=row.get("message", ""),
                origin=row.get("origin", ""),
                created_at=row.get("created_at", ""),
            )
        return out

    def effective_config_mirror(self) -> dict[tuple[str, str], OverrideEntry]:
        """The same computation over origin="config" rows only -- i.e.
        what the HISTORY currently believes settings.toml contains. Used
        by reconcile_config_rows() to work out what changed; never used
        to decide anything a guest sees."""
        last: dict[tuple[str, str], dict] = {}
        for row in self.read_all():
            if row.get("origin") != ORIGIN_CONFIG:
                continue
            last[_key(row)] = row
        return {
            key: OverrideEntry(
                course_shortname=key[0],
                date=key[1],
                start_time=row.get("start_time", ""),
                duration_minutes=_duration_of(row),
                message=row.get("message", ""),
                origin=ORIGIN_CONFIG,
                created_at=row.get("created_at", ""),
            )
            for key, row in last.items()
            if row.get("action") == ACTION_SET
        }

    # -- writing ---------------------------------------------------------

    def append(
        self,
        *,
        origin: str,
        action: str,
        course_shortname: str,
        occurrence_date: str,
        start_time: str = "",
        duration_minutes: int | None = None,
        message: str = "",
    ) -> dict:
        """Append one row and return it. The whole file is rewritten (via
        _LockedCsv, so: exclusive lock, atomic replace, git commit) rather
        than opened in append mode -- at a few rows per year the cost is
        irrelevant, and it keeps every data-dir write going through the
        one audited path instead of introducing a second."""
        return self.append_many([{
            "origin": origin,
            "action": action,
            "course_shortname": course_shortname,
            "occurrence_date": occurrence_date,
            "start_time": start_time,
            "duration_minutes": "" if duration_minutes is None else str(duration_minutes),
            "message": message,
        }])[0]

    def append_many(self, new_rows: list[dict]) -> list[dict]:
        """Append several rows under ONE lock/write/commit -- what
        reconciliation needs, where a single settings.toml edit can add,
        change and remove entries at once. An empty list writes nothing
        at all (no lock, no commit, no git noise)."""
        if not new_rows:
            return []
        stamped = []
        for row in new_rows:
            complete = {k: "" for k in FIELDNAMES}
            complete.update(row)
            complete["id"] = complete["id"] or uuid.uuid4().hex
            complete["created_at"] = complete["created_at"] or _now_iso()
            stamped.append(complete)

        with _LockedCsv(self.path, FIELDNAMES) as (rows, write):
            write(rows + stamped, f"date_overrides: +{len(stamped)} row(s)")
        return stamped


def reconcile_config_rows(
    store: OverrideStore, config_entries: list[OverrideEntry]
) -> list[dict]:
    """Bring the origin="config" history in line with what settings.toml
    currently says, and return the rows appended (empty when nothing
    changed, which is the normal case on every start).

    `config_entries` is what settings.toml holds right now. Appends:
      - "set" for an entry the history has never seen, or whose values
        changed;
      - "remove" for an entry the history has but the file no longer does.

    HISTORY ONLY. Nothing here affects the effective set (see the module
    docstring), so callers may skip it freely -- and must, in read-only
    contexts: this is the one function in this module that writes as a
    side effect of merely reading settings.toml."""
    known = store.effective_config_mirror()
    current = {(e.course_shortname, e.date): e for e in config_entries}

    def same(a: OverrideEntry, b: OverrideEntry) -> bool:
        return (a.start_time, a.duration_minutes, a.message) == \
               (b.start_time, b.duration_minutes, b.message)

    to_append: list[dict] = []
    for key, entry in current.items():
        if key in known and same(known[key], entry):
            continue
        to_append.append({
            "origin": ORIGIN_CONFIG,
            "action": ACTION_SET,
            "course_shortname": entry.course_shortname,
            "occurrence_date": entry.date,
            "start_time": entry.start_time,
            "duration_minutes": "" if entry.duration_minutes is None else str(entry.duration_minutes),
            "message": entry.message,
        })
    for key in known:
        if key in current:
            continue
        to_append.append({
            "origin": ORIGIN_CONFIG,
            "action": ACTION_REMOVE,
            "course_shortname": key[0],
            "occurrence_date": key[1],
        })
    return store.append_many(to_append)
