"""Logic behind `my-bt list`'s --upcoming/--past filtering and party-info
annotation (scripts/my-bt) -- deliberately NOT in that script, for the same
reason app/cli_history.py isn't: scripts/my-bt has no .py extension and
lives outside `app/`, so unittest can't import it directly. See
tests/test_cli_list.py.
"""
from __future__ import annotations

from datetime import date


def annotate_party_info(rows: list[dict], users_by_id: dict[str, dict]) -> list[dict]:
    """Adds a human-readable "party" column to each registration row dict
    (from Store.read_registrations) -- "+N guest(s)" on the leader's own
    row, "guest of <email>" on each guest's row, "" for an ordinary solo
    booking (blank party_id). Same computation app/webapp.py's
    admin_overview() does for its own Party column (see Registration's own
    docstring in app/storage.py for what party_id/invited_by_user_id
    record) -- kept here instead of duplicated inline in scripts/my-bt so
    `my-bt list`/`show`/`history` all show it identically.

    `users_by_id` maps user_id -> raw user dict (e.g. from
    `Store.read_users(scope="all")` keyed by "user_id") -- a plain dict
    rather than a `User` object since this is meant to work directly on
    the raw CSV rows `my-bt` already reads, without requiring a second,
    separate load of typed `User`/`Registration` objects.

    Returns NEW dicts (copies) -- never mutates `rows` in place, so a
    caller that still needs the original rows (e.g. to re-filter) isn't
    surprised by an extra key appearing on them."""
    party_members: dict[str, list[dict]] = {}
    for r in rows:
        pid = r.get("party_id")
        if pid:
            party_members.setdefault(pid, []).append(r)

    out = []
    for r in rows:
        party = ""
        invited_by = r.get("invited_by_user_id")
        pid = r.get("party_id")
        if invited_by:
            leader = users_by_id.get(invited_by)
            party = f"guest of {leader['email']}" if leader else f"guest of {invited_by}"
        elif pid:
            others = {
                m.get("user_id") for m in party_members.get(pid, [])
                if m.get("user_id") != r.get("user_id")
            }
            if others:
                party = f"+{len(others)} guest{'s' if len(others) != 1 else ''}"
        out.append({**r, "party": party})
    return out


def filter_by_date(rows: list[dict], upcoming: bool, past: bool, today: date | None = None) -> list[dict]:
    """Filters `rows` (dicts with an "occurrence_date" ISO-date key, e.g.
    from Store.read_registrations) by whether occurrence_date is
    today-or-later ("upcoming") or strictly before today ("past").

    At most one of `upcoming`/`past` should be True at a time -- enforced
    by scripts/my-bt's mutually-exclusive argparse group, not here. Neither
    True (the default when no flag is passed) returns `rows` completely
    unfiltered, preserving `my-bt list`'s behavior from before this filter
    existed.

    Same comparison app/webapp.py's admin_overview() uses for its own
    today+future default view: `date.fromisoformat(row["occurrence_date"])
    >= today`.
    """
    if not upcoming and not past:
        return rows
    today = today or date.today()
    if upcoming:
        return [r for r in rows if date.fromisoformat(r["occurrence_date"]) >= today]
    return [r for r in rows if date.fromisoformat(r["occurrence_date"]) < today]
