"""settings.web-editable.toml -- the console-writable half of the config.

The split exists for one reason: settings.toml holds a CalDAV username,
three paths to secret files and the admin password hash, and no web
process should ever be able to write that file. These tests pin the
properties that make the split real rather than decorative."""
import tempfile
import unittest
from pathlib import Path

from app import config


BASE = """
[site]
timezone = "Europe/Luxembourg"
admin_email = "admin@example.org"
base_url = "https://booking.example.org"

[booking_calendar]
caldav_url = "https://dav.example.org/caldav/"
username = "cal@example.org"
password_file = "{secrets}/caldav"
calendar = "Calendar"

[smtp]
host = "smtp.example.org"
port = 587
username = "smtp@example.org"
password_file = "{secrets}/smtp"
from_address = "no-reply@example.org"

[admin]
password_hash_file = "{secrets}/admin"

[privacy]
erasure_pepper_file = "{secrets}/pepper"

[[course]]
shortname = "yoga"
title = "Yoga"
location = "Studio"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 10
"""


class WebEditableSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)   # read with fromhex()
        self.toml = self.dir / "settings.toml"
        self.toml.write_text(BASE.format(secrets=secrets))

    def _editable(self, text: str) -> None:
        path = config.web_editable_path(self.toml)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # -- absent ----------------------------------------------------------
    def test_without_the_file_nothing_changes(self):
        settings = config.load_settings(self.toml)
        self.assertEqual([c.shortname for c in settings.courses], ["yoga"])
        self.assertEqual(settings.macros, {})

    def test_the_path_sits_beside_settings_toml(self):
        # In its own writable sub-directory: /etc/my-booking itself stays
        # read-only to the service, because the secrets are in there.
        self.assertEqual(config.web_editable_path(self.toml).parent,
                         self.dir / "web-editable")
        self.assertEqual(config.web_editable_path(self.toml).name,
                         "settings.web-editable.toml")

    # -- macros ----------------------------------------------------------
    def test_macros_are_read_from_it(self):
        self._editable('[macros]\nstudio = "Ayur Yoga"\n')
        self.assertEqual(config.load_settings(self.toml).macros, {"studio": "Ayur Yoga"})

    def test_a_name_that_cannot_be_one_is_refused_at_load(self):
        # The console blocks these while typing; this is the backstop for
        # a hand-edited file.
        for bad in ('2nd = "x"', '"with space" = "x"', '"$dyn" = "x"',
                    '"' + "a" * 21 + '" = "x"'):
            with self.subTest(bad=bad):
                self._editable(f"[macros]\n{bad}\n")
                with self.assertRaises(ValueError):
                    config.load_settings(self.toml)

    def test_a_system_macro_cannot_be_used_in_this_file(self):
        # {{!x}} reads settings.toml. A value here is writable through the
        # browser, so allowing it would let console access publish a
        # secret path on a public page.
        self._editable('[macros]\nleak = "see {{!password_hash_file}}"\n')
        with self.assertRaises(ValueError) as caught:
            config.load_settings(self.toml)
        self.assertIn("system macros", str(caught.exception))

    def test_a_dynamic_or_user_macro_in_a_value_is_fine(self):
        self._editable('[macros]\na = "hi {{b}}"\nb = "there"\n')
        self.assertEqual(config.load_settings(self.toml).macros["a"], "hi {{b}}")

    # -- courses ---------------------------------------------------------
    def test_a_course_only_it_defines_is_added_after_the_others(self):
        self._editable('''
[[course]]
shortname = "pilates"
title = "Pilates"
location = "Hall"
weekday = "fri"
start_time = "09:00"
duration_minutes = 45
capacity = 8
''')
        self.assertEqual([c.shortname for c in config.load_settings(self.toml).courses],
                         ["yoga", "pilates"])

    def test_it_wins_per_shortname_without_reordering(self):
        self._editable('''
[[course]]
shortname = "yoga"
title = "Yoga, edited in the console"
location = "Studio"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 12
''')
        courses = config.load_settings(self.toml).courses
        self.assertEqual([c.shortname for c in courses], ["yoga"])
        self.assertEqual(courses[0].title, "Yoga, edited in the console")
        self.assertEqual(courses[0].capacity, 12)

    def test_a_broken_file_raises_naming_it(self):
        self._editable("[macros\nbroken = ")
        with self.assertRaises(ValueError) as caught:
            config.load_settings(self.toml)
        self.assertIn("settings.web-editable.toml", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class WriteWebEditableTest(unittest.TestCase):
    """The console owns this file, so it writes it whole -- and reads it
    back before letting the write stand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.toml = self.dir / "settings.toml"
        self.course = config.Course(
            shortname="yoga", title="Yoga", location="Studio", weekday="wed",
            start_time="18:00", duration_minutes=60, capacity=10,
        )

    def test_what_it_writes_parses_back_to_what_went_in(self):
        rich = config.Course(
            shortname="trier", title='A "quoted" title', location="Hall",
            weekday="sat", start_time="10:45", duration_minutes=120, capacity=14,
            description='<p>One<br>two</p>\nthird line',
        )
        config.write_web_editable(self.toml, {"studio": "Ayur Yoga"}, (self.course, rich))
        raw = config.load_web_editable(self.toml)
        self.assertEqual(config.macros_from_raw(raw), {"studio": "Ayur Yoga"})
        courses = config.courses_from_raw(raw)
        self.assertEqual([c.shortname for c in courses], ["yoga", "trier"])
        self.assertEqual(courses[1].title, 'A "quoted" title')
        self.assertEqual(courses[1].description, '<p>One<br>two</p>\nthird line')

    def test_it_writes_beside_settings_toml(self):
        path = config.write_web_editable(self.toml, {}, (self.course,))
        self.assertEqual(path, self.dir / "web-editable" / "settings.web-editable.toml")

    def test_the_header_says_the_file_is_rewritten(self):
        config.write_web_editable(self.toml, {}, (self.course,))
        text = config.web_editable_path(self.toml).read_text()
        self.assertIn("WRITTEN BY /admin", text)
        self.assertIn("REWRITES IT WHOLE", text)

    def test_a_value_that_cannot_be_serialised_raises_before_writing(self):
        with self.assertRaises(TypeError):
            config.write_web_editable(self.toml, {"bad": 3.5}, ())
        self.assertFalse(config.web_editable_path(self.toml).exists())

    def test_an_empty_macro_table_writes_no_section(self):
        config.write_web_editable(self.toml, {}, (self.course,))
        self.assertNotIn("[macros]", config.web_editable_path(self.toml).read_text())


class HealthCheckTest(unittest.TestCase):
    """`my-bt admin health` on the console-writable file. The service
    keeps its last known good config when this file will not load --
    correct, and silent, so the check is where that becomes visible."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)
        self.toml = self.dir / "settings.toml"
        self.toml.write_text(BASE.format(secrets=secrets))

    def _check(self):
        from app import cli_checks
        return cli_checks.check_web_editable_settings(self.toml)

    def _editable(self, text: str) -> None:
        path = config.web_editable_path(self.toml)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_no_file_is_not_a_finding(self):
        self.assertEqual(self._check(), [])

    def test_a_good_file_reports_what_it_holds(self):
        self._editable('[macros]\nstudio = "S"\n')
        (label, level, detail), = self._check()
        self.assertEqual(level, "ok")
        self.assertIn("1 macro(s)", detail)

    def test_a_file_that_does_not_parse_fails_loudly(self):
        self._editable("[macros\nbroken")
        (_label, level, detail), = self._check()
        self.assertEqual(level, "fail")
        self.assertIn("last good config", detail)

    def test_an_invalid_value_fails_too(self):
        # Parses as TOML, refused by the loader: the service is equally
        # running on old config, so it is equally a failure.
        self._editable('[macros]\n"2nd" = "x"\n')
        (_label, level, _detail), = self._check()
        self.assertEqual(level, "fail")

    def test_a_course_defined_in_both_files_is_reported(self):
        self._editable('''
[[course]]
shortname = "yoga"
title = "Yoga, from the console"
location = "Studio"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 10
''')
        levels = {level for _l, level, _d in self._check()}
        self.assertIn("warn", levels)
        detail = [d for _l, level, d in self._check() if level == "warn"][0]
        self.assertIn("yoga", detail)
        self.assertIn("wins", detail)


class MacroExpansionInCoursesTest(unittest.TestCase):
    """A macro used in a course text must reach the page EXPANDED.

    2026-08-28, from the live site: the console saved {{cancel_please}}
    into a description and the booking page served those braces
    verbatim. Everything existed -- the file, the engine, the console --
    and nothing called the engine when rendering a course."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)
        self.toml = self.dir / "settings.toml"
        self.toml.write_text(BASE.format(secrets=secrets))

    def _editable(self, text: str) -> None:
        path = config.web_editable_path(self.toml)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _course(self):
        return config.load_settings(self.toml).courses[0]

    def test_a_macro_in_the_description_renders_as_markup(self):
        self._editable('''
[macros]
hint = "cancel under <a href=\\"https://x/my\\">x/my</a>"

[[course]]
shortname = "yoga"
title = "Yoga"
location = "Studio"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 10
description = "<p>Cannot come? {{hint}}</p>"
''')
        description = self._course().description
        self.assertNotIn("{{hint}}", description)
        self.assertIn('<a href="https://x/my">', description)

    def test_a_macro_in_a_plain_field_is_reduced_to_its_text(self):
        # A title reaches an escaped field, a calendar SUMMARY and an
        # email subject -- none of which can show markup.
        self._editable('''
[macros]
studio = "Ayur <b>Yoga</b>"

[[course]]
shortname = "yoga"
title = "Class at {{studio}}"
location = "{{studio}}"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 10
''')
        course = self._course()
        self.assertEqual(course.title, "Class at Ayur Yoga")
        self.assertNotIn("<b>", course.location)

    def test_a_course_without_macros_is_untouched(self):
        self._editable('[macros]\nstudio = "S"\n')
        self.assertEqual(self._course().title, "Yoga")

    def test_an_unknown_macro_is_loud_rather_than_silent(self):
        # The console refuses to save one, so this means a hand-edit --
        # and a macro vanishing from a booking page is the worse outcome.
        self._editable('''
[macros]
studio = "S"

[[course]]
shortname = "yoga"
title = "Class at {{typo}}"
location = "Studio"
weekday = "wed"
start_time = "18:00"
duration_minutes = 60
capacity = 10
''')
        with self.assertRaises(Exception):
            config.load_settings(self.toml)


class CommentOutSupersededCoursesTest(unittest.TestCase):
    """The offered cleanup for a course defined in both files.

    Legal, but the settings.toml block is dead config that still looks
    live: edit it and nothing happens. Offered by `my-bt admin setup`
    only -- it writes settings.toml, the file the console must never be
    able to touch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.toml = Path(self._tmp.name) / "settings.toml"
        self.toml.write_text('''[site]
timezone = "UTC"

# a note of my own, above the block
[[course]]
shortname = "yoga"
title = "Yoga"
capacity = 10

[[course.date_override]]
date = "2026-09-05"
message = "starts early"

[[course]]
shortname = "pilates"
title = "Pilates"

[smtp]
host = "x"
''')

    def _parsed(self) -> dict:
        import tomllib
        return tomllib.loads(self.toml.read_text())

    def test_it_removes_only_the_named_course(self):
        done = config.comment_out_superseded_courses(self.toml, ["yoga"])
        self.assertEqual(done, ["yoga"])
        self.assertEqual([c["shortname"] for c in self._parsed()["course"]], ["pilates"])

    def test_the_courses_own_date_override_goes_with_it(self):
        # It belongs to the block above it; left behind it would attach
        # to the NEXT course, silently.
        config.comment_out_superseded_courses(self.toml, ["yoga"])
        self.assertNotIn("date_override", self._parsed()["course"][0])

    def test_every_other_section_survives(self):
        config.comment_out_superseded_courses(self.toml, ["yoga"])
        parsed = self._parsed()
        self.assertEqual(parsed["site"]["timezone"], "UTC")
        self.assertEqual(parsed["smtp"]["host"], "x")

    def test_the_text_is_kept_and_dated_not_deleted(self):
        config.comment_out_superseded_courses(self.toml, ["yoga"], now_stamp="2026-08-28_0130")
        text = self.toml.read_text()
        self.assertIn('# shortname = "yoga"', text)
        self.assertIn("2026-08-28_0130: commented out", text)
        self.assertIn("# a note of my own, above the block", text)

    def test_all_of_them_at_once(self):
        done = config.comment_out_superseded_courses(self.toml, ["yoga", "pilates"])
        self.assertEqual(sorted(done), ["pilates", "yoga"])
        self.assertEqual(self._parsed().get("course", []), [])

    def test_an_unknown_name_changes_nothing(self):
        before = self.toml.read_text()
        self.assertEqual(config.comment_out_superseded_courses(self.toml, ["nope"]), [])
        self.assertEqual(self.toml.read_text(), before)
