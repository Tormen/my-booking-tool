"""load_settings()'s own TOML-parsing behavior -- most other tests build a
Settings/Course directly via tests/helpers.py's make_settings/make_course,
bypassing the TOML file entirely. This file covers what only load_settings()
itself does: reading real secret files, and (2026-07-09: added a
sorting key allowing the ORDER of the courses on
https://booking.example.org/courses to be determined; the key was originally named
`order`, renamed to `order_in_all_courses` the same day to be
self-explanatory about how this 'order' is actually USED) sorting courses
by this optional key before
they ever reach Settings.courses."""
import tempfile
import unittest
from pathlib import Path

from app import config as app_config
from app.config import CourseDateOverride, load_settings

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

    def _write_with_calendar_extra(self, extra_calendar_lines: str) -> Path:
        toml_path = self._write(self._course_block("c1"))
        toml_path.write_text(toml_path.read_text().replace(
            'conflict_calendars = ["Bookings"]',
            'conflict_calendars = ["Bookings"]\n' + extra_calendar_lines,
        ))
        return toml_path

    def test_log_file_defaults_on_when_key_absent(self):
        # 2026-07-16 (operator's call): file logging is ON by default --
        # the watchdog's rate-limit-block alerting and the CSP-violation
        # health checks read only this file and are blind without it.
        settings = load_settings(self._write(self._course_block("c1")))
        self.assertEqual(settings.log_file, app_config.DEFAULT_LOG_FILE)

    def test_log_file_empty_string_disables_file_logging(self):
        toml_path = self._write(self._course_block("c1"))
        toml_path.write_text(toml_path.read_text() + '\n[logging]\nlog_file = ""\n')
        self.assertIsNone(load_settings(toml_path).log_file)

    def test_log_file_explicit_path_wins(self):
        toml_path = self._write(self._course_block("c1"))
        toml_path.write_text(toml_path.read_text() + '\n[logging]\nlog_file = "/tmp/custom.log"\n')
        self.assertEqual(load_settings(toml_path).log_file, "/tmp/custom.log")

    def test_all_day_conflict_settings_defaults(self):
        # 2026-07-16 (operator's call, same day the setting was born with
        # default false): all-day events in the conflict calendars DO
        # hide course dates by default; the title-marker escape hatch is
        # disabled ("") and the "show as Free" one is on.
        settings = load_settings(self._write(self._course_block("c1")))
        self.assertTrue(settings.conflict_calendar_all_day_events_also_block_the_course)
        self.assertEqual(settings.all_day_non_blocking_title_marker, "")
        self.assertTrue(settings.all_day_free_events_do_not_block)

    def test_all_day_conflict_settings_parse_non_default_values(self):
        settings = load_settings(self._write_with_calendar_extra(
            "conflict_calendar_all_day_events_also_block_the_course = false\n"
            'all_day_non_blocking_title_marker = "#course-ok"\n'
            "all_day_free_events_do_not_block = false"
        ))
        self.assertFalse(settings.conflict_calendar_all_day_events_also_block_the_course)
        self.assertEqual(settings.all_day_non_blocking_title_marker, "#course-ok")
        self.assertFalse(settings.all_day_free_events_do_not_block)

    def test_location_url_defaults_to_empty_string_when_omitted(self):
        # 2026-07-09: a location_url was added and used on /my in
        # the column location to make those clickable -- optional, so an
        # existing settings.toml with no location_url key anywhere must
        # keep parsing exactly as before.
        toml_path = self._write(self._course_block("no-url"))
        settings = load_settings(toml_path)
        self.assertEqual(settings.course("no-url").location_url, "")

    def test_location_url_is_parsed_when_present(self):
        # _course_block doesn't itself support location_url -- appended
        # directly onto the last [[course]] block's own key=value lines
        # (still inside that same table, since no new [[course]]/table
        # header follows) rather than extending that helper just for this
        # one test.
        toml_path = self._write(
            self._course_block("has-url")
            + '\nlocation_url = "https://maps.example.org/?q=Example+Room"\n'
        )
        settings = load_settings(toml_path)
        self.assertEqual(
            settings.course("has-url").location_url, "https://maps.example.org/?q=Example+Room",
        )

    def test_host_calendar_entry_cc_list_defaults_to_empty_tuple_when_omitted(self):
        # 2026-07-14: a list of email addresses that if set on a
        # course in settings.toml will also be invited as optional (cc)
        # -- optional, so an existing settings.toml with no
        # host_calendar_entry_cc_list key anywhere must keep parsing
        # exactly as before.
        toml_path = self._write(self._course_block("no-cc"))
        settings = load_settings(toml_path)
        self.assertEqual(settings.course("no-cc").host_calendar_entry_cc_list, ())

    def test_host_calendar_entry_cc_list_is_parsed_when_present(self):
        toml_path = self._write(
            self._course_block("has-cc")
            + '\nhost_calendar_entry_cc_list = ["a@example.org", "b@example.org"]\n'
        )
        settings = load_settings(toml_path)
        self.assertEqual(
            settings.course("has-cc").host_calendar_entry_cc_list, ("a@example.org", "b@example.org"),
        )


class LoadSettingsIndexEmbeddedTest(LoadSettingsCourseOrderTest):
    """[site].index_embedded_enabled / index_embedded_new_tab_links
    (2026-07-13) -- off/on-by-default when the keys are simply omitted
    (the common case: most deployments never touch either). Subclasses
    LoadSettingsCourseOrderTest purely to reuse its setUp() (secret files)
    -- same established pattern LoadSettingsDateOverrideTest below already
    uses."""

    def _write_with_site_extra(self, extra_site_lines: str) -> Path:
        toml_path = self.dir / "settings.toml"
        header = MINIMAL_HEADER.format(
            caldav_password_file=self.caldav_password_file,
            smtp_password_file=self.smtp_password_file,
            admin_password_hash_file=self.admin_password_hash_file,
            erasure_pepper_file=self.erasure_pepper_file,
        )
        header = header.replace(
            'base_url = "https://example.org"',
            'base_url = "https://example.org"\n' + extra_site_lines,
        )
        toml_path.write_text(header)
        return toml_path

    def test_defaults_when_keys_omitted(self):
        toml_path = self._write_with_site_extra("")
        settings = load_settings(toml_path)
        self.assertFalse(settings.index_embedded_enabled)
        self.assertTrue(settings.index_embedded_new_tab_links)

    def test_enabled_true_is_parsed(self):
        toml_path = self._write_with_site_extra("index_embedded_enabled = true\n")
        settings = load_settings(toml_path)
        self.assertTrue(settings.index_embedded_enabled)

    def test_new_tab_links_false_is_parsed(self):
        toml_path = self._write_with_site_extra(
            "index_embedded_enabled = true\nindex_embedded_new_tab_links = false\n"
        )
        settings = load_settings(toml_path)
        self.assertTrue(settings.index_embedded_enabled)
        self.assertFalse(settings.index_embedded_new_tab_links)


class LoadSettingsCalendarReminderMinutesTest(unittest.TestCase):
    """2026-07-07: the reminders (list) became a setting, defaulting
    to NO reminders (trainer's own CalDAV event) / invites to course
    participants have a reminder 1h before (guest_reminder_minutes)."""

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


class LoadSettingsBccAttendeeEmailsTest(unittest.TestCase):
    """2026-07-09: BCC a given email address on all mails
    that go out to the attendees, for a time, to ensure that all is OK
    -- optional, comma-separated [smtp]
    key; settings.bcc_attendee_email_list is what every attendee-facing
    send_mail() call site actually reads (see app/config.py's own
    docstring on both)."""

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

    def _write(self, extra_smtp_line: str = "") -> Path:
        toml_path = self.dir / "settings.toml"
        header = MINIMAL_HEADER.format(
            caldav_password_file=self.caldav_password_file,
            smtp_password_file=self.smtp_password_file,
            admin_password_hash_file=self.admin_password_hash_file,
            erasure_pepper_file=self.erasure_pepper_file,
        )
        header = header.replace(
            'from_address = "admin@example.org"',
            'from_address = "admin@example.org"\n' + extra_smtp_line,
        )
        toml_path.write_text(header)
        return toml_path

    def test_defaults_to_empty_string_and_empty_list_when_omitted(self):
        settings = load_settings(self._write())
        self.assertEqual(settings.bcc_attendee_emails, "")
        self.assertEqual(settings.bcc_attendee_email_list, ())

    def test_single_address_is_parsed(self):
        toml_path = self._write('bcc_attendee_emails = "watcher@example.org"\n')
        settings = load_settings(toml_path)
        self.assertEqual(settings.bcc_attendee_email_list, ("watcher@example.org",))

    def test_multiple_comma_separated_addresses_are_split_and_trimmed(self):
        toml_path = self._write(
            'bcc_attendee_emails = "watcher1@example.org, watcher2@example.org"\n'
        )
        settings = load_settings(toml_path)
        self.assertEqual(
            settings.bcc_attendee_email_list, ("watcher1@example.org", "watcher2@example.org"),
        )

    def test_blank_string_gives_empty_list_not_a_list_with_one_blank_entry(self):
        toml_path = self._write('bcc_attendee_emails = ""\n')
        settings = load_settings(toml_path)
        self.assertEqual(settings.bcc_attendee_email_list, ())


class LoadSettingsAccountDeletionWarningDaysTest(unittest.TestCase):
    """2026-07-09: the scheduler that deletes accounts should
    detect imminent accounts that would need to be deleted and then send
    out such an email -- optional [privacy] key, three equivalent ways
    to disable it (0, "", or omitted -- all fall through `or 0` in
    load_settings() to the same falsy value)."""

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

    def _write(self, extra_privacy_line: str = "") -> Path:
        toml_path = self.dir / "settings.toml"
        header = MINIMAL_HEADER.format(
            caldav_password_file=self.caldav_password_file,
            smtp_password_file=self.smtp_password_file,
            admin_password_hash_file=self.admin_password_hash_file,
            erasure_pepper_file=self.erasure_pepper_file,
        )
        header = header.replace(
            'erasure_pepper_file = "{erasure_pepper_file}"'.format(erasure_pepper_file=self.erasure_pepper_file),
            'erasure_pepper_file = "{erasure_pepper_file}"\n'.format(erasure_pepper_file=self.erasure_pepper_file)
            + extra_privacy_line,
        )
        toml_path.write_text(header)
        return toml_path

    def test_defaults_to_zero_when_omitted(self):
        settings = load_settings(self._write())
        self.assertEqual(settings.account_deletion_warning_days, 0)

    def test_explicit_zero_is_zero(self):
        toml_path = self._write("how_many_days_before_account_deletion_send_warning_mail = 0\n")
        settings = load_settings(toml_path)
        self.assertEqual(settings.account_deletion_warning_days, 0)

    def test_blank_string_is_zero(self):
        toml_path = self._write('how_many_days_before_account_deletion_send_warning_mail = ""\n')
        settings = load_settings(toml_path)
        self.assertEqual(settings.account_deletion_warning_days, 0)

    def test_positive_value_is_parsed(self):
        toml_path = self._write("how_many_days_before_account_deletion_send_warning_mail = 30\n")
        settings = load_settings(toml_path)
        self.assertEqual(settings.account_deletion_warning_days, 30)


class WeekdayTimeRangeLabelTest(unittest.TestCase):
    """Course.weekday_time_range_label() -- 2026-07-10: the
    weekday was added to the TIME column (e.g. SAT 10h45-12h45) on /my's bookings
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


class CourseDateOverrideTest(unittest.TestCase):
    """Course.date_overrides + its *_for() lookup helpers -- 2026-07-16:
    a new config option per course in settings.toml was added to
    exceptionally change time for a course on a certain date, optionally
    with a message, supporting a LIST of dates with
    different times."""

    def test_no_overrides_at_all_is_unaffected(self):
        course = make_course(weekday="sat", start_time="10:45", duration_minutes=120)
        self.assertIsNone(course.override_for("2026-07-18"))
        self.assertEqual(course.time_range_label_for("2026-07-18"), course.time_range_label())
        self.assertEqual(course.override_message_for("2026-07-18"), "")

    def test_matching_date_overrides_the_start_time_keeps_duration(self):
        # duration_minutes omitted on the override -- normal 120min length
        # is kept, only the start shifts (09:45 - 11:45, not 09:45 - 12:45).
        course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        self.assertEqual(course.time_range_label_for("2026-07-18"), "9h45 - 11h45")
        self.assertEqual(course.start_hm_for("2026-07-18"), (9, 45))
        self.assertEqual(course.end_hm_for("2026-07-18"), (11, 45))
        self.assertEqual(course.duration_minutes_for("2026-07-18"), 120)

    def test_override_can_also_change_duration(self):
        course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45", duration_minutes=60),),
        )
        self.assertEqual(course.time_range_label_for("2026-07-18"), "9h45 - 10h45")
        self.assertEqual(course.duration_minutes_for("2026-07-18"), 60)

    def test_non_matching_date_is_unaffected(self):
        course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120,
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        self.assertEqual(course.time_range_label_for("2026-07-25"), "10h45 - 12h45")
        self.assertIsNone(course.override_for("2026-07-25"))

    def test_message_is_optional_and_defaults_to_blank(self):
        course = make_course(
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        self.assertEqual(course.override_message_for("2026-07-18"), "")

    def test_message_is_returned_when_set(self):
        course = make_course(
            date_overrides=(
                CourseDateOverride(date="2026-07-18", start_time="09:45", message="I need to leave early."),
            ),
        )
        self.assertEqual(course.override_message_for("2026-07-18"), "I need to leave early.")

    def test_multiple_dates_are_looked_up_independently(self):
        course = make_course(
            weekday="sat", start_time="10:45", duration_minutes=120,
            date_overrides=(
                CourseDateOverride(date="2026-07-18", start_time="09:45", message="early one week"),
                CourseDateOverride(date="2026-08-01", start_time="14:00", message="late another week"),
            ),
        )
        self.assertEqual(course.time_range_label_for("2026-07-18"), "9h45 - 11h45")
        self.assertEqual(course.override_message_for("2026-07-18"), "early one week")
        self.assertEqual(course.time_range_label_for("2026-08-01"), "14h00 - 16h00")
        self.assertEqual(course.override_message_for("2026-08-01"), "late another week")
        # A third, unrelated date sees neither.
        self.assertEqual(course.time_range_label_for("2026-08-08"), "10h45 - 12h45")


class LoadSettingsDateOverrideTest(LoadSettingsCourseOrderTest):
    """Real end-to-end TOML parsing of `[[course.date_override]]` --
    reuses LoadSettingsCourseOrderTest's own setUp/_write (secrets +
    MINIMAL_HEADER) since date_override sub-tables need a real, fully
    loadable settings.toml, not just a bare Course() construction."""

    def _course_block_with_override(
        self, shortname: str, date: str, start_time: str,
        duration_minutes: int | None = None, message: str | None = None,
    ) -> str:
        override_lines = [
            "[[course.date_override]]",
            f'date = "{date}"',
            f'start_time = "{start_time}"',
        ]
        if duration_minutes is not None:
            override_lines.append(f"duration_minutes = {duration_minutes}")
        if message is not None:
            override_lines.append(f'message = "{message}"')
        return self._course_block(shortname) + "\n".join(override_lines) + "\n"

    def test_no_date_override_table_is_an_empty_tuple(self):
        toml_path = self._write(self._course_block("plain"))
        settings = load_settings(toml_path)
        self.assertEqual(settings.course("plain").date_overrides, ())

    def test_date_override_with_message_parses(self):
        toml_path = self._write(self._course_block_with_override(
            "trier", date="2026-07-18", start_time="09:45", message="I need to be in Kaiserslautern before 13h.",
        ))
        settings = load_settings(toml_path)
        overrides = settings.course("trier").date_overrides
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].date, "2026-07-18")
        self.assertEqual(overrides[0].start_time, "09:45")
        self.assertEqual(overrides[0].message, "I need to be in Kaiserslautern before 13h.")
        self.assertIsNone(overrides[0].duration_minutes)

    def test_date_override_message_is_optional(self):
        toml_path = self._write(self._course_block_with_override("trier", date="2026-07-18", start_time="09:45"))
        settings = load_settings(toml_path)
        self.assertEqual(settings.course("trier").date_overrides[0].message, "")

    def test_date_override_duration_minutes_is_optional_and_parsed_when_present(self):
        toml_path = self._write(self._course_block_with_override(
            "trier", date="2026-07-18", start_time="09:45", duration_minutes=60,
        ))
        settings = load_settings(toml_path)
        self.assertEqual(settings.course("trier").date_overrides[0].duration_minutes, 60)

    def test_multiple_date_overrides_on_the_same_course(self):
        block = self._course_block("trier")
        block += '[[course.date_override]]\ndate = "2026-07-18"\nstart_time = "09:45"\n'
        block += '[[course.date_override]]\ndate = "2026-08-01"\nstart_time = "14:00"\n'
        toml_path = self._write(block)
        settings = load_settings(toml_path)
        overrides = settings.course("trier").date_overrides
        self.assertEqual([o.date for o in overrides], ["2026-07-18", "2026-08-01"])


class CoursesFromRawTest(LoadSettingsDateOverrideTest):
    """courses_from_raw() (2026-07-16, factored out of load_settings() so a
    raw-dict-only caller -- e.g. app/site_render.py's index_embedded.html
    rendering, app/cli_checks.py's health checks -- can get real Course
    objects without needing every secret file to exist first) must parse
    IDENTICALLY to what load_settings() itself produces for the exact same
    input -- reuses LoadSettingsDateOverrideTest's own
    secrets+MINIMAL_HEADER+date_override fixture so both can be loaded from
    one file."""

    def test_matches_load_settings_courses_for_the_same_file(self):
        from app.config import courses_from_raw, load_raw_toml

        toml_path = self._write(self._course_block_with_override(
            "trier", date="2026-07-18", start_time="09:45", message="Back at 13h.",
        ))
        settings = load_settings(toml_path)
        raw = load_raw_toml(toml_path)
        self.assertEqual(courses_from_raw(raw), settings.courses)

    def test_duplicate_shortname_raises(self):
        from app.config import courses_from_raw

        raw = {"course": [
            {"shortname": "dup", "title": "A", "location": "L", "weekday": "mon",
             "start_time": "10:00", "duration_minutes": 60, "capacity": 5},
            {"shortname": "dup", "title": "B", "location": "L", "weekday": "tue",
             "start_time": "10:00", "duration_minutes": 60, "capacity": 5},
        ]}
        with self.assertRaises(ValueError):
            courses_from_raw(raw)

    def test_no_course_table_is_an_empty_tuple(self):
        from app.config import courses_from_raw

        self.assertEqual(courses_from_raw({}), ())


class TodayInRawTimezoneTest(unittest.TestCase):
    def test_uses_configured_timezone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.config import today_in_raw_timezone

        raw = {"site": {"timezone": "Europe/Berlin"}}
        expected = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
        self.assertEqual(today_in_raw_timezone(raw), expected)

    def test_falls_back_to_utc_when_missing(self):
        from datetime import datetime, timezone

        from app.config import today_in_raw_timezone

        expected = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(today_in_raw_timezone({}), expected)


class UpcomingDateOverridesTest(unittest.TestCase):
    def test_filters_past_dates(self):
        from app.config import upcoming_date_overrides

        course = make_course(
            shortname="trier",
            date_overrides=(
                CourseDateOverride(date="2026-07-01", start_time="09:00"),
                CourseDateOverride(date="2026-07-18", start_time="09:45", message="Back at 13h."),
            ),
        )
        items = upcoming_date_overrides([course], "2026-07-10")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2026-07-18")
        self.assertEqual(items[0]["course_shortname"], "trier")
        self.assertEqual(items[0]["message"], "Back at 13h.")

    def test_sorted_by_date_then_shortname(self):
        from app.config import upcoming_date_overrides

        course_a = make_course(shortname="b-course", date_overrides=(
            CourseDateOverride(date="2026-08-01", start_time="09:00"),
        ))
        course_b = make_course(shortname="a-course", date_overrides=(
            CourseDateOverride(date="2026-08-01", start_time="09:00"),
        ))
        course_c = make_course(shortname="z-course", date_overrides=(
            CourseDateOverride(date="2026-07-20", start_time="09:00"),
        ))
        items = upcoming_date_overrides([course_a, course_b, course_c], "2026-07-10")
        self.assertEqual(
            [(it["date"], it["course_shortname"]) for it in items],
            [("2026-07-20", "z-course"), ("2026-08-01", "a-course"), ("2026-08-01", "b-course")],
        )

    def test_no_overrides_is_empty_list(self):
        from app.config import upcoming_date_overrides

        self.assertEqual(upcoming_date_overrides([make_course()], "2026-07-10"), [])


if __name__ == "__main__":
    unittest.main()
