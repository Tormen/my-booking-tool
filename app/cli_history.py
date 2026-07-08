"""Logic behind `my-bt admin dearchive` (scripts/my-bt, cmd_dearchive) --
deliberately NOT in that script, for the same reason app/cli_checks.py and
app/cli_setup.py aren't: scripts/my-bt has no .py extension and lives
outside `app/`, so unittest can't import it directly.

A guest who was erased (app/erasure.py, Store.erase_user) and later
re-books under the same email gets a brand-new live user_id, while their
pre-erasure registrations stay parked under the old, archived user_id --
`find_archived_user_ids_for_email` (app/erasure.py) is how `dearchive`
(and app/cli_list.py's own read-only merge_archived_for_display, and
app/webapp.py's admin_overview()) find that old identity from the live
email alone. `dearchive` (mutating) is the one that actually moves the
archived registrations onto the live user_id, via run_merge() below ->
Store.merge_archived_registrations() -- it never touches the archived
user row itself; that old, erased identity is never un-erased (see
Store.merge_archived_registrations's docstring for the exact boundary).

2026-07-13: `my-bt history` (read-only) and its build_history() were
dropped -- that functionality folded into `my-bt list --all`/`--past`
instead (see app/cli_list.py::merge_archived_for_display), which needed
a display-only (non-mutating) merge rather than a separate report. Only
the mutating half (`dearchive`/run_merge) still lives here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .storage import Store


@dataclass
class MergeResult:
    """What actually happened, for cmd_dearchive to report."""
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
