"""GDPR storage-limitation purge job (Art. 5(1)(e)).

Deletes registration rows once they're older than the configured retention
window:
  - confirmed / canceled_by_* rows: purged `retention_months` after the
    course occurrence_date (default 24 -- see settings.toml [privacy]).
  - canceled rows specifically: purged sooner, `canceled_retention_months`
    after cancellation (default 6), since a canceled booking has little
    ongoing value once the dispute window has passed.
  - pending_confirmation rows (see storage.STATUS_PENDING_CONFIRMATION):
    purged `pending_confirmation_hours` (default 48 -- see settings.toml
    [defaults]) after registered_at, independent of the two rules above --
    an abandoned or bogus signup that never confirmed its account never
    held a real spot anyway, so there's no reason to let it linger for
    months like a real booking.
A row is purged as soon as the applicable rule applies. Runs from a systemd
timer (the modern equivalent of cron) -- see systemd/my-booking-retention.timer.
run_purge() above has no side effects beyond rewriting registrations.csv; it
never touches users.csv itself (email/password rows are cheap to keep and
are needed to recognize returning guests -- an unconfirmed user row left
behind by an expired pending registration is just an inert, password-less
row, same as any other).

2026-07-09, the operator: send_account_deletion_warnings() below (run from the same
nightly timer, right after run_purge() in main()) emails a dormant account
ONE warning as it approaches `retention_months` of inactivity (User.
last_login_at, falling back to created_at) -- see that function's own
docstring for the full story and for the standing gap it does NOT close:
there is still no automated job that actually ERASES a user account once
that deadline passes, only this warning and the registration-row purge
above. If/when real enforcement is wanted, it belongs here too, calling
app.erasure.erase_user_by_email for each account past its deadline.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import Settings
from .emailer import send_mail
from .storage import STATUS_PENDING_CONFIRMATION, Store, Registration, User

log = logging.getLogger("my_booking.retention")


def _months_ago(today: date, months: int) -> date:
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, 28)  # avoid month-length edge cases; good enough for a cutoff date
    return date(year, month, day)


def _months_from(start: date, months: int) -> date:
    """Inverse of _months_ago() above -- `start` plus `months`, same
    edge-case handling (day clamped to 28). Used by
    send_account_deletion_warnings() below to project FORWARD from an
    account's last activity to the date it would reach retention_months
    of inactivity, rather than back from today like every other use of
    "months" in this file."""
    year = start.year
    month = start.month + months
    while month > 12:
        month -= 12
        year += 1
    day = min(start.day, 28)
    return date(year, month, day)


def _occurrence_date(reg: Registration) -> date:
    return date.fromisoformat(reg.occurrence_date)


def should_purge(reg: Registration, today: date, settings: Settings, now: datetime | None = None) -> bool:
    if reg.status == STATUS_PENDING_CONFIRMATION:
        # Hour-granularity, unrelated to the month-based rules below --
        # `now` defaults to midnight UTC of `today` when not given
        # explicitly (fine for this job's actual once-a-night granularity;
        # tests wanting hour precision can pass `now` directly).
        moment = now if now is not None else datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        registered = datetime.fromisoformat(reg.registered_at)
        return moment - registered >= timedelta(hours=settings.pending_confirmation_hours)
    occ_date = _occurrence_date(reg)
    if occ_date <= _months_ago(today, settings.retention_months):
        return True
    if reg.status != "confirmed" and reg.canceled_at:
        canceled_date = datetime.fromisoformat(reg.canceled_at).date()
        if canceled_date <= _months_ago(today, settings.canceled_retention_months):
            return True
    return False


def run_purge(
    store: Store, settings: Settings, today: date | None = None, now: datetime | None = None
) -> int:
    today = today or datetime.now(timezone.utc).date()
    all_regs = store.all_registrations()
    keep = [r for r in all_regs if not should_purge(r, today, settings, now=now)]
    purged = len(all_regs) - len(keep)
    if purged:
        store.replace_all_registrations(keep)
    # WARNING, not INFO: runs once a night via the systemd timer, cheap and
    # low-volume, and this is the only confirmation it actually ran and
    # what it did -- worth surfacing in `journalctl -u
    # my-booking-retention.service` under the default (non-debug) log
    # level, same reasoning as erasure.py's erase confirmation.
    if purged:
        log.warning("retention purge: removed %d of %d registration rows", purged, len(all_regs))
    else:
        log.warning("retention purge: nothing to remove (%d rows checked)", len(all_regs))
    return purged


def send_account_deletion_warnings(
    store: Store, settings: Settings, today: date | None = None, now: datetime | None = None,
) -> int:
    """2026-07-09, the operator: "Our scheduler that then deletes accounts should
    detect imminent accounts that would need to be deleted and then send
    out such an email" (a dormant-account warning email, prompted by a
    Notion account-cleanup notice he forwarded as an example: "your
    account will be deleted in 90 days unless you log in"). Runs from the
    SAME nightly systemd timer as run_purge() above (see main() below) --
    the operator's own mental model is "the scheduler that deletes accounts",
    and this file is already that scheduler.

    A no-op entirely (returns 0, touches nothing) when
    settings.account_deletion_warning_days <= 0 -- see that field's own
    docstring in app/config.py for the three equivalent ways to disable
    it (0, blank, or the key simply omitted from settings.toml).

    Deliberately reuses `retention_months` (the operator: "there is already a
    variable that defines the duration, I believe currently 2 years")
    as the actual dormancy threshold, rather than adding a second
    duration setting -- account_deletion_warning_days only controls HOW
    LONG BEFORE that threshold the one warning email goes out.

    "Inactive" (the operator: "Last login should count") is User.last_login_at;
    an account that has never logged back in since its initial booking
    (last_login_at is blank until Store.touch_login() is ever called)
    falls back to created_at instead -- there's no other activity signal
    to use for it.

    Sends AT MOST ONE warning per dormancy period: skips any account
    whose deletion_warning_sent_at is already set (cleared again by
    Store.touch_login() the next real login -- see that method's own
    docstring), and only warns once the account is within
    `account_deletion_warning_days` of its computed deletion date and
    hasn't already passed it (a full account-deletion ENFORCEMENT job
    doesn't exist yet in this codebase -- see this function's own
    call site in main() for that standing gap).

    Returns how many warning emails were actually sent.
    """
    if settings.account_deletion_warning_days <= 0:
        return 0
    today = today or datetime.now(timezone.utc).date()
    site = urlparse(settings.base_url).hostname or settings.base_url
    my_url = f"{settings.base_url}/my"
    warned = 0
    for row in store.read_users(scope="live"):
        user = User(**row)
        if user.deletion_warning_sent_at:
            continue
        reference_iso = user.last_login_at or user.created_at
        if not reference_iso:
            continue
        reference_date = datetime.fromisoformat(reference_iso).date()
        deletion_date = _months_from(reference_date, settings.retention_months)
        days_left = (deletion_date - today).days
        if days_left < 0 or days_left > settings.account_deletion_warning_days:
            continue
        send_mail(
            settings, user.email, f"Your {site} account will be deleted soon",
            f"Dear {user.name},\n\n"
            f"Your {site} account has had no activity since "
            f"{reference_date.isoformat()}, and is scheduled to be deleted on "
            f"{deletion_date.isoformat()}.\n\n"
            f"Log in before then to keep it: {my_url}\n\n"
            "If you're fine letting it go, no action is needed -- this is the only "
            "reminder you'll get.",
            bcc_addrs=settings.bcc_attendee_email_list,
        )
        store.mark_deletion_warning_sent(user.user_id, now.isoformat() if now else None)
        warned += 1
    if warned:
        log.warning("account-deletion warning: sent %d warning email(s)", warned)
    return warned


def main() -> None:  # pragma: no cover - exercised via systemd, not tests
    import argparse
    from .config import load_settings
    from .logutil import configure_logging

    parser = argparse.ArgumentParser(description="Purge registrations past their GDPR retention window")
    parser.add_argument("--settings", default="/etc/my-booking/settings.toml")
    parser.add_argument("--data-dir", default="/var/lib/my-booking")
    args = parser.parse_args()

    # Load settings first so a configured [logging].log_file is honored --
    # see app/logutil.py (MY_BOOKING_DEBUG=1 for verbose).
    settings = load_settings(args.settings)
    configure_logging(settings.log_file)
    store = Store(args.data_dir)
    run_purge(store, settings)
    send_account_deletion_warnings(store, settings)


if __name__ == "__main__":  # pragma: no cover
    main()
