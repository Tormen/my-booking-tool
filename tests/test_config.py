"""load_settings()'s own TOML-parsing behavior -- most other tests build a
Settings/Course directly via tests/helpers.py's make_settings/make_course,
bypassing the TOML file entirely. This file covers what only load_settings()
itself does: reading real secret files, and (2026-07-09, the operator: "add a
sorting key ... allowing me to determine the ORDER of the courses on
https://booking.example.org/courses"; the key was originally named `order`, renamed
to `order_in_all_courses` the same day -- the operator: "please rename to
something like order_in_all_courses to be self-explanatory how this
'order' is actually USED") sorting courses by this optional key before
they ever reach Settings.courses."""
import tempfile
import unittest
from pathlib import Path

from app.config import load_settings

from .helpers import make_course

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

    def _course_block(self, shortname: str, order_in_all_courses: int | None = None) -> str:
        extra = f"order_in_all_courses = {order_in_all_courses}\n" if order_in_all_courses is not None else ""
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
        # Backward compatibility: an existing settings.toml with no
        # `order_in_all_courses` keys anywhere must not have its course
        # list silently reordered.
        toml_path = self._write(
            self._course_block("third")
            + self._course_block("first")
            + self._course_block("second")
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["third", "first", "second"])

    def test_explicit_order_overrides_file_position(self):
        toml_path = self._write(
            self._course_block("c-last", order_in_all_courses=30)
            + self._course_block("c-first", order_in_all_courses=10)
            + self._course_block("c-middle", order_in_all_courses=20)
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["c-first", "c-middle", "c-last"])

    def test_order_defaults_to_zero_and_sorts_before_any_positive_order(self):
        toml_path = self._write(
            self._course_block("has-order", order_in_all_courses=5)
            + self._course_block("no-order")
        )
        settings = load_settings(toml_path)
        self.assertEqual([c.shortname for c in settings.courses], ["no-order", "has-order"])
        self.assertEqual(settings.course("no-order").order_in_all_courses, 0)

    def test_ties_at_the_same_order_fall_back_to_file_order(self):
        toml_path = self._write(
            self._course_block("tie-a", order_in_all_courses=10)
            + self._course_block("tie-b", order_in_all_courses=5)
            + self._course_block("tie-c", order_in_all_courses=10)
        )
        settings = load_settings(toml_path)
        # tie-b (order 5) first, then tie-a/tie-c (both order 10) in their
        # original relative file order -- stable sort, not re-shuffled.
        self.assertEqual([c.shortname for c in settings.courses], ["tie-b", "tie-a", "tie-c"])


class LoadSettingsCalendarReminderMinutesTest(unittest.TestCase):
    """2026-07-07, the operator: "make the reminders (list) a setting. But default
    to NO reminders" (trainer's own CalDAV event) / "invites to course
    participants should have reminder 1h before" (guest_reminder_minutes)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.caldav_password_file = self.dir / "caldav_password"
        self.smtp_password_file = self.dir / "smtp_password"
        self.admin_password_hash_file = self.dir / "admin_password_hash"
        self.erasure_pepper_file = self.dir / "erasure_pepper"
        for p in (self.caldav_password_file, self.smtp_password_file, self.admin_password_hash_file):
            p.write_text("secret")
        self.erasure_pepper_file.write_text("00" * 32)

    def _write(self, extra_calendar_lines: str = "") -> Path:
        toml_path = self.dir / "settings.toml"
        header = MINIMAL_HEADER.format(
            caldav_password_file=self.caldav_password_file,
            smtp_password_file=self.smtp_password_file,
            admin_password_hash_file=self.admin_password_hash_file,
            erasure_pepper_file=self.erasure_pepper_file,
        )
        # extra_calendar_lines gets appended right after the [calendar]
        # section's own conflict_calendars line, still inside that table.
        header = header.replace(
            'conflict_calendars = ["Bookings"]',
            'conflict_calendars = ["Bookings"]\n' + extra_calendar_lines,
        )
        toml_path.write_text(header)
        return toml_path

    def test_defaults_are_no_trainer_reminder_and_one_hour_guest_reminder(self):
        settings = load_settings(self._write())
        self.assertEqual(settings.trainer_calendar_reminder_minutes, ())
        self.assertEqual(settings.guest_calendar_reminder_minutes, (60,))

    def test_explicit_values_are_read_from_the_calendar_section(self):
        toml_path = self._write(
            "trainer_reminder_minutes = [30]\nguest_reminder_minutes = [15, 60]\n"
        )
        settings = load_settings(toml_path)
        self.assertEqual(settings.trainer_calendar_reminder_minutes, (30,))
        self.assertEqual(settings.guest_calendar_reminder_minutes, (15, 60))


class WeekdayTimeRangeLabelTest(unittest.TestCase):
    """Course.weekday_time_range_label() -- 2026-07-10, the operator: "add the
    weekday to the TIME column (e.g. SAT 10h45-12h45)" on /my's bookings
    table (app/webapp.py). Tighter than time_range_label() (no spaces
    around the dash) -- deliberately a separate method rather than
    changing time_range_label() itself, which the booking page subtitle
    also uses with its own spaced style ("10h45 - 12h45")."""

    def test_formats_weekday_and_compact_time_range(self):
        course = make_course(weekday="sat", start_time="10:45", duration_minutes=120)
        self.assertEqual(course.weekday_time_range_label(), "SAT 10h45-12h45")

    def test_uppercases_the_stored_lowercase_weekday_code(self):
        course = make_course(weekday="wed", start_time="17:15", duration_minutes=100)
        self.assertEqual(course.weekday_time_range_label(), "WED 17h15-18h55")

    def test_minutes_always_zero_padded(self):
        course = make_course(weekday="fri", start_time="12:00", duration_minutes=60)
        self.assertEqual(course.weekday_time_range_label(), "FRI 12h00-13h00")


if __name__ == "__main__":
    unittest.main()
