"""Periodic health check for "strange usage patterns" -- emails
settings.admin_email once per run if any signal below crosses its
configured threshold within the last [watchdog].window_minutes; silent
otherwise. Runs from a systemd timer (my-booking-watchdog.timer), same
one-shot-job pattern as app/retention.py.

This is deliberately a coarse, sitewide, periodic heads-up -- NOT a
replacement for either of the two finer-grained defenses that already
exist:
  - app/security.py's RateLimiter (5 attempts/hour, per email or per IP)
    already blocks/slows a single attacker in real time; the watchdog
    only notices the aggregate pattern afterwards.
  - fail2ban (recommended in README.md, configured at the nginx/sshd
    layer, outside this repo) already bans a single abusive IP outright;
    the watchdog's sshd/nginx checks are deliberately cruder and sitewide,
    an early "something's going on" signal rather than a ban mechanism.

Four independent signals, each optional (a None/0 threshold or missing
log path skips that check silently):
  - nginx access log: one IP making an unusually large number of requests,
    or with an unusually high 4xx/5xx share, within the window.
  - the app's own log: a burst of rate-limiter rejections (login/reset),
    across all keys combined -- see the log.warning() calls in
    app/webapp.py at each login_limiter.allow() rejection.
  - storage.py: a burst of brand-new pending_confirmation registrations
    (see STATUS_PENDING_CONFIRMATION) -- the shape a capacity-grab attempt
    against the account-confirmation flow would take, since a real
    confirmed booking can never produce this signal.
  - sshd: a burst of failed-password attempts, read via `journalctl -u
    sshd` (systemd) -- deliberately sitewide/coarse, not per-IP; that's
    fail2ban's job.

Every check function below is pure (takes already-read lines/rows plus a
`now`, returns a list of human-readable alert strings) so none of it needs
a real nginx log file, journald, or filesystem to unit test -- only
main()'s CLI entrypoint touches those.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import Settings
from .emailer import send_mail
from .storage import STATUS_PENDING_CONFIRMATION, Store

log = logging.getLogger("my_booking.watchdog")

# Below this many total requests from an IP within the window, its error
# rate isn't evaluated at all -- one 404 out of one request is a 100%
# "error rate" but not a meaningful signal.
_MIN_REQUESTS_FOR_ERROR_RATE = 20

# Matches the leading "%(asctime)s %(levelname)s ..." (or, in DEBUG mode,
# "%(asctime)s %(levelname)s %(name)s: ...") produced by
# app/logutil.py::configure_logging -- asctime's default format is
# "YYYY-MM-DD HH:MM:SS,mmm".
_APP_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")

# nginx's default combined log format:
# '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent ...'
_NGINX_LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3})'
)
# e.g. "05/Jul/2026:14:32:10 +0200"
_NGINX_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


def _parse_app_log_timestamp(line: str) -> datetime | None:
    m = _APP_LOG_TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_nginx_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _NGINX_TIME_FMT)
    except ValueError:
        return None


def check_pending_signup_burst(store: Store, settings: Settings, now: datetime | None = None) -> list[str]:
    if settings.watchdog_pending_signup_threshold <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=settings.watchdog_window_minutes)
    count = sum(
        1
        for r in store.all_registrations()
        if r.status == STATUS_PENDING_CONFIRMATION and datetime.fromisoformat(r.registered_at) >= since
    )
    if count >= settings.watchdog_pending_signup_threshold:
        return [
            f"{count} new pending (unconfirmed) signups in the last "
            f"{settings.watchdog_window_minutes} min (threshold "
            f"{settings.watchdog_pending_signup_threshold}) -- possible capacity-grab attempt "
            "against the booking page."
        ]
    return []


def check_app_log_rate_limit_blocks(
    lines: Iterable[str], settings: Settings, now: datetime | None = None
) -> list[str]:
    if settings.watchdog_rate_limit_block_threshold <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=settings.watchdog_window_minutes)
    count = 0
    for line in lines:
        if "rate limit blocked:" not in line:
            continue
        ts = _parse_app_log_timestamp(line)
        if ts is not None and ts >= since:
            count += 1
    if count >= settings.watchdog_rate_limit_block_threshold:
        return [
            f"{count} rate-limiter rejections (login/reset, all keys combined) in the last "
            f"{settings.watchdog_window_minutes} min (threshold "
            f"{settings.watchdog_rate_limit_block_threshold})."
        ]
    return []


def check_nginx_bursts(lines: Iterable[str], settings: Settings, now: datetime | None = None) -> list[str]:
    if not settings.watchdog_nginx_access_log:
        return []
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=settings.watchdog_window_minutes)
    total: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for line in lines:
        m = _NGINX_LINE_RE.match(line)
        if not m:
            continue
        ts = _parse_nginx_timestamp(m.group("time"))
        if ts is None:
            continue
        # nginx's $time_local carries its own UTC offset -- compare in UTC
        # so `since` (already UTC) lines up regardless of the server's zone.
        if ts.astimezone(timezone.utc) < since:
            continue
        ip = m.group("ip")
        total[ip] += 1
        if m.group("status").startswith(("4", "5")):
            errors[ip] += 1

    alerts = []
    for ip, count in total.items():
        if count >= settings.watchdog_nginx_request_threshold:
            alerts.append(
                f"{ip} made {count} requests in the last {settings.watchdog_window_minutes} min "
                f"(threshold {settings.watchdog_nginx_request_threshold})."
            )
        elif count >= _MIN_REQUESTS_FOR_ERROR_RATE:
            rate = errors[ip] / count
            if rate >= settings.watchdog_nginx_error_rate_threshold:
                alerts.append(
                    f"{ip}: {errors[ip]}/{count} requests ({rate:.0%}) were 4xx/5xx in the last "
                    f"{settings.watchdog_window_minutes} min (threshold "
                    f"{settings.watchdog_nginx_error_rate_threshold:.0%})."
                )
    return alerts


def check_sshd_failures(lines: Iterable[str], settings: Settings) -> list[str]:
    """`lines` is expected to already be scoped to the window (e.g. via
    `journalctl --since ...`) -- unlike the other two log-based checks,
    journalctl's own `--since` does the time filtering, so there's no
    per-line timestamp parsing to do here."""
    if settings.watchdog_sshd_failure_threshold <= 0:
        return []
    count = sum(1 for line in lines if "Failed password" in line)
    if count >= settings.watchdog_sshd_failure_threshold:
        return [
            f"{count} failed SSH password attempts in the last "
            f"{settings.watchdog_window_minutes} min (threshold "
            f"{settings.watchdog_sshd_failure_threshold}) -- fail2ban should already be acting on "
            "repeat offenders per-IP; this is just the sitewide total."
        ]
    return []


def run_watchdog(
    store: Store,
    settings: Settings,
    app_log_lines: Iterable[str] = (),
    nginx_log_lines: Iterable[str] = (),
    sshd_log_lines: Iterable[str] = (),
    now: datetime | None = None,
) -> list[str]:
    """Runs every enabled check, emails settings.admin_email ONE combined
    message if anything fired, and always returns the list of alert
    strings (empty if everything's quiet) so callers/tests don't have to
    inspect the sent email to know what happened."""
    if not settings.watchdog_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    alerts: list[str] = []
    alerts += check_pending_signup_burst(store, settings, now=now)
    alerts += check_app_log_rate_limit_blocks(app_log_lines, settings, now=now)
    alerts += check_nginx_bursts(nginx_log_lines, settings, now=now)
    alerts += check_sshd_failures(sshd_log_lines, settings)

    if alerts:
        body = "The my-booking-tool watchdog noticed the following in the last {} minutes:\n\n{}".format(
            settings.watchdog_window_minutes, "\n".join(f"- {a}" for a in alerts)
        )
        send_mail(settings, settings.admin_email, "my-booking-tool watchdog alert", body)
        # WARNING (not DEBUG): worth surfacing in journalctl even without
        # MY_BOOKING_DEBUG -- same reasoning as retention.py/erasure.py.
        log.warning("watchdog: %d alert(s) fired, email sent to %s", len(alerts), settings.admin_email)
    else:
        log.warning("watchdog: nothing unusual found")
    return alerts


def _read_lines(path: str | None) -> list[str]:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError as exc:
        log.warning("watchdog: could not read %s (%s); skipping that check", path, exc)
        return []


def _sshd_lines_since(window_minutes: int) -> list[str]:
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "sshd", "--since", f"-{window_minutes}min", "-o", "cat"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("watchdog: could not read sshd journal (%s); skipping that check", exc)
        return []
    if proc.returncode != 0:
        log.warning("watchdog: journalctl exited %d; skipping sshd check", proc.returncode)
        return []
    return proc.stdout.splitlines()


def main() -> None:  # pragma: no cover - exercised via systemd, not tests
    import argparse
    from .config import load_settings
    from .logutil import configure_logging

    parser = argparse.ArgumentParser(description="Check for strange usage patterns and email admin_email if found")
    parser.add_argument("--settings", default="/opt/my-booking/settings.toml")
    parser.add_argument("--data-dir", default="/var/lib/my-booking")
    args = parser.parse_args()

    settings = load_settings(args.settings)
    configure_logging(settings.log_file)
    store = Store(args.data_dir)

    app_log_lines = _read_lines(settings.log_file)
    nginx_log_lines = _read_lines(settings.watchdog_nginx_access_log)
    sshd_log_lines = _sshd_lines_since(settings.watchdog_window_minutes) if settings.watchdog_sshd_failure_threshold > 0 else []

    run_watchdog(
        store, settings,
        app_log_lines=app_log_lines,
        nginx_log_lines=nginx_log_lines,
        sshd_log_lines=sshd_log_lines,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
