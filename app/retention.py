"""GDPR storage-limitation purge job (Art. 5(1)(e)).

Deletes registration rows once they're older than the configured retention
window:
  - confirmed / canceled_by_* rows: purged `retention_months` after the
    course occurrence_date (default 24 -- see settings.toml [privacy]).
  - canceled rows specifically: purged sooner, `canceled_retention_months`
    after cancellation (default 6), since a canceled booking has little
    ongoing value once the dispute window has passed.
A row is purged as soon as EITHER rule applies. Runs from a systemd timer
(the modern equivalent of cron) -- see systemd/my-booking-retention.timer.
This module has no side effects beyond rewriting registrations.csv; it never
touches users.csv (email/PIN rows are cheap to keep and are needed to
recognize returning guests -- revisit if you want a separate user-retention
policy later).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .config import Settings
from .storage import Store, Registration

log = logging.getLogger("my_booking.retention")


def _months_ago(today: date, months: int) -> date:
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, 28)  # avoid month-length edge cases; good enough for a cutoff date
    return date(year, month, day)


def _occurrence_date(reg: Registration) -> date:
    return date.fromisoformat(reg.occurrence_date)


def should_purge(reg: Registration, today: date, settings: Settings) -> bool:
    occ_date = _occurrence_date(reg)
    if occ_date <= _months_ago(today, settings.retention_months):
        return True
    if reg.status != "confirmed" and reg.canceled_at:
        canceled_date = datetime.fromisoformat(reg.canceled_at).date()
        if canceled_date <= _months_ago(today, settings.canceled_retention_months):
            return True
    return False


def run_purge(store: Store, settings: Settings, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    all_regs = store.all_registrations()
    keep = [r for r in all_regs if not should_purge(r, today, settings)]
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


def main() -> None:  # pragma: no cover - exercised via systemd, not tests
    import argparse
    from .config import load_settings
    from .logutil import configure_logging

    parser = argparse.ArgumentParser(description="Purge registrations past their GDPR retention window")
    parser.add_argument("--settings", default="/opt/my-booking/settings.toml")
    parser.add_argument("--data-dir", default="/var/lib/my-booking")
    args = parser.parse_args()

    # Load settings first so a configured [logging].log_file is honored --
    # see app/logutil.py (MY_BOOKING_DEBUG=1 for verbose).
    settings = load_settings(args.settings)
    configure_logging(settings.log_file)
    store = Store(args.data_dir)
    run_purge(store, settings)


if __name__ == "__main__":  # pragma: no cover
    main()
