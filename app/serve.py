"""Entrypoint: `python3 -m app.serve` -- runs the WSGI app behind nginx via
a plain TCP socket on 127.0.0.1 (nginx does TLS + is the public front door;
see nginx/my-booking.conf). Stdlib wsgiref -- no gunicorn/uwsgi needed for
this traffic level; if that ever changes, swap this file only.
"""
from __future__ import annotations

import argparse
import logging
from typing import Callable
from wsgiref.simple_server import make_server

from .atomic_io import probe_dir_fsync_support
from .config import Settings, load_settings
from .emailer import send_mail
from .logutil import configure_logging
from .storage import Store
from .webapp import App

log = logging.getLogger("my_booking.serve")


def check_directory_fsync_support_at_startup(
    data_dir: str, settings: Settings, send_mail_fn: Callable[..., None] = send_mail,
) -> bool:
    """Runs `app.atomic_io.probe_dir_fsync_support()` once at process
    startup and reacts LOUDLY to a False result, unlike `fsync_dir()`'s
    own routine per-write WARNING.

    2026-07-15: fsync_dir()'s best-effort/never-raises design is correct
    for availability, but it's also the kind of failure that's invisible
    until the one time it matters -- if the actual production mount
    silently doesn't support directory fsync, every write since deploy
    has been getting the weaker guarantee with nobody the wiser. Worth a
    one-time capability probe at startup that logs loudly, or surfaces
    via whatever alerting is already in place, rather than relying on
    someone noticing a warning line in a log nobody tails. This
    project's own actual admin-alert mechanism is app.watchdog's
    admin_email (fail2ban/
    rkhunter are separate host-level tools with no code-level
    integration here) -- reused here directly, best-effort, rather than
    bolted onto watchdog's own 15-minute timer, which would re-alert on
    a persistent condition every single run instead of once per restart.

    Also checked, repeatably, via `my-bt admin setup`/`admin health` --
    see app.cli_checks.check_directory_fsync_support -- this startup
    check is the "impossible to miss even if nobody ever runs that
    command" half; that one is the "re-checkable any time, participates
    in the exit-1-on-warn policy" half.

    Returns whether support was confirmed (so tests/callers don't have
    to inspect logs or a sent email to know what happened). Never
    raises, and never prevents the server from actually starting -- a
    weaker durability guarantee is not a reason to also take the site
    down; a failed alert EMAIL specifically is swallowed (logged as a
    warning) for the same reason, since SMTP being unreachable at boot
    must not be able to block every request behind it."""
    if probe_dir_fsync_support(data_dir):
        return True
    log.error(
        "STARTUP CHECK FAILED: directory fsync is NOT supported on %s -- every atomic write's "
        "rename can silently roll back on a hard power loss instead of surviving it (see the "
        "'Data durability' section of README.md and app/atomic_io.py). Run `my-bt admin setup` "
        "or `my-bt admin health` any time to re-check without restarting the service.",
        data_dir,
    )
    try:
        send_mail_fn(
            settings, settings.admin_email,
            "my-booking-tool: directory fsync NOT supported",
            f"The data directory ({data_dir}) does not support directory fsync.\n\n"
            "Every write is still crash-safe against a torn/partial file, but a rename can "
            "silently roll back on a hard power loss instead of surviving it -- see the "
            "'Data durability' section of README.md and app/atomic_io.py.\n\n"
            "This check runs once per service start, so this email will repeat on every "
            "restart until it's fixed. Run `my-bt admin setup` or `my-bt admin health` any time "
            "to re-check without restarting the service.",
        )
    except Exception as exc:  # noqa: BLE001 -- an alert failing to send must never block startup
        log.warning("could not email admin_email about the fsync-support failure above: %s", exc)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="/etc/my-booking/settings.toml")
    parser.add_argument("--data-dir", default="/var/lib/my-booking")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8811)
    args = parser.parse_args()

    # settings.toml [logging].log_file (if set) is honored automatically --
    # load settings first so configure_logging() knows about it. See
    # app/logutil.py: MY_BOOKING_DEBUG=1 for verbose tracing, unset for
    # quiet-but-errors-still-show. This one startup line is always printed
    # (not gated behind the log level) so a manual run confirms it's
    # actually listening; journalctl shows it too either way since it's
    # still going to stdout.
    settings = load_settings(args.settings)
    configure_logging(settings.log_file)
    check_directory_fsync_support_at_startup(args.data_dir, settings)
    store = Store(args.data_dir)
    app = App(settings, store)

    with make_server(args.host, args.port, app) as httpd:
        print(f"my-booking-tool: serving on {args.host}:{args.port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
