"""Logic behind `my-bt list`'s --upcoming/--past filtering, party-info
annotation, (2026-07-13) its default clean/readable view, and (2026-07-13)
git-style short registration ids (scripts/my-bt) -- deliberately NOT in that
script, for the same reason app/cli_history.py isn't: scripts/my-bt has no
.py extension and lives outside `app/`, so unittest can't import it
directly. See tests/test_cli_list.py.
"""
from __future__ import annotations

from collections import Counter
from datetime import date

from .storage import format_display_timestamp, status_label

# 2026-07-13, the operator: "would it be possible that my-bt lists a short-id that
# can also be used with my-bt cancel to cancel a booking? a bit like what
# git does with its commit ids ... please add this shortened ID to my-bt
# list (not to the web interface as there we have the cancel button)."
# 8 hex chars of a uuid4's ~122 bits of entropy is effectively collision-free
# at this app's scale (a handful of live registrations at a time) -- see
# assign_short_ids's own docstring for what happens on the (practically
# never) actual collision.
SHORT_ID_LENGTH = 8


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


def annotate_admin_party_label(rows: list[dict], users_by_id: dict[str, dict]) -> list[dict]:
    """Adds a "party_label" column matching app/webapp.py's admin_overview()
    Guests column EXACTLY: "" for a solo booking, "Guest of <name>" (falling
    back to the leader's email if their name is the "Guest" placeholder --
    2026-07-08, the operator: "Is guest of Guest correct??" -- technically yes, but
    unreadable, see admin_overview()'s own comment on this) on a guest's own
    row, "Host (+N guest(s))" on the leader's row.

    NOT the same as annotate_party_info() above -- that one is a separate,
    older, lowercase/email-only format ("guest of x@y.com" / "+N guest(s)")
    used by `my-bt show`/`history`'s existing output, kept as-is so those
    don't change shape. This one exists specifically so `my-bt list`'s
    2026-07-13 clean default view can show the IDENTICAL column /admin's
    own web table shows (2026-07-13, the operator: "for my-bt list: can you
    mimick the view of the web-interface?") -- extracted from
    admin_overview()'s own inline version of this same logic so the two
    can never drift apart; admin_overview() now calls this too instead of
    keeping a second copy.

    `rows` needs "party_id"/"invited_by_user_id"/"user_id" keys (any
    Store.read_registrations row already has these); `users_by_id` needs
    "name"/"email" per user (any Store.read_users row already has these)."""
    party_members: dict[str, list[dict]] = {}
    for r in rows:
        pid = r.get("party_id")
        if pid:
            party_members.setdefault(pid, []).append(r)

    out = []
    for r in rows:
        label = ""
        invited_by = r.get("invited_by_user_id")
        pid = r.get("party_id")
        if invited_by:
            leader = users_by_id.get(invited_by)
            if leader is None:
                label = "Guest of (unknown)"
            else:
                name = leader.get("name") or ""
                label = f"Guest of {name if name and name != 'Guest' else leader.get('email', '')}"
        elif pid:
            others = {
                m.get("user_id") for m in party_members.get(pid, [])
                if m.get("user_id") != r.get("user_id")
            }
            if others:
                n = len(others)
                label = f"Host (+{n} guest{'s' if n != 1 else ''})"
        out.append({**r, "party_label": label})
    return out


def compute_times_booked_counts(rows: list[dict], today: date) -> tuple[Counter, Counter]:
    """Returns (total_by_user, upto_now_by_user), both Counter[user_id].
    total_by_user counts EVERY registration ever made by that user_id (any
    status, any date); upto_now_by_user restricts to occurrence_date <=
    today. Same computation, same "N/total" up-to-now-over-total framing,
    app/webapp.py's admin_overview() uses for its own Times booked column
    (2026-07-08, the operator: "actually even better: make it 2/9 ... so that I
    see the total and also see the current time they joined") -- `rows`
    should be ALL registrations (live + archived, e.g.
    Store.read_registrations(scope="all")), same as admin_overview()'s own
    `all_regs`, so an erased-but-never-rebooked guest's true historical
    count isn't silently dropped."""
    total_by_user = Counter(r["user_id"] for r in rows)
    upto_now_by_user = Counter(
        r["user_id"] for r in rows if date.fromisoformat(r["occurrence_date"]) <= today
    )
    return total_by_user, upto_now_by_user


def build_clean_registration_view(
    rows: list[dict], users_by_id: dict[str, dict], all_rows: list[dict], today: date,
    short_ids_by_reg_id: dict[str, str] | None = None,
) -> list[dict]:
    """Builds the compact, human-readable rows `my-bt list` shows by
    default (2026-07-13, the operator: "the default command shows it cleaned up
    without technical ids... can you mimick the view of the
    web-interface?") -- the exact same columns /admin's own table shows:
    Status, Course, Date, Name, Email, Registered, Times booked, Guests.
    No registration_id/user_id/party_id/invited_by_user_id/token hashes --
    pass -r/--raw for the full raw CSV-column view instead (see
    scripts/my-bt's cmd_list).

    ONE exception to "no ids" (2026-07-13, same day, the operator's very next
    message): a leading "id" column with a short, git-style abbreviated
    registration_id (see assign_short_ids), usable with `my-bt cancel` --
    "a bit like what git does with its commit ids". Deliberately CLI-only,
    never shown on the web (the operator: "not to the web interface as there we
    have the cancel button") -- pass `short_ids_by_reg_id` (see
    scripts/my-bt's cmd_list for how it's built, from the LIVE
    registration_id universe only) to populate it; omit/None leaves every
    row's "id" blank (e.g. an archived row, which was never live and so
    was never assigned one -- can't be `cancel`ed by id anyway).

    `rows` is the (already filtered/sorted) set to display; `all_rows`
    is the FULL unfiltered set (live + archived, no --course/--status/
    etc. narrowing) that Times booked's totals must be computed over --
    same "count everything, display only what's asked for" split
    admin_overview() itself uses (its `all_regs` vs. the possibly-
    narrower `regs` it renders)."""
    total_by_user, upto_now_by_user = compute_times_booked_counts(all_rows, today)
    labeled = annotate_admin_party_label(rows, users_by_id)
    short_ids_by_reg_id = short_ids_by_reg_id or {}
    out = []
    for r in labeled:
        user = users_by_id.get(r["user_id"])
        name = user["name"] if user else "(unknown)"
        email = user["email"] if user else "(unknown)"
        uid = r["user_id"]
        out.append({
            "id": short_ids_by_reg_id.get(r["registration_id"], ""),
            "status": status_label(r["status"]),
            "course": r["course_shortname"],
            "date": r["occurrence_date"],
            "name": name,
            "email": email,
            "registered": format_display_timestamp(r.get("registered_at", "")),
            "times_booked": f"{upto_now_by_user.get(uid, 0)}/{total_by_user.get(uid, 0)}",
            "guests": r["party_label"],
        })
    return out


def build_clean_user_view(rows: list[dict]) -> list[dict]:
    """Builds the compact, human-readable rows `my-bt users` shows by
    default (2026-07-13, same "clean by default, -r/--raw for the full
    table" request as `my-bt list` above) -- name, email, when they
    joined, and when they last logged in, with no user_id or any of the
    token/hash columns (password_hash/salt, confirm_token_hash,
    pending_email_*, ... -- see User's own dataclass in app/storage.py for
    the full raw list). pin_hash/pin_salt were already stripped upstream
    by cmd_users before this ever runs, same as -r/--raw still does."""
    out = []
    for r in rows:
        out.append({
            "name": r.get("name", ""),
            "email": r.get("email", ""),
            "joined": format_display_timestamp(r.get("created_at", "")),
            "last_login": format_display_timestamp(r.get("last_login_at", "")) or "(never)",
        })
    return out


def assign_short_ids(full_ids: list[str], min_length: int = SHORT_ID_LENGTH) -> dict[str, str]:
    """Git-style short ids for registration_id (a uuid4): a `min_length`-
    char prefix of the id with its dashes stripped, e.g.
    "a1b2c3d4-...-..." -> "a1b2c3d4". If that length collides for ANY two
    ids in `full_ids` (astronomically unlikely at this app's scale -- see
    SHORT_ID_LENGTH's own comment -- but checked for real, not assumed
    away), the length grows by one for the WHOLE set and is rechecked,
    exactly like `git log --abbrev-commit` picking one uniform length
    rather than a different one per commit.

    Pure function of the CURRENT `full_ids` list -- recomputed fresh by
    every caller (`my-bt list`'s clean view, `my-bt cancel`'s short-id
    resolution) on every invocation, so the same full id gets the same
    short id across runs as long as the live registration set hasn't
    changed enough to introduce a new collision. Never actually consulted
    for correctness, though: resolve_short_id() below re-validates
    uniqueness at resolve time regardless of what was last displayed, so a
    stale/copy-pasted short id can never silently resolve to the WRONG
    row -- at worst it fails to resolve at all (not found / ambiguous) and
    `my-bt cancel` reports that plainly."""
    stripped = {fid: fid.replace("-", "") for fid in full_ids}
    length = min_length
    max_len = max((len(s) for s in stripped.values()), default=0)
    while length < max_len:
        prefixes: dict[str, int] = {}
        for s in stripped.values():
            prefixes[s[:length]] = prefixes.get(s[:length], 0) + 1
        if all(count == 1 for count in prefixes.values()):
            break
        length += 1
    return {fid: s[:length] for fid, s in stripped.items()}


def resolve_short_id(short: str, full_ids: list[str]) -> tuple[str | None, list[str]]:
    """Resolves a short (or even full) id typed by the operator against the
    LIVE registration_id universe (`full_ids` -- see `my-bt cancel`'s own
    caller for why it's live-only: Store.find_by_id/cancel() only ever
    operate on live rows anyway). Matches by prefix, dashes stripped on
    both sides, so ANY unambiguous prefix length works, not just exactly
    SHORT_ID_LENGTH characters -- same flexibility `git show <abbrev>`
    gives, and forgiving of the id having grown longer since it was
    displayed (see assign_short_ids's own docstring on when that happens).

    Returns (full_id, []) on exactly one match, (None, []) on no match,
    (None, matches) with 2+ candidates on an ambiguous prefix -- three
    distinct outcomes `my-bt cancel` reports differently (not found vs.
    "be more specific")."""
    needle = short.replace("-", "").lower()
    matches = [fid for fid in full_ids if fid.replace("-", "").lower().startswith(needle)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches
