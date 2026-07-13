"""Logic behind `my-bt show`'s query classification. 2026-07-08:
generalized from "show one registration by id" (its original, narrower
shape) to auto-detecting which of FOUR entity types (booking, course,
course-occurrence, or user) a single free-form query means, e.g.
`my-bt show ada@example.com` or `my-bt show lux-fri-yoga` or `my-bt show
2026-07-10`. Same day, short ids were added to the set of things this
can resolve (the OLD `show` never did -- see cmd_show's own history in
scripts/my-bt, it only ever compared against the full registration_id,
despite its own -h text already claiming short ids worked).

Also same day: scripts/my-bt's cmd_show tries auto-detection (see
classify_show_query below) UNLESS --course/--user was passed, in which
case that type is used directly, no guessing -- avoids having to repeat
information already given via an explicit flag.

Deliberately NOT in scripts/my-bt, for the same reason as every other
app/cli_*.py module: that script has no .py extension and lives outside
`app/`, so unittest can't import it directly. See tests/test_cli_show.py.
"""
from __future__ import annotations

import re

from .cli_list import resolve_short_id

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def looks_like_registration_id(query: str, min_length: int) -> bool:
    """True if `query` is plausibly a registration id fragment: hex
    digits only (dashes ignored, case-insensitive) and at least
    `min_length` characters once dashes are stripped.

    2026-07-08: the free-text search covers email address, name,
    shortname, and date, but deliberately NOT registration ID -- id
    lookup and free-text search are two separate paths, not one fuzzy
    blend where a name might accidentally get mistaken for an id
    fragment (or vice versa). Requiring hex-only
    AND a minimum length keeps a short, all-hex name (e.g. "Ada", "Cafe"
    are technically valid hex) from ever being tried as an id at all --
    `min_length` should be the same SHORT_ID_LENGTH `my-bt list` displays
    ids at, so anything actually copy-pasted from that column is always
    long enough to qualify."""
    stripped = query.replace("-", "")
    return len(stripped) >= min_length and bool(_HEX_RE.match(stripped.lower()))


def looks_like_date(query: str) -> bool:
    """True if `query` is exactly YYYY-MM-DD -- deliberately strict (no
    partial-date substring matching like the web admin's own client-side
    filter does), so e.g. "2026" alone is treated as a name/email search
    term instead of silently matching every registration in that year."""
    return bool(_DATE_RE.match(query.strip()))


def find_course_by_shortname(query: str, courses) -> list:
    """Exact, case-insensitive match against each course's `shortname` --
    not a substring search, since shortnames are short, deliberately
    chosen identifiers (unlike names/emails, where a substring search
    genuinely helps)."""
    q = query.strip().lower()
    return [c for c in courses if c.shortname.lower() == q]


def find_users_by_name_or_email(query: str, users: list[dict]) -> list[dict]:
    """Case-insensitive SUBSTRING match against each user's name OR
    email -- same "type part of it" search the web admin's own
    client-side table filter offers, just server-side and restricted to
    these two fields (not every column), since those are the fields a
    user search is expected to match on."""
    q = query.strip().lower()
    return [u for u in users if q in u.get("email", "").lower() or q in u.get("name", "").lower()]


def find_registrations_on_date(query: str, rows: list[dict]) -> list[dict]:
    """Every registration row (any course, any status) whose
    occurrence_date exactly equals `query` (already validated as
    YYYY-MM-DD by looks_like_date before this is called)."""
    d = query.strip()
    return [r for r in rows if r["occurrence_date"] == d]


def classify_show_query(
    query: str,
    full_reg_ids: list[str],
    courses,
    users: list[dict],
    all_rows: list[dict],
    min_id_length: int,
) -> tuple[str, object]:
    """Classifies one free-form `my-bt show` query and returns (kind,
    data):

    - ("registration", full_registration_id)
    - ("ambiguous_registration", [candidate full ids])
    - ("course", Course)
    - ("occurrence", [rows on that date])
    - ("user", user_row)
    - ("ambiguous_user", [user_rows])
    - ("none", None) -- nothing matched anything at all

    Tried in this fixed order, first match wins (no further checks run
    once something matches -- e.g. an exact course shortname match is
    never second-guessed by also trying the date/user checks):
    1. Exact full registration_id match, any charset (uuid4 or a
       migrated "simplymeet-<n>" id) -- always tried, cheap, and
       unambiguous by construction (an exact string match against a
       supposedly-unique id column).
    2. A short-id hash-prefix match (see app.cli_list.resolve_short_id),
       but ONLY if `query` looks id-shaped (see
       looks_like_registration_id) -- so this is skipped entirely for
       ordinary search terms, never silently mis-firing on one.
    3. An exact course shortname match.
    4. If `query` looks like a YYYY-MM-DD date, every registration on
       that date (any course, any status).
    5. A name/email substring search across `users`.

    `full_reg_ids`/`all_rows` should be whatever universe the caller
    wants searched (scripts/my-bt's cmd_show passes live+archived
    combined, since `show` is a lookup tool, not a mutating action --
    unlike `my-bt cancel`, which is deliberately live-only)."""
    stripped_query = query.replace("-", "").strip().lower()
    exact_id_matches = [fid for fid in full_reg_ids if fid.replace("-", "").lower() == stripped_query]
    if len(exact_id_matches) == 1:
        return "registration", exact_id_matches[0]

    if looks_like_registration_id(query, min_id_length):
        resolved, candidates = resolve_short_id(query, full_reg_ids)
        if resolved:
            return "registration", resolved
        if candidates:
            return "ambiguous_registration", candidates

    course_matches = find_course_by_shortname(query, courses)
    if len(course_matches) == 1:
        return "course", course_matches[0]

    if looks_like_date(query):
        date_rows = find_registrations_on_date(query, all_rows)
        if date_rows:
            return "occurrence", date_rows

    user_matches = find_users_by_name_or_email(query, users)
    if len(user_matches) == 1:
        return "user", user_matches[0]
    if len(user_matches) > 1:
        return "ambiguous_user", user_matches

    return "none", None
