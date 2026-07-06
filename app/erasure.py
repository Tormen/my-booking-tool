"""Right-to-erasure orchestration (Art. 17 GDPR): cancels any future
confirmed/waitlisted bookings (freeing the spot + updating the calendar),
then archives the user + all their registration rows with a hashed email
(see security.hash_email_for_erasure and storage.Store.erase_user).

Used from two places:
  - the guest self-service portal (`/my` -> "delete my account & data"),
    fully automatic, no admin involvement;
  - the `my-bt erase` CLI command, for a manual/admin-triggered erasure
    (e.g. someone emails the configured admin address asking to be
    forgotten instead of using the portal).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .caldav_client import CalDAVClient
from .cancel_flow import cancel_and_promote
from .config import Settings
from .security import hash_email_for_erasure, is_erased_email
from .storage import STATUS_CONFIRMED, STATUS_WAITLISTED, Store

log = logging.getLogger("my_booking.erasure")


def erase_user_by_email(
    store: Store,
    settings: Settings,
    email: str,
    today: date | None = None,
    caldav: CalDAVClient | None = None,
) -> bool:
    """Returns False if no user with this email exists (nothing to erase).

    `caldav`, if given, runs the exact same app.cancel_flow.cancel_and_promote
    used by every other cancellation path (promote the next waitlisted
    person + re-sync the calendar event) for each future confirmed/
    waitlisted booking force-canceled here -- see cancel_flow's own
    docstring. Both real callers (app/webapp.py's `/my` self-erasure path
    and `my-bt erase`, see scripts/my-bt::cmd_erase) pass one; it's optional
    here (default None: cancel the rows, skip promotion/calendar sync) so
    existing direct-call tests -- ones deliberately exercising just the
    cancel-and-archive logic without any CalDAV/network dependency -- keep
    working unchanged."""
    user = store.find_user_by_email(email)
    if user is None:
        return False

    today = today or datetime.now(timezone.utc).date()
    for reg in store.registrations_for_user(user.user_id):
        if reg.status in (STATUS_CONFIRMED, STATUS_WAITLISTED) and date.fromisoformat(reg.occurrence_date) >= today:
            store.cancel(reg.registration_id, canceled_by="guest", host_message="account deleted by guest")
            if caldav is not None and settings.course(reg.course_shortname):
                cancel_and_promote(store, settings, caldav, reg.course_shortname, reg.occurrence_date)

    hashed = hash_email_for_erasure(user.email, settings.erasure_pepper)
    ok = store.erase_user(user.user_id, hashed)
    if ok:
        # WARNING, not INFO/DEBUG: this is the only server-side record when
        # a guest self-erases via /my (no admin/terminal is involved at
        # all), so it needs to show up under the default MY_BOOKING_DEBUG=
        # unset log level, not just when actively debugging. Logs user_id,
        # never the email itself.
        log.warning("erased user %s (email hashed, moved to archive)", user.user_id)
    return ok


def find_archived_user_ids_for_email(store: Store, settings: Settings, email: str) -> list[str]:
    """Hashes `email` the same way erase_user_by_email did at erasure time
    (security.hash_email_for_erasure, keyed with settings.erasure_pepper),
    then returns every archived user_id whose stored (hashed) email matches
    -- i.e. every past identity this same guest was erased under. Normally
    at most one, but a guest could in principle be erased more than once
    under the same email over time (book again, get erased again, ...), so
    this always returns a list rather than assuming a single match.

    This is the exact lookup app/webapp.py's admin_overview() does inline
    for its display-only "N (incl. M pre-erasure)" merge -- factored out
    here so `my-bt history`/`my-bt merge` (app/cli_history.py) compute the
    same thing the same way, rather than re-deriving the hash logic a
    second time somewhere else."""
    hashed = hash_email_for_erasure(email, settings.erasure_pepper)
    return [
        u["user_id"] for u in store.read_users(scope="archived")
        if is_erased_email(u["email"]) and u["email"] == hashed
    ]
