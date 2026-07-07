"""load_settings()'s own TOML-parsing behavior -- most other tests build a
Settings/Course directly via tests/helpers.py's make_settings/make_course,
bypassing the TOML file entirely. This file covers what only load_settings()
itself does: reading real secret files, and (2026-07-09, the operator: "add a
sorting key ... allowing me to determine the ORDER of the courses on
https://booking.example.org/courses") sorting courses by their optional `order` key
before they ever reach Settings.courses."""
import tempfile
import unittest
from pathlib import Path

from app.config import load_settings

MINIMAL_HEADER = """
[site]
timezone = "Europe/Berlin"
admin_email = "admin@example.org"
base_url = "https://example.org"

[calendar]
caldav_url = "https://dav.example.org/"
caldav_username = "calendar@example.org"
caldav_password_file = "{caldav_password_file}"
booking_calendar = "Bookings"
conflict_calendars = ["Bookings"]

[smtp]
host = "smtp.example.org"
port = 465
username = "calendar@example.org"
password_file = "{smtp_password_file}"
from_address = "admin@example.org"

[admin]
password_hash_file = "{admin_password_hash_file}"

[privacy]
erasure_pepper_file = "{erasure_pepper_file}"
"""


class LoadSettingsCourseOrderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # _read_secret() insists these exist and are non-empty.
        self.caldav_password_file = self.dir / "caldav_password"
        self.smtp_password_file = self.dir / "smtp_password"
        self.admin_password_hash_file = self.dir / "admin_password_hash"
        self.erasure_pepper_file = self.dir / "erasure_pepper"
        for p in (self.caldav_password_file, self.smtp_password_file, self.admin_password_hash_file):
            p.write_text("secret")
        self.erasure_pepper_file.write_text("00" * 32)

    def _write(self, course_blocks: str) -> Path:
        toml_path = self.dir / "settings.toml"
        header = MINIMAL_HEADER.format(
            caldav_password_file=self.caldav_password_file,
            smtp_password_file=self.smtp_password_file,
            admin_password_hash_file=self.admin_password_hash_file,
            erasure_pepper_file=self.erasure_pepper_file,
        )
        toml_path.write_text(header + course_blocks)
        return toml_path

    def _course_block(self, shortname: str, order: int | None = None) -> str:
        extra = f"order = {order}\n" if order is not None else ""
        return f"""
[[course]]
shortname = "{shortname}"
title = "{shortname}"
location = "Example Room"
weekday = "mon"
start_time = "18:00"
duration_minutes = 60
capacity = 10
{extra}"""

    def test_courses_with_no_order_keep_settings_toml_file_order(self):
        # Backward compatibility: an existing settings.toml with no `order`
        # keys anywhere must not have its course list silently reordered.
        toml_path = self._write(
            self._course_block("third")
            + self._course_block("first")
            + self._course_block("second")
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["third", "first", "second"])

    def test_explicit_order_overrides_file_position(self):
        toml_path = self._write(
            self._course_block("c-last", order=30)
            + self._course_block("c-first", order=10)
            + self._course_block("c-middle", order=20)
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["c-first", "c-middle", "c-last"])

    def test_order_defaults_to_zero_and_sorts_before_any_positive_order(self):
        toml_path = self._write(
            self._course_block("has-order", order=5)
            + self._course_block("no-order")
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["no-order", "has-order"])
        self.assertEqual(settings.course("no-order").order, 0)

    def test_ties_at_the_same_order_fall_back_to_file_order(self):
        toml_path = self._write(
            self._course_block("tie-a", order=10)
            + self._course_block("tie-b", order=5)
            + self._course_block("tie-c", order=10)
        )
        settings = load_settings(toml_path)
        # tie-b (order 5) first, then tie-a/tie-c (both order 10) in their
        # original relative file order -- stable sort, not re-shuffled.
        self.assertEqual([c.shortname for c in settings.courses], ["tie-b", "tie-a", "tie-c"])


if __name__ == "__main__":
    unittest.main()
