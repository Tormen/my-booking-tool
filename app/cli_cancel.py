"""Logic behind `my-bt cancel` (scripts/my-bt) -- deliberately NOT in that
script, for the same reason app/cli_checks.py isn't: scripts/my-bt has no
.py extension and lives outside `app/`, so unittest can't import it
directly. Anything here beyond trivial argument parsing belongs in this
module so it's unit-tested the normal way (see tests/test_cli_cancel.py).

`my-bt cancel --registration-id ...` is the CLI equivalent of the web
admin's /admin/cancel/<reg_id> (see app/webapp.py::App.admin_cancel): same
identification (by registration_id, via Store.find_by_id), same status
transition (Store.cancel(..., canceled_by="host", ...)), same optional
message stored in host_message, the SAME cancellation emails to both the
participant and admin_email -- via app.cancellation.send_cancellation_emails,
not a reimplementation of it -- and, since 2026-07-06, the SAME
promote-next-waitlisted + calendar re-sync as the web admin's cancel too,
via app.cancel_flow.cancel_and_promote (see that module's docstring). This
used to be a smaller subset (email only, no promotion, no calendar sync) --
that gap is closed now: this command has a real CalDAV network dependency,
same as every other cancel path, since a cancellation isn't actually
"the same" without it.
"""
from __future__ import annotations

from dataclasses import dataclass

from datetime import date

from . import calendar_sync
from .cancel_flow import CANCELABLE_STATUSES, build_caldav_client, cancel_and_promote
from .cancellation import send_cancellation_emails
from .cli_list import resolve_short_id
from .cli_show import find_course_by_shortname, looks_like_date, looks_like_registration_id
from .config import Settings
from .security import hash_token, new_token
from .storage import STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store


@dataclass
class CancelResult:
    """What actually happened, for cmd_cancel to report. `ok` is False (and
    every other field is None/default) when the registration_id doesn't
    exist or isn't in a cancelable status -- the caller should report that
    plainly and exit without error noise, not treat it as an exception."""
    ok: bool
    reason: str = ""
    registration_id: str = ""
    course_shortname: str = ""
    occurrence_date: str = ""
    status_before: str = ""
    user_name: str = ""
    user_email: str = ""
    message: str = ""
    emailed: bool = False


def cancel_registration(
    store: Store, settings: Settings, registration_id: str, message: str = ""
) -> CancelResult:
    """Cancels one LIVE registration by id, exactly like the web admin's
    /admin/cancel: Store.cancel(..., canceled_by="host", ...) (same status
    transition -> canceled_by_host, same host_message field), then emails
    both the participant and admin_email via
    app.cancellation.send_cancellation_emails -- the identical function
    app/webapp.py's App.admin_cancel calls, so the CLI can never drift out
    of sync with what the web admin path sends. Then runs the exact same
    app.cancel_flow.cancel_and_promote as admin_cancel() does: promotes the
    next waitlisted person (if this cancellation freed a confirmed spot)
    and re-syncs the calendar event -- this builds its own CalDAVClient
    (app.cancel_flow.build_caldav_client) since this CLI has no App
    instance/long-lived process to hold one on, so it needs a real
    network/CalDAV connection to complete, same as the web admin path does.

    Returns a CancelResult with ok=False (no exception, no side effects)
    if:
      - registration_id doesn't exist at all, or
      - it's already canceled -- Store.cancel() itself is a no-op in that
        case, so this checks the status up front to give a clear reason
        instead of silently "succeeding" at nothing.

    2026-07-13: also cancelable now if the row is still
    STATUS_PENDING_CONFIRMATION (a guest who registered but hasn't yet
    clicked their account-confirmation email link) -- previously excluded
    here, which meant that guest's booking couldn't be canceled by ANY
    path. See Store.cancel()'s own docstring for why this is safe (no
    promotion/calendar-sync consequence for a row that never held a real
    spot).
    """
    reg = store.find_by_id(registration_id)
    if reg is None:
        return CancelResult(ok=False, reason="no registration with that id", registration_id=registration_id)
    if reg.status not in (STATUS_CONFIRMED, STATUS_WAITLISTED, STATUS_PENDING_CONFIRMATION):
        return CancelResult(
            ok=False,
            reason=f"not cancelable (status is already {reg.status!r})",
            registration_id=registration_id,
            course_shortname=reg.course_shortname,
            occurrence_date=reg.occurrence_date,
            status_before=reg.status,
        )

    user = store.find_user_by_id(reg.user_id)
    course = settings.course(reg.course_shortname)

    # Freshly minted (2026-07-10) -- see Store.cancel()'s own
    # `reinstate_token_hash` docstring for why the ORIGINAL cancel token
    # can't be reused for this. Still minted/stored even though this is a
    # host-initiated cancel (canceled_by="host" below) and
    # send_cancellation_emails no longer puts a participant-facing
    # /reinstate/<token> link in a host-initiated cancel's email
    # (2026-07-09: host cancellations should NOT show a Reinstate link
    # to the participant) -- the operator's own unconditional
    # /host-reinstate/<registration_id> link (registration_id, not this
    # token) is what actually gets used to undo a `my-bt cancel`.
    reinstate_token = new_token()
    changed = store.cancel(
        registration_id, canceled_by="host", host_message=message,
        reinstate_token_hash=hash_token(reinstate_token),
    )
    if not changed:  # pragma: no cover - guarded by the status check above; belt-and-suspenders
        return CancelResult(
            ok=False,
            reason=f"not cancelable (status is already {reg.status!r})",
            registration_id=registration_id,
            course_shortname=reg.course_shortname,
            occurrence_date=reg.occurrence_date,
            status_before=reg.status,
        )

    emailed = False
    if course:
        # Same "both sides, always" notification (+ CANCEL .ics attachment
        # on the participant's copy) as every other cancellation path --
        # see send_cancellation_emails's own docstring.
        ics_filename, ics_text = calendar_sync.guest_cancel_ics(settings, course, date.fromisoformat(reg.occurrence_date))
        send_cancellation_emails(
            settings, course, reg.occurrence_date, user, canceled_by="host", message=message,
            registration_id=registration_id, reinstate_token=reinstate_token,
            ics_attachment=(ics_filename, ics_text, "CANCEL"),
        )
        emailed = True
        # Same promote-next-waitlisted + calendar re-sync as the web admin's
        # /admin/cancel (app/webapp.py::App.admin_cancel via
        # App._cancel_and_promote) -- see app.cancel_flow's own docstring.
        # Guarded by `if course:` (cancel_and_promote needs course.capacity)
        # same as the email above: a registration for a course shortname no
        # longer in settings.toml can still be canceled, it just can't be
        # promoted/synced without a course to promote/sync against.
        caldav = build_caldav_client(settings)
        cancel_and_promote(store, settings, caldav, reg.course_shortname, reg.occurrence_date)

    return CancelResult(
        ok=True,
        registration_id=registration_id,
        course_shortname=reg.course_shortname,
        occurrence_date=reg.occurrence_date,
        status_before=reg.status,
        user_name=user.name if user else "",
        user_email=user.email if user else "",
        message=message,
        emailed=emailed,
    )


def resolve_course_shortname_for_date(
    store: Store, occurrence_date: str, course_shortname: str | None = None,
) -> tuple[str | None, list[str]]:
    """`my-bt cancel --date ... [--course ...]`'s own course auto-detection
    (2026-07-13: --course is optional since a single date usually only
    holds a single course). If
    `course_shortname` is given, it's returned as-is (unvalidated -- the
    caller, cancel_flow.find_cancelable_registrations_for_occurrence, will
    simply find nothing to cancel if it's wrong, same as passing a bogus
    --course today).

    Otherwise, looks at every LIVE, still-cancelable registration
    (cancel_flow.CANCELABLE_STATUSES -- confirmed/waitlisted/pending-
    confirmation) on `occurrence_date` and collects the distinct
    course_shortnames among them:
      - exactly one -> (that shortname, [])
      - none at all -> (None, []) -- nothing to cancel, not an ambiguity
      - more than one -> (None, sorted list of every candidate) -- caller
        must ask the operator to pass --course explicitly.

    Returns (resolved_shortname_or_None, candidates). `candidates` is only
    ever non-empty in the ambiguous case -- a caller can tell "nothing to
    cancel" and "ambiguous, please disambiguate" apart by checking it."""
    if course_shortname:
        return course_shortname, []
    candidates = sorted({
        r["course_shortname"] for r in store.read_registrations(scope="live")
        if r["occurrence_date"] == occurrence_date and r["status"] in CANCELABLE_STATUSES
    })
    if len(candidates) == 1:
        return candidates[0], []
    if not candidates:
        return None, []
    return None, candidates


def classify_cancel_query(
    query: str, live_full_reg_ids: list[str], courses, min_id_length: int, digest_fn=None,
) -> tuple[str, object]:
    """Classifies one free-form `my-bt cancel <query>` positional -- 2026-
    07-09: `cancel` gained the same smart auto-detection `show` already
    has (short or long ID, a date, or a course name) -- same id/date
    auto-detection app.cli_show.classify_show_query
    gives `my-bt show`, reusing its exact same building blocks
    (looks_like_registration_id/looks_like_date/find_course_by_shortname,
    resolve_short_id), just narrowed to what `cancel` can actually ACT on:
    unlike `show` (a read-only lookup across live+archived), `cancel` is
    live-only and mutating, so this only ever searches LIVE registrations,
    and doesn't classify by name/email at all -- "cancel this person"
    isn't a specific enough target the way an id/date/occurrence is.

    Tried in this order, first match wins (mirrors classify_show_query's
    own precedence exactly, minus the user/email step it doesn't need
    here):
    1. Exact full registration_id match.
    2. A short-id hash-prefix match (only if `query` looks id-shaped --
       see looks_like_registration_id), against the LIVE registration set
       only (a short id is only ever assigned over live rows in the first
       place -- see app.cli_list.assign_short_ids's own docstring).
    3. An exact course shortname match -- NOT itself cancelable (a course
       alone has no specific occurrence), so the caller should explain
       that rather than silently failing; returned so it CAN explain it,
       instead of falling through to a generic "not recognized".
    4. A YYYY-MM-DD date.

    Returns (kind, data):
    - ("id", full_registration_id)
    - ("ambiguous_id", [candidate full ids])
    - ("course", Course) -- needs a --date to actually be cancelable
    - ("date", the query string, already validated YYYY-MM-DD)
    - ("none", None) -- not recognized as anything

    --id/--date/--course (see scripts/my-bt's own `cancel` subparser)
    still FORCE a specific type directly, bypassing this entirely, same
    as `show`'s --course/--user.

    `digest_fn`, if given, is passed straight through to resolve_short_id
    -- for tests only, same reason app.cli_list.assign_short_ids/
    resolve_short_id themselves accept it (real sha1 output can't be
    hand-crafted to collide on demand, so exercising the ambiguous-
    collision branch needs a trivial injectable digest instead)."""
    stripped_query = query.replace("-", "").strip().lower()
    exact_id_matches = [fid for fid in live_full_reg_ids if fid.replace("-", "").lower() == stripped_query]
    if len(exact_id_matches) == 1:
        return "id", exact_id_matches[0]

    if looks_like_registration_id(query, min_id_length):
        resolve_kwargs = {"digest_fn": digest_fn} if digest_fn is not None else {}
        resolved, candidates = resolve_short_id(query, live_full_reg_ids, **resolve_kwargs)
        if resolved:
            return "id", resolved
        if candidates:
            return "ambiguous_id", candidates

    course_matches = find_course_by_shortname(query, courses)
    if len(course_matches) == 1:
        return "course", course_matches[0]

    if looks_like_date(query):
        return "date", query.strip()

    return "none", None
