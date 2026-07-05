"""Right-to-erasure orchestration (Art. 17 GDPR): cancels any future
confirmed bookings (freeing the spot + updating the calendar), then archives
the user + all their registration rows with a hashed email (see
security.hash_email_for_erasure and storage.Store.erase_user).

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

from .config import Settings
from .security import hash_email_for_erasure
from .storage import STATUS_CONFIRMED, STATUS_WAITLISTED, Store

log = logging.getLogger("my_booking.erasure")


def erase_user_by_email(
    store: Store,
    settings: Settings,
    email: str,
    today: date | None = None,
) -> bool:
    """Returns False if no user with this email exists (nothing to erase)."""
    user = store.find_user_by_email(email)
    if user is None:
        return False

    today = today or datetime.now(timezone.utc).date()
    for reg in store.registrations_for_user(user.user_id):
        if reg.status in (STATUS_CONFIRMED, STATUS_WAITLISTED) and date.fromisoformat(reg.occurrence_date) >= today:
            store.cancel(reg.registration_id, canceled_by="guest", host_message="account deleted by guest")
            # Caller (webapp.py / my-bt) is responsible for re-running
            # _cancel_and_promote()/calendar_sync for this course/date
            # afterwards, same as any other cancellation -- this function
            # only touches the erased user's own rows.

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
