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
import os
import sys


def debug_enabled() -> bool:
    return os.environ.get("MY_BOOKING_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


def configure_logging(log_file: str | None = None) -> bool:
    """Call once, near the start of each entrypoint. Returns debug_enabled()
    for convenience (callers that want to gate extra behavior on it).

    log_file (from settings.toml [logging].log_file, see app/config.py) is
    optional -- when given, log records also get appended there, in
    addition to stdout/journal. If the file can't be opened (missing
    directory, permissions), that's reported once and logging continues
    stdout-only rather than crashing the caller over a logging nicety.
    """
    debug = debug_enabled()
    level = logging.DEBUG if debug else logging.WARNING
    fmt = (
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
        if debug
        else "%(asctime)s %(levelname)s %(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file))
        except OSError as exc:
            print(f"my-booking-tool: could not open log_file {log_file!r} ({exc}); "
                  "continuing without it", file=sys.stderr)
    # force=True: safe to call configure_logging() more than once in the
    # same process (e.g. a caller that re-resolves log_file after loading
    # settings) without ending up with duplicated handlers.
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    return debug
