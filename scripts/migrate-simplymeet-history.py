#!/usr/bin/env python3
"""One-off: import SimplyMeet.me's booking history into my-booking-tool.

    scripts/migrate-simplymeet-history.py PATH/TO/export.csv [--commit]
        [--settings /etc/my-booking/settings.toml] [--data-dir /var/lib/my-booking]

Default is a DRY RUN: parses the export, decides what it would do with
every row, and prints a report -- writes nothing. Pass --commit to
actually write. Safe to re-run (including with --commit) any number of
times: already-imported rows are detected and skipped, never duplicated.

See app/migrate_simplymeet.py for all the actual decision logic (course
matching by title, the past-only cutoff, the erased-email safety skip,
idempotency) and -- importantly -- the assumptions it documents that
SimplyMeet.me's export can't actually tell us (who canceled a booking,
the true original signup time). Read that module's docstring before
running with --commit.

This is deliberately a STANDALONE script, not a `my-bt` subcommand -- see
SOLUTION-DESIGN.md: a permanent `--import-from-simplymeet.me` flag isn't
worth the added surface area for something run exactly once during the
2026-07 cutover away from SimplyMeet.me. Unlike scripts/my-bt, this script
is NOT installed by packaging/my-booking-tool.spec -- run it straight from
a git checkout (see MY_BOOKING_HOME below).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

# Mirrors scripts/my-bt's own MY_BOOKING_HOME pattern, but defaults to this
# checkout's own repo root (two levels up from scripts/) rather than
# /opt/my-booking -- this script isn't RPM-installed (see this file's own
# docstring), so "run from wherever you cloned/pulled the repo" is the
# realistic default; MY_BOOKING_HOME still lets you point it at an
# installed /opt/my-booking copy of app/ instead, if you'd rather do that.
HOME = os.environ.get("MY_BOOKING_HOME", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, HOME)

try:
    from app.config import load_settings
    from app.migrate_simplymeet import parse_simplymeet_export, plan_import, run_migration
    from app.storage import Store
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Could not import my-booking-tool's app package from {HOME!r} ({exc}).\n"
        "Set MY_BOOKING_HOME to the directory containing app/, or run this "
        "script from within a my-booking-tool checkout."
    )

DEFAULT_SETTINGS = os.environ.get("MY_BOOKING_SETTINGS", "/etc/my-booking/settings.toml")
DEFAULT_DATA_DIR = os.environ.get("MY_BOOKING_DATA", "/var/lib/my-booking")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv_path", help="Path to the SimplyMeet.me 'List view' export CSV")
    parser.add_argument("--settings", default=DEFAULT_SETTINGS, help=f"default: {DEFAULT_SETTINGS}")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"default: {DEFAULT_DATA_DIR}")
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the data directory (default: dry run, writes nothing)",
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)
    store = Store(args.data_dir)

    rows = parse_simplymeet_export(args.csv_path)
    report = plan_import(rows, settings, store)

    print(f"Parsed {len(rows)} row(s) from {args.csv_path}")
    print(f"  {len(report.planned)} registration(s) to import (leaders + guests combined)")
    print(f"    of which {report.guests_imported} are guests (from SimplyMeet.me's \"Other participants\")")
    print(f"  {report.skipped_future} skipped (future occurrence -- not history yet)")
    print(f"  {report.skipped_already_imported} skipped (already imported)")
    print(f"  {report.skipped_erased_email} skipped (email matches an erased/archived account)")
    print(f"  {report.skipped_missing_email} skipped (no client email on the row)")
    if report.skipped_guest_duplicate:
        print(f"  {report.skipped_guest_duplicate} guest(s) skipped (duplicate email -- matched the leader or another guest on the same row)")
    if report.skipped_guest_malformed:
        print(f"  {report.skipped_guest_malformed} guest(s) skipped (malformed email in \"Other participants\")")
    if report.skipped_guest_erased:
        print(f"  {report.skipped_guest_erased} guest(s) skipped (email matches an erased/archived account)")
    if report.skipped_unmatched_course:
        print(
            f"  {len(report.skipped_unmatched_course)} skipped (meeting type not found "
            "among settings.toml [[course]] titles):"
        )
        for title, count in Counter(report.skipped_unmatched_course).most_common():
            print(f"    {count:>4}x  {title!r}")

    if not args.commit:
        print("\nDry run only -- nothing written. Re-run with --commit to actually import.")
        return

    written = run_migration(report.planned, store)
    print(f"\nWrote {written} registration(s) (and any new user accounts they needed).")


if __name__ == "__main__":
    main()
