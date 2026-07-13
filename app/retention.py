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

2026-07-09: send_account_deletion_warnings() below (run from the same
nightly timer, right after run_purge() in main()) emails a dormant account
ONE warning as it approaches `retention_months` of inactivity (User.
last_login_at, falling back to created_at) -- see that function's own
docstring for the full story.

2026-07-14, closing the gap flagged above: this must apply regardless of
whether the warning email is even enabled, and must be tied to
retention_months exactly, since that's the actual GDPR-mandated window --
purge_dormant_accounts()
below now actually ERASES (archives with a hashed email, same as any other
erasure -- see app.erasure.erase_user_by_email) every account past that
same retention_months deadline, unconditionally: it does not care whether
account_deletion_warning_days is configured, disabled, or whether this
particular account ever got warned. The warning is an optional courtesy
notice; this purge is the actual GDPR-mandated enforcement and always
applies once retention_months is set (which it always is, default 24).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from .caldav_client import CalDAVClient
from .config import Settings
from .emailer import send_mail
from .erasure import erase_user_by_email
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


def registration_purge_date(reg: Registration, settings: Settings) -> date:
    """Forward-projected counterpart to should_purge()'s backward
    month(s)-ago checks -- the actual calendar date this row would be (or
    already was) purged on, for `my-bt admin gdpr bookings`'s per-row
    listing. Mirrors should_purge()'s own rules exactly (keep both in sync
    if the retention rules ever change) but expressed as a date rather
    than a yes/no, and pending_confirmation rows are rounded to a date too
    (should_purge checks those at hour granularity; a listing has no
    reason to be more precise than a day)."""
    if reg.status == STATUS_PENDING_CONFIRMATION:
        registered = datetime.fromisoformat(reg.registered_at)
        return (registered + timedelta(hours=settings.pending_confirmation_hours)).date()
    expiry = _months_from(_occurrence_date(reg), settings.retention_months)
    if reg.status != "confirmed" and reg.canceled_at:
        canceled_expiry = _months_from(
            datetime.fromisoformat(reg.canceled_at).date(), settings.canceled_retention_months
        )
        expiry = min(expiry, canceled_expiry)
    return expiry


def account_deletion_date(user: User, settings: Settings) -> date | None:
    """The date `user`'s account reaches `retention_months` of inactivity,
    projected forward from their last real activity -- User.last_login_at,
    falling back to created_at if they've never logged back in since (see
    send_account_deletion_warnings()'s own docstring, "Inactive... Last
    login should count"). None if there's no activity timestamp at all to
    project from (shouldn't happen in practice -- created_at is always set
    -- but a listing/purge job should skip rather than crash on a
    corrupted/hand-edited row).

    Shared by send_account_deletion_warnings() (the courtesy notice),
    purge_dormant_accounts() (the actual enforcement), and `my-bt admin
    gdpr accounts`'s listing, so all three always agree on exactly the
    same date for the same account -- no drift between "you were warned
    this would happen on X" and "it actually happened on Y"."""
    reference_iso = user.last_login_at or user.created_at
    if not reference_iso:
        return None
    reference_date = datetime.fromisoformat(reference_iso).date()
    return _months_from(reference_date, settings.retention_months)


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
    """2026-07-09: the scheduler that deletes accounts should also detect
    imminent accounts that would need to be deleted and send a warning
    email first (a dormant-account warning email, the same idea as a
    typical "your account will be deleted in 90 days unless you log in"
    notice). Runs from the
    SAME nightly systemd timer as run_purge() above (see main() below) --
    this file is already "the scheduler that deletes accounts", so the
    warning belongs alongside it.

    A no-op entirely (returns 0, touches nothing) when
    settings.account_deletion_warning_days <= 0 -- see that field's own
    docstring in app/config.py for the three equivalent ways to disable
    it (0, blank, or the key simply omitted from settings.toml).

    Deliberately reuses `retention_months` (the existing setting that
    already defines the retention duration) as the actual dormancy
    threshold, rather than adding a second
    duration setting -- account_deletion_warning_days only controls HOW
    LONG BEFORE that threshold the one warning email goes out.

    "Inactive" is User.last_login_at (last login is what counts, not
    just account age); an account that has never logged back in since its initial booking
    (last_login_at is blank until Store.touch_login() is ever called)
    falls back to created_at instead -- there's no other activity signal
    to use for it.

    Sends AT MOST ONE warning per dormancy period: skips any account
    whose deletion_warning_sent_at is already set (cleared again by
    Store.touch_login() the next real login -- see that method's own
    docstring), and only warns once the account is within
    `account_deletion_warning_days` of its computed deletion date and
    hasn't already passed it. Purely a courtesy notice -- see
    purge_dormant_accounts() below for the actual enforcement, which runs
    independently of this (2026-07-14: regardless of whether
    this warning is even enabled).

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
        deletion_date = account_deletion_date(user, settings)
        if deletion_date is None:
            continue
        days_left = (deletion_date - today).days
        if days_left < 0 or days_left > settings.account_deletion_warning_days:
            continue
        reference_date = datetime.fromisoformat(user.last_login_at or user.created_at).date()
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


def purge_dormant_accounts(
    store: Store, settings: Settings, today: date | None = None, caldav: CalDAVClient | None = None,
) -> int:
    """`my-bt admin gdpr accounts --purge` -- the actual account-erasure
    enforcement (2026-07-14: added the account purge after the same
    duration as the warning above). Erases (archives with a hashed email, via
    app.erasure.erase_user_by_email -- same mechanism as the guest's own
    `/my` self-erasure and `my-bt erase`) every LIVE account whose
    account_deletion_date() has already passed.

    Unconditional -- runs regardless of whether
    settings.account_deletion_warning_days is configured/enabled, and
    regardless of whether this particular account was ever actually
    warned. There is no separate on/off switch
    for this: it is tied exactly to `retention_months` (the actual
    GDPR-mandated window), the same setting send_account_deletion_warnings()
    above already projects from.

    `caldav`, if given, is passed straight through to
    erase_user_by_email() so any still-future confirmed/waitlisted
    booking a dormant account happens to hold gets properly canceled
    (promoting the next waitlisted person + re-syncing the calendar), same
    as every other erasure path -- see erase_user_by_email()'s own
    docstring.

    Returns how many accounts were erased."""
    today = today or datetime.now(timezone.utc).date()
    purged = 0
    for row in store.read_users(scope="live"):
        user = User(**row)
        deadline = account_deletion_date(user, settings)
        if deadline is None or deadline > today:
            continue
        if erase_user_by_email(store, settings, user.email, today=today, caldav=caldav):
            purged += 1
    if purged:
        log.warning("account purge: erased %d dormant account(s)", purged)
    return purged


def registration_purge_counts_by_month(store: Store, settings: Settings) -> dict[str, int]:
    """`my-bt admin gdpr`'s "expected purge counts per month" table --
    every registration row's registration_purge_date(), bucketed by
    "YYYY-MM". Includes rows already past due (their month is in the
    past) exactly as `admin gdpr bookings`'s own listing does -- this is
    a forward-*and*-backward-looking projection of when each row
    reaches/reached its retention window, not just a count of what's
    still upcoming. 2026-07-14: added to also show the currently
    expected purge counts per month for accounts and for bookings."""
    counts: dict[str, int] = {}
    for reg in store.all_registrations():
        month = registration_purge_date(reg, settings).strftime("%Y-%m")
        counts[month] = counts.get(month, 0) + 1
    return counts


def account_deletion_counts_by_month(store: Store, settings: Settings) -> dict[str, int]:
    """Same as registration_purge_counts_by_month() above, but for LIVE
    accounts and account_deletion_date() -- skips any account with no
    activity timestamp to project from (see that function's own
    docstring for why that's possible in principle)."""
    counts: dict[str, int] = {}
    for row in store.read_users(scope="live"):
        user = User(**row)
        deadline = account_deletion_date(user, settings)
        if deadline is None:
            continue
        month = deadline.strftime("%Y-%m")
        counts[month] = counts.get(month, 0) + 1
    return counts
