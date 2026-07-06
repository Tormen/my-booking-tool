"""Logic behind `my-bt list`'s --upcoming/--past filtering (scripts/my-bt)
-- deliberately NOT in that script, for the same reason app/cli_history.py
isn't: scripts/my-bt has no .py extension and lives outside `app/`, so
unittest can't import it directly. See tests/test_cli_list.py.
"""
from __future__ import annotations

from datetime import date


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
