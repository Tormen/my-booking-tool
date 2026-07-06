"""Logic behind `my-bt cancel` (scripts/my-bt) -- deliberately NOT in that
script, for the same reason app/cli_history.py isn't: scripts/my-bt has no
.py extension and lives outside `app/`, so unittest can't import it
directly. Anything here beyond trivial argument parsing belongs in this
module so it's unit-tested the normal way (see tests/test_cli_cancel.py).

`my-bt cancel --registration-id ...` is the CLI equivalent of the web
admin's /admin/cancel/<reg_id> (see app/webapp.py::App.admin_cancel): same
identification (by registration_id, via Store.find_by_id), same status
transition (Store.cancel(..., canceled_by="host", ...)), same optional
message stored in host_message, and the SAME cancellation emails to both
the participant and admin_email -- via app.cancellation.send_cancellation_emails,
not a reimplementation of it. The one thing this CLI command does NOT do
that admin_cancel() does is promote the next waitlisted person / re-sync
the calendar (App._cancel_and_promote) -- that needs a live CalDAV
connection, which this CLI intentionally has no dependency on (same
reasoning as `my-bt erase`'s own docstring). Run the app's normal cancel
flow from the web admin, or restart the service (which re-syncs lazily),
if the calendar needs to reflect this cancellation immediately.
"""
from __future__ import annotations

from dataclasses import dataclass

from .cancellation import send_cancellation_emails
from .config import Settings
from .storage import STATUS_CONFIRMED, STATUS_WAITLISTED, Store


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
    of sync with what the web admin path sends.

    Returns a CancelResult with ok=False (no exception, no side effects)
    if:
      - registration_id doesn't exist at all, or
      - it exists but isn't confirmed/waitlisted (already canceled, or a
        stale pending_confirmation row) -- Store.cancel() itself is a
        no-op in that case, so this checks the status up front to give a
        clear reason instead of silently "succeeding" at nothing.

    Does NOT promote the next waitlisted person or re-sync the calendar
    (see this module's own docstring) -- callers needing that should use
    the web admin's /admin/cancel instead.
    """
    reg = store.find_by_id(registration_id)
    if reg is None:
        return CancelResult(ok=False, reason="no registration with that id", registration_id=registration_id)
    if reg.status not in (STATUS_CONFIRMED, STATUS_WAITLISTED):
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

    changed = store.cancel(registration_id, canceled_by="host", host_message=message)
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
        # Same "both sides, always" notification as every other cancellation
        # path -- see send_cancellation_emails's own docstring.
        send_cancellation_emails(settings, course, reg.occurrence_date, user, canceled_by="host", message=message)
        emailed = True

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
