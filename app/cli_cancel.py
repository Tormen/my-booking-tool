"""Logic behind `my-bt cancel` (scripts/my-bt) -- deliberately NOT in that
script, for the same reason app/cli_history.py isn't: scripts/my-bt has no
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
from .cancel_flow import build_caldav_client, cancel_and_promote
from .cancellation import send_cancellation_emails
from .config import Settings
from .security import hash_token, new_token
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
      - it exists but isn't confirmed/waitlisted (already canceled, or a
        stale pending_confirmation row) -- Store.cancel() itself is a
        no-op in that case, so this checks the status up front to give a
        clear reason instead of silently "succeeding" at nothing.
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

    # Freshly minted (2026-07-10) so the participant's cancellation email
    # gets a working /reinstate/<token> link too -- see
    # Store.cancel()'s own `reinstate_token_hash` docstring for why the
    # ORIGINAL cancel token can't be reused for this.
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
