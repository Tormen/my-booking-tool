"""Logic behind `my-bt stats`'s extended breakdown -- last/next occurrence
counts and year-over-year comparisons, per (course, status). Deliberately
NOT in scripts/my-bt, for the same reason as every other app/cli_*.py
module: that script has no .py extension and lives outside `app/`, so
unittest can't import it directly. See tests/test_cli_stats.py.

2026-07-08: extended `my-bt stats`'s original bare per-(course, status)
count table with a "total" column, last/next-occurrence counts ("last
slot"/"next slot"), and three year-based windows (last year, last year
to date, this year to date) -- each computed PER (course, status), the
same grouping the base table already used, rather than confirmed-only/
course-only in a separate table. The base table's own "count" column was
renamed to "total" to make room for these (done in scripts/my-bt's
cmd_stats, not here). Each year-based count also gets a "/xx" showing
the number of distinct participants in that period, since it can span
many occurrences the same guest might hold more than one registration
across; last/next slot (a single occurrence date, so count and
distinct-participant count are trivially the same number) stay a plain
count.
"""
from __future__ import annotations

from datetime import date


def compute_totals_with_distinct(rows: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    """For each (course_shortname, status) pair, returns (count, distinct
    user_id count) across all of `rows`. 2026-07-08: the base table's own
    "total" column gets the same "count/distinct" treatment as the
    year-over-year columns.

    `rows` is whatever the caller has already filtered (e.g. `my-bt
    stats`'s own --year/--scope) -- same scope the "total" column always
    had, just with a distinct-participant count added alongside it now."""
    counts: dict[tuple[str, str], int] = {}
    distinct: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        key = (r["course_shortname"], r["status"])
        counts[key] = counts.get(key, 0) + 1
        distinct.setdefault(key, set()).add(r["user_id"])
    return {key: (n, len(distinct[key])) for key, n in counts.items()}


def compute_last_and_next_slot(
    rows: list[dict], today: date | None = None,
) -> dict[tuple[str, str], dict[str, tuple[str, int] | None]]:
    """For each (course_shortname, status) pair, finds the most recent
    occurrence_date that's today-or-earlier ("last_slot") and the soonest
    occurrence_date strictly after today ("next_slot"), plus how many
    registrations of that exact status fall on that date.

    "Today counts as already happened" for last_slot -- same convention
    app/webapp.py's admin_overview() "Times booked" column already uses
    (occurrence_date <= today counts toward "up to now").

    Returns {(course, status): {"last_slot": (date_iso, count) | None,
    "next_slot": (date_iso, count) | None}} -- a (course, status)
    combination with zero rows is simply absent from the result."""
    today = today or date.today()
    by_key_date: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        key = (r["course_shortname"], r["status"])
        d = r["occurrence_date"]
        by_date = by_key_date.setdefault(key, {})
        by_date[d] = by_date.get(d, 0) + 1

    out: dict[tuple[str, str], dict[str, tuple[str, int] | None]] = {}
    for key, counts_by_date in by_key_date.items():
        past_or_today = sorted(d for d in counts_by_date if date.fromisoformat(d) <= today)
        future = sorted(d for d in counts_by_date if date.fromisoformat(d) > today)
        last_slot = (past_or_today[-1], counts_by_date[past_or_today[-1]]) if past_or_today else None
        next_slot = (future[0], counts_by_date[future[0]]) if future else None
        out[key] = {"last_slot": last_slot, "next_slot": next_slot}
    return out


def _year_windows(today: date) -> dict[str, tuple[date, date]]:
    """The three fixed calendar windows compute_year_period_stats()
    compares, relative to `today`:
    - "last_year": the whole prior calendar year (Jan 1 -- Dec 31)
    - "last_year_to_date": Jan 1 -- the same month/day as `today`, but in
      the prior year -- an apples-to-apples comparison against...
    - "this_year_to_date": Jan 1 -- `today`, in the current year

    Falls back to Feb 28 for last_year_to_date if `today` is a Feb 29
    (last year, not being a leap year, has no such date)."""
    try:
        last_year_to_date_end = today.replace(year=today.year - 1)
    except ValueError:
        last_year_to_date_end = today.replace(year=today.year - 1, day=28)
    return {
        "last_year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
        "last_year_to_date": (date(today.year - 1, 1, 1), last_year_to_date_end),
        "this_year_to_date": (date(today.year, 1, 1), today),
    }


def compute_year_period_stats(
    rows: list[dict], today: date | None = None,
) -> dict[tuple[str, str], dict[str, tuple[int, int]]]:
    """For each (course_shortname, status) pair, counts how many rows (and
    how many DISTINCT user_id among them) fall in each of the three
    _year_windows() windows.

    Returns {(course, status): {"last_year": (count, distinct), "last_
    year_to_date": (count, distinct), "this_year_to_date": (count,
    distinct)}} -- a (course, status, window) combination with zero rows
    is simply absent from that key's inner dict."""
    today = today or date.today()
    windows = _year_windows(today)

    counts: dict[tuple[str, str, str], int] = {}
    distinct: dict[tuple[str, str, str], set[str]] = {}
    for r in rows:
        occ = date.fromisoformat(r["occurrence_date"])
        key = (r["course_shortname"], r["status"])
        for label, (start, end) in windows.items():
            if start <= occ <= end:
                full_key = (*key, label)
                counts[full_key] = counts.get(full_key, 0) + 1
                distinct.setdefault(full_key, set()).add(r["user_id"])

    out: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
    for (course, status, label), n in counts.items():
        out.setdefault((course, status), {})[label] = (n, len(distinct[(course, status, label)]))
    return out
