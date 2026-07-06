"""Logic behind `my-bt history` and `my-bt merge` (scripts/my-bt) --
deliberately NOT in that script, for the same reason app/cli_checks.py and
app/cli_setup.py aren't: scripts/my-bt has no .py extension and lives
outside `app/`, so unittest can't import it directly. Anything here beyond
trivial argument parsing + a direct Store/erasure call belongs in this
module so it's unit-tested the normal way (see tests/test_cli_history.py).

Both commands are read/write variants of the same idea: a guest who was
erased (app/erasure.py, Store.erase_user) and later re-books under the
same email gets a brand-new live user_id, while their pre-erasure
registrations stay parked under the old, archived user_id --
`find_archived_user_ids_for_email` (app/erasure.py) is how both commands
(and app/webapp.py's admin_overview() display-only merge) find that old
identity from the live email alone.

- `history` (read-only) just reports the two sets of registrations and a
  combined count -- see build_history().
- `merge` (mutating) actually moves the archived registrations onto the
  live user_id via Store.merge_archived_registrations() -- see
  run_merge(). It never touches the archived user row itself; that old,
  erased identity is never un-erased (see Store.merge_archived_registrations's
  docstring for the exact boundary).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .erasure import find_archived_user_ids_for_email
from .storage import Store, User


@dataclass
class HistoryResult:
    """Everything `my-bt history --email ...` needs to print. `live_user`
    is None when there's no live account with this email at all (the
    caller should say so and exit non-error, not treat it as a failure --
    see cmd_history in scripts/my-bt)."""
    live_user: User | None
    live_registrations: list[dict]
    archived_user_ids: list[str]
    archived_registrations: list[dict]
    live_times_booked: int
    archived_times_booked: int

    @property
    def combined_times_booked(self) -> int:
        return self.live_times_booked + self.archived_times_booked


def build_history(store: Store, settings: Settings, email: str) -> HistoryResult:
    """Read-only. Mirrors app/webapp.py::admin_overview's display-only
    merge computation exactly (same find_archived_user_ids_for_email
    lookup, same "sum registrations by user_id" logic) so the CLI and the
    web admin overview never disagree about what these numbers mean."""
    live_user = store.find_user_by_email(email)
    archived_user_ids: list[str] = []
    archived_regs: list[dict] = []
    if live_user is not None:
        archived_user_ids = find_archived_user_ids_for_email(store, settings, live_user.email)
        if archived_user_ids:
            archived_regs = [
                r for r in store.read_registrations(scope="archived")
                if r["user_id"] in archived_user_ids
            ]

    live_regs = store.registrations_for_user(live_user.user_id) if live_user else []
    live_regs_as_dicts = [
        {
            "registration_id": r.registration_id,
            "course_shortname": r.course_shortname,
            "occurrence_date": r.occurrence_date,
            "status": r.status,
            # user_id/party_id/invited_by_user_id are included so a caller
            # (e.g. scripts/my-bt's cmd_history) can run these through
            # app.cli_list.annotate_party_info the same way `my-bt
            # list`/`show` do -- see Registration's own docstring in
            # app/storage.py for what party_id/invited_by_user_id record.
            # user_id itself isn't shown by cmd_history today, but
            # annotate_party_info requires it on every row to tell party
            # members apart from each other.
            "user_id": r.user_id,
            "party_id": r.party_id,
            "invited_by_user_id": r.invited_by_user_id,
        }
        for r in live_regs
    ]

    return HistoryResult(
        live_user=live_user,
        live_registrations=live_regs_as_dicts,
        archived_user_ids=archived_user_ids,
        archived_registrations=archived_regs,
        live_times_booked=len(live_regs_as_dicts),
        archived_times_booked=len(archived_regs),
    )


@dataclass
class MergeResult:
    """What actually happened, for cmd_merge to report."""
    moved_count: int
    moved_registrations: list[dict]


def run_merge(store: Store, archived_user_ids: list[str], into_user_id: str) -> MergeResult:
    """Thin wrapper around Store.merge_archived_registrations that also
    hands back which rows moved (course/date), so scripts/my-bt can print
    a useful summary instead of just a count. Reads the to-be-moved rows
    BEFORE calling the Store method (which removes them from the archived
    CSV), since afterwards they're already gone from there and now live in
    the live CSV under a different user_id."""
    would_move = [
        r for r in store.read_registrations(scope="archived")
        if r["user_id"] in archived_user_ids
    ]
    moved_count = store.merge_archived_registrations(archived_user_ids, into_user_id)
    return MergeResult(moved_count=moved_count, moved_registrations=would_move)
