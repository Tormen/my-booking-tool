"""Logic behind `my-bt list`'s --upcoming/--past filtering, party-info
annotation, (2026-07-13) its default clean/readable view, and (2026-07-13)
git-style short registration ids (scripts/my-bt) -- deliberately NOT in that
script, for the same reason app/cli_history.py isn't: scripts/my-bt has no
.py extension and lives outside `app/`, so unittest can't import it
directly. See tests/test_cli_list.py.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, datetime

from .erasure import find_archived_user_ids_for_email
from .security import is_erased_email
from .storage import format_display_timestamp, status_label


def _format_display_date(iso_str: str) -> str:
    """Date-only rendering of a now_iso()-produced (or any ISO-8601)
    timestamp -- YYYY-MM-DD, no time-of-day, ever. 2026-07-08, the operator,
    looking at `my-bt users`'s joined/last_login columns: "please only
    use YYYY-MM-DD for the columns and only with -V show also the
    timestamp" -- unlike format_display_timestamp() (which still shows
    the time-of-day when it isn't exactly midnight), this ALWAYS drops
    it; the full timestamp is only ever shown when the caller explicitly
    asks for verbose output (see build_clean_user_view's own `verbose`
    param). Falls back to the raw string unchanged if it isn't valid
    ISO-8601, same as format_display_timestamp()."""
    if not iso_str:
        return iso_str
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d")
    except ValueError:
        return iso_str


def compute_last_confirmed_course(rows: list[dict], today: date) -> dict[str, str]:
    """For each user_id, the course_shortname of their most recent
    CONFIRMED registration with occurrence_date today-or-earlier ("today
    counts as already happened", same convention as
    compute_times_booked_counts/app.cli_stats.compute_last_and_next_
    slot). 2026-07-08, the operator: "please have name joined last_login
    last_course email ... last_course = last confirmed course" -- a new
    column on `my-bt users`'s clean view.

    `rows` should be ALL registrations regardless of the users list's
    own --live/--archive/--all scope (e.g. Store.read_registrations(
    scope="all")) -- an archived (erased) user's own row still belongs to
    that same user_id, so their last_course should still resolve.

    A user_id with no qualifying (confirmed, today-or-earlier) row at
    all is simply absent from the result -- caller treats that as a
    blank column, same as every other "nothing to show" case here."""
    best_date_by_user: dict[str, str] = {}
    course_by_user: dict[str, str] = {}
    today_iso = today.isoformat()
    for r in rows:
        if r["status"] != "confirmed":
            continue
        occ = r["occurrence_date"]
        if occ > today_iso:
            continue
        if occ >= best_date_by_user.get(r["user_id"], ""):
            best_date_by_user[r["user_id"]] = occ
            course_by_user[r["user_id"]] = r["course_shortname"]
    return course_by_user

# 2026-07-13, the operator: "would it be possible that my-bt lists a short-id that
# can also be used with my-bt cancel to cancel a booking? a bit like what
# git does with its commit ids ... please add this shortened ID to my-bt
# list (not to the web interface as there we have the cancel button)."
#
# 2026-07-08, the operator, looking at a real `my-bt list` full of ~23-char
# "short" ids: "is there no way to have a shorter 'shorter ID'? ... I
# said like git and there they have 6 chars I believe to reference the
# commit". Root cause of the 23 chars: a live-registration universe isn't
# ALWAYS fresh uuid4s -- migrated SimplyMeet.me registrations get a
# deterministic registration_id, "simplymeet-<numeric id>" (see
# app/migrate_simplymeet.py's REGISTRATION_ID_PREFIX), so hundreds of
# them share the same long literal prefix (plus often-similar leading
# digits in the numeric suffix too). The OLD scheme took a literal
# prefix of the id itself, so it had to grow well past 8 chars just to
# get past "simplymeet" and then past shared leading digits -- for a
# uuid4 that's still "effectively collision-free" at 8 chars (~122 bits
# of entropy), but that guarantee silently didn't hold for these
# deterministic ids at all.
#
# Fixed by hashing the full id first (see _short_id_digest below) and
# taking a prefix of THAT instead -- every character carries real,
# uniform entropy this way regardless of what the underlying id looks
# like, so 6 hex chars (~24 bits, matching git's own default abbreviation
# length) is fine at this app's scale (a few hundred live registrations
# at most): assign_short_ids's own collision-growth loop still extends it
# for real if that scale ever changes enough to make 6 risky.
SHORT_ID_LENGTH = 6


def _short_id_digest(full_id: str) -> str:
    """Turns any registration_id -- a random uuid4 OR a deterministic,
    shared-prefix id like SimplyMeet-imported rows' "simplymeet-<n>" --
    into a fixed-length hex string where every character is uniformly
    likely, so a short PREFIX of this digest is genuinely collision-
    resistant no matter what the real id's own structure looks like. Not
    a security boundary (sha1, not keyed) -- purely for even, compact
    display-only short ids; see assign_short_ids/resolve_short_id, the
    only two callers."""
    return hashlib.sha1(full_id.encode("utf-8")).hexdigest()


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
    short_ids_by_reg_id: dict[str, str] | None = None, verbose: bool = False,
) -> list[dict]:
    """Builds the compact, human-readable rows `my-bt list` shows by
    default (2026-07-13, the operator: "the default command shows it cleaned up
    without technical ids... can you mimick the view of the
    web-interface?") -- Date, Id, Status, Course, Name, Email, Times
    booked, plus Registered/Guests when `verbose` (see below). No
    registration_id/user_id/party_id/invited_by_user_id/token hashes --
    pass -r/--raw for the full raw CSV-column view instead (see
    scripts/my-bt's cmd_list).

    2026-07-08, the operator: "put [date] as first column" -- "date" leads every
    row (it was 4th, after id/status/course); "Please don't show when
    they registered by default but only with my-bt list -V" -- the
    "registered" column (registration timestamp) is now OMITTED unless
    `verbose=True` (scripts/my-bt's cmd_list wires this to -V/--verbose,
    the same "adds more detail on top of the summary" axis already used
    by `gdpr-retention -V` and the old `status -V` -- a separate axis
    from -r/--raw, which swaps the shape rather than adding to it; see
    raw_arg()'s own comment in scripts/my-bt). `rows` is already sorted
    by occurrence_date by the caller (cmd_list) before this runs, so the
    added "date first" column reads top-to-bottom in the order it's
    sorted by.

    2026-07-08, same day, the operator looking at a fresh `list` output: "the
    'guests' here is additional fluff as any guest will have their own
    line here, correct? if yes, then please also only show with -V" --
    correct: every party member (leader AND each guest they bring) is
    already its own row in `rows` (one registration = one row), so the
    "guests"/party_label column is a convenience summary of something
    that's already fully visible across the other rows, same spirit as
    "registered". Now gated behind the SAME `verbose` flag as
    "registered", not a separate one.

    ONE exception to "no ids" (2026-07-13, same day, the operator's very next
    message): a leading -- well, now second -- "id" column with a short,
    git-style abbreviated registration_id (see assign_short_ids), usable
    with `my-bt cancel` -- "a bit like what git does with its commit
    ids". Deliberately CLI-only, never shown on the web (the operator: "not to
    the web interface as there we have the cancel button") -- pass
    `short_ids_by_reg_id` (see scripts/my-bt's cmd_list for how it's
    built, from the LIVE registration_id universe only) to populate it;
    omit/None leaves every row's "id" blank (e.g. an archived row, which
    was never live and so was never assigned one -- can't be `cancel`ed
    by id anyway).

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
        row = {
            "date": r["occurrence_date"],
            "id": short_ids_by_reg_id.get(r["registration_id"], ""),
            "status": status_label(r["status"]),
            "course": r["course_shortname"],
            "name": name,
            "email": email,
        }
        if verbose:
            row["registered"] = format_display_timestamp(r.get("registered_at", ""))
        row["times_booked"] = f"{upto_now_by_user.get(uid, 0)}/{total_by_user.get(uid, 0)}"
        if verbose:
            row["guests"] = r["party_label"]
        out.append(row)
    return out


def build_clean_user_view(
    rows: list[dict], last_course_by_user: dict[str, str] | None = None, verbose: bool = False,
) -> list[dict]:
    """Builds the compact, human-readable rows `my-bt users` shows by
    default (2026-07-13, same "clean by default, -r/--raw for the full
    table" request as `my-bt list` above) -- name, when they joined, when
    they last logged in, their last confirmed course, and email (in that
    order), with no user_id or any of the token/hash columns
    (password_hash/salt, confirm_token_hash, pending_email_*, ... -- see
    User's own dataclass in app/storage.py for the full raw list).
    pin_hash/pin_salt were already stripped upstream by cmd_users before
    this ever runs, same as -r/--raw still does.

    2026-07-08, the operator, re-ordering + two more changes in the same
    message:
    - "please have name joined last_login last_course email" -- email
      moves from 2nd to LAST; new "last_course" column added (see
      compute_last_confirmed_course -- pass its result as
      `last_course_by_user`; a user_id absent from that dict, e.g. no
      confirmed history yet, gets a blank cell).
    - "please only make email as wide as needed!" -- root cause of the
      column being far wider than any real email: an ERASED (archived)
      user's "email" field is a long keyed HMAC hash (see
      app/security.py's hash_email_for_erasure, "erased:<64 hex chars>"),
      shown here in full. Detected via is_erased_email() and rendered as
      "[erased]" instead -- matching "name"'s own existing "[erased]"
      placeholder for the same rows -- so the column is only ever as wide
      as a real display value needs.
    - "please only use YYYY-MM-DD for the columns and only with -V show
      also the timestamp" -- joined/last_login are date-only
      (_format_display_date) unless `verbose=True` (scripts/my-bt's
      cmd_users wires this to a new -V/--verbose flag, same "more detail
      on top of the summary" axis as `list -V`/`gdpr-retention -V`), in
      which case the full format_display_timestamp() rendering (date, or
      date_HHMM.SS when there's a real time-of-day) is used instead."""
    last_course_by_user = last_course_by_user or {}
    fmt = format_display_timestamp if verbose else _format_display_date
    out = []
    for r in rows:
        email = r.get("email", "")
        if is_erased_email(email):
            email = "[erased]"
        out.append({
            "name": r.get("name", ""),
            "joined": fmt(r.get("created_at", "")),
            "last_login": fmt(r.get("last_login_at", "")) or "(never)",
            "last_course": last_course_by_user.get(r.get("user_id", ""), ""),
            "email": email,
        })
    return out


def assign_short_ids(
    full_ids: list[str], min_length: int = SHORT_ID_LENGTH, digest_fn=_short_id_digest,
) -> dict[str, str]:
    """Git-style short ids for registration_id: a `min_length`-char prefix
    of `digest_fn(full_id)` (see _short_id_digest -- NOT a literal prefix
    of the id itself, see that function's docstring for why). If that
    length collides for ANY two ids in `full_ids` (checked for real, not
    assumed away), the length grows by one for the WHOLE set and is
    rechecked, exactly like `git log --abbrev-commit` picking one uniform
    length rather than a different one per commit.

    `digest_fn` defaults to the real hash and is only ever overridden by
    tests (e.g. an identity function to test the growth mechanism in
    isolation against hand-picked strings, since real sha1 output can't
    be hand-crafted to collide).

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
    digests = {fid: digest_fn(fid) for fid in full_ids}
    length = min_length
    max_len = max((len(s) for s in digests.values()), default=0)
    while length < max_len:
        prefixes: dict[str, int] = {}
        for s in digests.values():
            prefixes[s[:length]] = prefixes.get(s[:length], 0) + 1
        if all(count == 1 for count in prefixes.values()):
            break
        length += 1
    return {fid: s[:length] for fid, s in digests.items()}


def resolve_short_id(
    short: str, full_ids: list[str], digest_fn=_short_id_digest,
) -> tuple[str | None, list[str]]:
    """Resolves a short (or full) id typed by the operator against the LIVE
    registration_id universe (`full_ids` -- see `my-bt cancel`'s own
    caller for why it's live-only: Store.find_by_id/cancel() only ever
    operate on live rows anyway). Matches by prefix against
    `digest_fn(fid)` (case-insensitive), so ANY unambiguous prefix length
    works, not just exactly SHORT_ID_LENGTH characters -- same
    flexibility `git show <abbrev>` gives, and forgiving of the id having
    grown longer since it was displayed (see assign_short_ids's own
    docstring on when that happens). Also accepts the literal FULL
    registration_id (dashes optional, case-insensitive) as a direct
    fallback match, for anyone pasting it from a raw CSV/API response
    rather than from `my-bt list`'s own short id column.

    Returns (full_id, []) on exactly one match, (None, []) on no match,
    (None, matches) with 2+ candidates on an ambiguous prefix -- three
    distinct outcomes `my-bt cancel` reports differently (not found vs.
    "be more specific")."""
    needle = short.replace("-", "").lower()
    matches = [
        fid for fid in full_ids
        if digest_fn(fid).startswith(needle) or fid.replace("-", "").lower() == needle
    ]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def merge_archived_for_display(store, settings, live_users: list[dict], archived_regs: list[dict]) -> list[dict]:
    """Read-only equivalent of `my-bt admin dearchive`/Store.
    merge_archived_registrations: for each LIVE user, finds any archived
    (erased) identity sharing their email hash (find_archived_user_ids_
    for_email) and re-labels those archived rows with the LIVE user_id --
    WITHOUT writing anything to disk.

    2026-07-13, the operator: "/admin should [be] non-mutating" -- both `my-bt
    list --all`/`--past` and app/webapp.py's admin_overview() call this
    instead of the previous behavior (silently rewriting the CSVs on
    every page/command load). `dearchive` remains the one deliberate,
    explicit action that actually persists a merge.

    Same duplicate-avoidance rule as Store.merge_archived_registrations
    (2026-07-10, the operator's own bug report): an archived row is DROPPED
    entirely -- not relabeled, not shown at all -- if the live account it
    would relabel onto already has its own live row for that exact
    (course_shortname, occurrence_date). Showing both would look like two
    bookings for a session that only really happened once.

    Returns a NEW list of archived-row dicts -- unmatched rows (genuinely
    orphaned pre-erasure history with no live rebook yet) come back
    unchanged, still under their old archived user_id."""
    live_user_id_by_archived_id: dict[str, str] = {}
    for u in live_users:
        for archived_id in find_archived_user_ids_for_email(store, settings, u["email"]):
            live_user_id_by_archived_id[archived_id] = u["user_id"]

    live_occurrences_by_user: dict[str, set] = {}
    for r in store.read_registrations(scope="live"):
        live_occurrences_by_user.setdefault(r["user_id"], set()).add((r["course_shortname"], r["occurrence_date"]))

    out = []
    for r in archived_regs:
        live_id = live_user_id_by_archived_id.get(r["user_id"])
        if not live_id:
            out.append(r)
            continue
        if (r["course_shortname"], r["occurrence_date"]) in live_occurrences_by_user.get(live_id, set()):
            continue
        out.append({**r, "user_id": live_id})
    return out
