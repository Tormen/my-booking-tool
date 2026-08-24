"""Validation of the REAL settings.toml -- the operator's own config.

Nothing to enable: these run automatically whenever a real settings.toml
is found, which on the maintainer's machine and on the server is always.
They skip only where one cannot exist (fresh clone, CI, the RPM build),
so the suite stays green everywhere without ever silently skipping where
it matters.

Every other test in this suite builds Settings/Course in memory via
tests/helpers.py, on purpose: unit tests must be hermetic, must not
depend on one machine's configuration, and must keep passing in a fresh
clone, in CI and in the RPM's %check -- none of which have a real
settings.toml (it is gitignored) or its secrets. That stays true.

What was missing is the other half: nothing ever checked that the
operator's OWN config is actually valid, so a mistake in it only
surfaced when the service refused to start, or when a booking page
500'd. This module closes that gap without giving up hermeticity: it
SKIPS entirely unless a real settings.toml is found, so it is inert
everywhere except the machine that has one -- exactly the same
skip-if-absent pattern tests/test_cli_checks.py already uses for the
real, gitignored nginx conf and index.html.

A hard rule for anything added here: an assertion must never echo a
config VALUE. Failures print into terminals, CI output and the RPM build
log, and this config holds a CalDAV account, secret file paths and a
published feed URL carrying its own access token. Prefer
assertTrue(cond, "explanatory message") over assertIn/assertEqual, whose
failure output dumps the container.

Deliberately OFFLINE and secret-free: it never reads a password file and
never touches CalDAV, SMTP or an ICS feed. It validates the parts of the
config that can be wrong on their own -- which is where the real-world
mistakes have been. Live checks (does this calendar exist on the server,
does this feed fetch) belong to `my-bt admin health`, which already does
them; duplicating them here would make the test suite depend on the
network and on someone else's uptime.

Searched, in order: $MY_BOOKING_SETTINGS, this checkout's `*.local/`
overlay directory (app/local_overlay.py), this checkout's own
settings.toml, then the installed /etc/my-booking/settings.toml.
"""
import os
import unittest
from pathlib import Path

from app import config as app_config
from app import local_overlay

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OVERLAY_SETTINGS = local_overlay.source(_REPO_ROOT, "settings.toml")

_CANDIDATES = (
    os.environ.get("MY_BOOKING_SETTINGS") or "",
    str(_OVERLAY_SETTINGS) if _OVERLAY_SETTINGS else "",
    str(_REPO_ROOT / "settings.toml"),
    "/etc/my-booking/settings.toml",
)


def _real_settings_path() -> Path | None:
    for candidate in _CANDIDATES:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class RealSettingsFileTest(unittest.TestCase):
    def setUp(self):
        path = _real_settings_path()
        if path is None:
            self.skipTest(
                "no real settings.toml found (fresh clone / CI / RPM build) -- "
                f"looked in: {', '.join(c for c in _CANDIDATES if c)}"
            )
        self.path = path
        raw = app_config.load_raw_toml(path)
        if raw is None:
            self.fail(f"{path} exists but could not be parsed as TOML")
        self.raw = raw

    # -- the mistakes that stop the service from starting --------------------

    def test_no_leftover_legacy_calendar_section(self):
        # The 2026-07-18 redesign deliberately kept NO backward
        # compatibility: a surviving [calendar] section is a hard startup
        # error. Since settings.toml is %config(noreplace), an upgrade
        # will not migrate it for you -- this catches it while you can
        # still fix it calmly, instead of after `systemctl restart`.
        # assertTrue, not assertNotIn: a failing assertNotIn prints the
        # whole container, which here is the operator's entire parsed
        # config -- CalDAV account, secret file paths and the published
        # feed URL with its token -- straight into the terminal or build
        # log. No assertion in this module may echo config VALUES; see
        # this module's own docstring.
        self.assertTrue(
            "calendar" not in self.raw,
            "[calendar] was replaced by [booking_calendar] + [[conflict_calendar]]; "
            "the service refuses to start until it is migrated",
        )

    def test_booking_calendar_has_every_required_key(self):
        booking = self.raw.get("booking_calendar")
        self.assertIsNotNone(booking, "[booking_calendar] section is missing")
        for key in ("caldav_url", "username", "password_file", "calendar"):
            # assertTrue rather than assertIn -- same no-echo rule.
            self.assertTrue(key in booking, f"[booking_calendar].{key} is missing")

    def test_courses_parse_and_shortnames_are_unique(self):
        courses = app_config.courses_from_raw(self.raw)
        self.assertTrue(courses, "no [[course]] entries -- the site would have nothing to book")
        names = [c.shortname for c in courses]
        self.assertCountEqual(
            names, set(names),
            "duplicate course shortnames -- registrations key on the shortname, "
            "so two courses sharing one would share bookings",
        )
        for course in courses:
            with self.subTest(course=course.shortname):
                self.assertTrue(
                    course.weekday.lower() in course.WEEKDAY_LABELS,
                    f"{course.shortname}: weekday {course.weekday!r} is not one of mon..sun",
                )
                self.assertGreater(course.capacity, 0)
                course.start_hm()   # raises on a malformed "HH:MM"
                course.end_hm()

    def test_every_conflict_calendar_entry_validates(self):
        # _conflict_calendar_from_raw does all the load-time validation
        # (exactly one source, known mode/show_as, HH:MM windows, and that
        # `courses`/`all_courses_but` name real shortnames). Anything it
        # rejects would fail the service at startup.
        shortnames = {c.shortname for c in app_config.courses_from_raw(self.raw)}
        for i, entry in enumerate(self.raw.get("conflict_calendar", [])):
            name = entry.get("name") or f"conflict-{i + 1}"
            with self.subTest(entry=name):
                if entry.get("caldav_url") and not Path(entry.get("password_file", "")).is_file():
                    self.skipTest(f"{name}: own-CalDAV entry whose secret lives on the server")
                app_config._conflict_calendar_from_raw(entry, i, shortnames)

    def test_a_blocks_mode_entry_covers_the_booking_calendar(self):
        # Without one, a "cancel entire session" CANCELED blocker event is
        # never conflict-checked, so the canceled date silently stays
        # bookable -- the exact bug fixed on 2026-07-14. `my-bt admin
        # health` warns about this too; this catches it offline.
        booking = self.raw.get("booking_calendar", {})
        covered = any(
            entry.get("mode", "requires") == "blocks"
            and (
                str(entry.get("source", "")) == "booking_calendar"
                or (entry.get("caldav_url") == booking.get("caldav_url")
                    and entry.get("calendar") == booking.get("calendar"))
            )
            for entry in self.raw.get("conflict_calendar", [])
        )
        self.assertTrue(
            covered,
            'no blocks-mode [[conflict_calendar]] entry covers the booking calendar '
            '(e.g. source = "booking_calendar", mode = "blocks") -- cancel-entire-session '
            "blocker events would never hide a date",
        )

    def test_secret_files_exist_when_this_machine_has_the_secrets_dir(self):
        # Only meaningful where the secrets actually live (the server): on
        # a dev checkout the paths point at /etc/my-booking, which is not
        # there, and that is not a misconfiguration.
        paths = {
            "[booking_calendar].password_file": self.raw.get("booking_calendar", {}).get("password_file"),
            "[smtp].password_file": self.raw.get("smtp", {}).get("password_file"),
            "[admin].password_hash_file": self.raw.get("admin", {}).get("password_hash_file"),
            "[privacy].erasure_pepper_file": self.raw.get("privacy", {}).get("erasure_pepper_file"),
        }
        configured = [p for p in paths.values() if p]
        if not configured or not any(Path(p).parent.is_dir() for p in configured):
            self.skipTest("secrets directory not present on this machine (dev checkout)")
        for label, p in paths.items():
            with self.subTest(secret=label):
                self.assertIsNotNone(p, f"{label} is not configured")
                self.assertTrue(Path(p).is_file(), f"{label}: {p} does not exist")


if __name__ == "__main__":
    unittest.main()
