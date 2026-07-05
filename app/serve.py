"""Entrypoint: `python3 -m app.serve` -- runs the WSGI app behind nginx via
a plain TCP socket on 127.0.0.1 (nginx does TLS + is the public front door;
see nginx/my-booking.conf). Stdlib wsgiref -- no gunicorn/uwsgi needed for
this traffic level; if that ever changes, swap this file only.
"""
from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .config import load_settings
from .logutil import configure_logging
from .storage import Store
from .webapp import App


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="/opt/my-booking/settings.toml")
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
    store = Store(args.data_dir)
    app = App(settings, store)

    with make_server(args.host, args.port, app) as httpd:
        print(f"my-booking-tool: serving on {args.host}:{args.port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
