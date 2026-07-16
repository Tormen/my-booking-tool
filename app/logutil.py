"""Shared logging setup for every entrypoint (serve.py, retention.py,
scripts/my-bt). One env var controls verbosity everywhere:

  MY_BOOKING_DEBUG=1   verbose: every request/CalDAV call logged, full
                       tracebacks on errors.
  unset / 0 / (empty)  quiet: routine operation isn't logged at all, but
                       real problems (an unhandled request exception, a
                       failed CalDAV/SMTP call) are never silenced --
                       they're logged at WARNING/ERROR, which always
                       shows regardless of this setting.

This is deliberately just two levels, not a numeric verbosity scale --
this is meant for a single operator running their own small instance, and
"quiet" vs. "I'm actively debugging something" is the only distinction
that matters in practice.

Privacy note: log lines here are written with the same care as anything
else in this project -- avoid putting a guest's raw email/name in a log
line (log a user_id/registration_id instead; that's enough to cross-
reference the CSVs if you need more detail). journald has its own
retention that's independent of this app's GDPR retention_months config,
so anything logged here effectively bypasses that -- keep it minimal on
purpose. If you ever paste `journalctl` output into a bug report (e.g. to
the assistant, or anyone else), skim it first for anything personal.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time

# Size-capped rotation for [logging].log_file (2026-07-16, added the same
# moment file logging became ON by default -- see config.DEFAULT_LOG_FILE):
# a plain FileHandler grows forever and this project ships no logrotate
# config, which is fine for an opt-in file the operator knowingly enabled
# but not for a default. ~2 MB x (1 live + 3 backups) caps the whole thing
# at ~8 MB -- years of WARNING-level logs at this project's scale. The
# watchdog/`my-bt status`/health checks read only the LIVE file; a
# rollover mid-window can at most hide lines from before the rollover,
# acceptable for coarse heads-up tooling (fail2ban/RateLimiter are the
# precise layers).
LOG_FILE_MAX_BYTES = 2_000_000
LOG_FILE_BACKUP_COUNT = 3


def debug_enabled() -> bool:
    return os.environ.get("MY_BOOKING_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


def configure_logging(log_file: str | None = None) -> bool:
    """Call once, near the start of each entrypoint. Returns debug_enabled()
    for convenience (callers that want to gate extra behavior on it).

    log_file (from settings.toml [logging].log_file, see app/config.py::
    log_file_from_raw for the on-by-default/"" semantics) is optional --
    when given, log records also get appended there (size-capped
    rotation, see LOG_FILE_MAX_BYTES above), in addition to
    stdout/journal. If the file can't be opened (missing directory,
    permissions), that's reported once and logging continues stdout-only
    rather than crashing the caller over a logging nicety.
    """
    debug = debug_enabled()
    level = logging.DEBUG if debug else logging.WARNING
    fmt = (
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
        if debug
        else "%(asctime)s %(levelname)s %(message)s"
    )
    formatter = logging.Formatter(fmt)
    # UTC, not whatever the server's system timezone happens to be --
    # every other timestamp in this app is UTC (storage.now_iso(),
    # watchdog.py's own datetime.now(timezone.utc) calls), and
    # app/watchdog.py::_parse_app_log_timestamp() parses this exact
    # "asctime" format and labels it UTC directly. logging.Formatter
    # defaults to the LOCAL system time for %(asctime)s, which was only
    # "correct by accident" as long as the server's OS timezone happened
    # to be UTC -- caught 2026-07-05 when changing the server's system
    # timezone came up (see the maintainer's local notes): without this, the
    # watchdog's rate-limit-block window would silently skew by the
    # server's UTC offset the moment the OS clock isn't UTC.
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        try:
            handlers.append(logging.handlers.RotatingFileHandler(
                log_file, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUP_COUNT,
            ))
        except OSError as exc:
            print(f"my-booking-tool: could not open log_file {log_file!r} ({exc}); "
                  "continuing without it", file=sys.stderr)
    for h in handlers:
        h.setFormatter(formatter)
    # force=True: safe to call configure_logging() more than once in the
    # same process (e.g. a caller that re-resolves log_file after loading
    # settings) without ending up with duplicated handlers. format= is
    # deliberately NOT passed here -- each handler already carries its own
    # (UTC-converting) formatter, and passing format= too would just be
    # redundant/ignored by basicConfig for handlers that already have one.
    logging.basicConfig(level=level, handlers=handlers, force=True)
    return debug
