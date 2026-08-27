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
        (self.dir / config.WEB_EDITABLE_FILENAME).write_text(text)

    # -- absent ----------------------------------------------------------
    def test_without_the_file_nothing_changes(self):
        settings = config.load_settings(self.toml)
        self.assertEqual([c.shortname for c in settings.courses], ["yoga"])
        self.assertEqual(settings.macros, {})

    def test_the_path_sits_beside_settings_toml(self):
        self.assertEqual(config.web_editable_path(self.toml).parent, self.dir)
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
        self.assertEqual(path, self.dir / "settings.web-editable.toml")

    def test_the_header_says_the_file_is_rewritten(self):
        config.write_web_editable(self.toml, {}, (self.course,))
        text = (self.dir / "settings.web-editable.toml").read_text()
        self.assertIn("WRITTEN BY /admin", text)
        self.assertIn("REWRITES IT WHOLE", text)

    def test_a_value_that_cannot_be_serialised_raises_before_writing(self):
        with self.assertRaises(TypeError):
            config.write_web_editable(self.toml, {"bad": 3.5}, ())
        self.assertFalse((self.dir / "settings.web-editable.toml").exists())

    def test_an_empty_macro_table_writes_no_section(self):
        config.write_web_editable(self.toml, {}, (self.course,))
        self.assertNotIn("[macros]", (self.dir / "settings.web-editable.toml").read_text())
