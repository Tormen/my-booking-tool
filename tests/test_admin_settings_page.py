"""/admin/settings -- the macros console.

The page is the mockup made real; what matters here is the behaviour
behind it: who may reach it, what a save writes, and the refusals that
keep the console out of the locked settings file."""
import io
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import config, webapp
from app.storage import Store
from app.webapp import App
from .helpers import make_course, make_settings
from .test_web_editable_settings import BASE as BASE_SETTINGS


class AdminSettingsPageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # A real settings.toml, so the reload after a save is genuinely
        # exercised rather than falling back to last-known-good.
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)
        (self.dir / "settings.toml").write_text(BASE_SETTINGS.format(secrets=secrets))
        self.store = Store(str(self.dir / "data"))
        settings = make_settings(courses=(make_course(shortname="yoga", weekday="sat"),))
        self.settings = config.replace(settings, macros={"studio": "Ayur Yoga"})
        self.app = App(self.settings, self.store, settings_path=str(self.dir / "settings.toml"))
        self.admin = {"HTTP_COOKIE": f"session={webapp._new_session({'kind': 'admin'})}"}

    def _post(self, form, environ=None):
        body = urlencode(form).encode()
        env = dict(environ if environ is not None else self.admin)
        env.update({"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})
        return self.app.admin_settings_macro("POST", env)

    def _written(self) -> dict:
        return config.load_web_editable(self.dir / "settings.toml")

    # -- access ----------------------------------------------------------
    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        status, headers, _body = self.app.admin_settings("GET", {})
        self.assertEqual(status, "302 Found")
        self.assertIn(("Location", "/admin/login"), headers)

    def test_a_guest_session_is_not_an_admin_session(self):
        user = self.store.upsert_user_for_booking("g@example.org", "G")
        env = {"HTTP_COOKIE":
               f"session={webapp._new_session({'kind': 'guest', 'user_id': user.user_id})}"}
        status, _headers, _body = self.app.admin_settings("GET", env)
        self.assertEqual(status, "302 Found")

    def test_saving_requires_post(self):
        status, headers, _body = self.app.admin_settings_macro("GET", self.admin)
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertIn(("Allow", "POST"), headers)

    # -- saving ----------------------------------------------------------
    def test_adding_a_macro_writes_the_web_editable_file_only(self):
        self._post({"old_name": "", "name": "gym", "value": "Clearstream Gym", "action": "save"})
        self.assertEqual(self._written()["macros"]["gym"], "Clearstream Gym")
        # The locked file is untouched: that is the whole point of the split.
        self.assertNotIn("gym", (self.dir / "settings.toml").read_text())

    def test_a_saved_macro_is_live_without_a_restart(self):
        self._post({"old_name": "", "name": "gym", "value": "Clearstream Gym", "action": "save"})
        self.assertEqual(self.app.settings.macros.get("gym"), "Clearstream Gym")

    def test_removing_a_macro(self):
        self._post({"old_name": "studio", "name": "studio", "value": "x", "action": "remove"})
        self.assertNotIn("studio", self._written().get("macros", {}))

    def test_renaming_rewrites_every_use_of_the_old_name(self):
        self._post({"old_name": "", "name": "where", "value": "at {{studio}}", "action": "save"})
        self._post({"old_name": "studio", "name": "studio_trier",
                    "value": "Ayur Yoga", "action": "save"})
        macros = self._written()["macros"]
        self.assertEqual(macros["where"], "at {{studio_trier}}")
        self.assertNotIn("studio", macros)

    def test_a_duplicate_name_is_refused_and_changes_nothing(self):
        self._post({"old_name": "", "name": "gym", "value": "A", "action": "save"})
        status, headers, _b = self._post(
            {"old_name": "gym", "name": "studio", "value": "A", "action": "save"})
        self.assertEqual(status, "302 Found")
        self.assertIn("err=", dict(headers)["Location"])
        self.assertEqual(self._written()["macros"]["studio"], "Ayur Yoga")

    def test_an_impossible_name_is_refused(self):
        _s, headers, _b = self._post({"old_name": "", "name": "2nd", "value": "x", "action": "save"})
        self.assertIn("err=", dict(headers)["Location"])

    def test_a_system_macro_cannot_be_saved_into_a_value(self):
        # It reads settings.toml, and this file is writable from a browser.
        _s, headers, _b = self._post({"old_name": "", "name": "leak",
                                      "value": "{{!password_hash_file}}", "action": "save"})
        self.assertIn("err=", dict(headers)["Location"])
        self.assertNotIn("leak", self._written().get("macros", {}))

    def test_markup_is_sanitized_on_save_and_the_page_says_what_went(self):
        _s, headers, _b = self._post({"old_name": "", "name": "note", "action": "save",
                                      "value": "hi<script>alert(1)</script><b>there</b>"})
        self.assertEqual(self._written()["macros"]["note"], "hi<b>there</b>")
        self.assertIn("msg=", dict(headers)["Location"])

    # -- rendering -------------------------------------------------------
    def test_the_page_shows_the_macros_and_the_courses(self):
        _s, _h, body = self.app.admin_settings("GET", self.admin)
        self.assertIn("<h2>Macros</h2>", body)
        self.assertIn("<h2>Courses</h2>", body)
        self.assertIn('id="macro-studio"', body)
        self.assertIn("yoga", body)

    def test_macro_values_reach_the_preview_as_data_not_as_script(self):
        # Splicing operator text into a <script> body would change the
        # script's CSP hash on every edit, and put un-escaped text one
        # quote away from being markup.
        _s, _h, body = self.app.admin_settings("GET", self.admin)
        self.assertIn('<div id="macro-values" hidden>', body)
        script = body[body.index("<script>"):]
        self.assertNotIn("Ayur Yoga", script)


if __name__ == "__main__":
    unittest.main()


class AdminSettingsCourseTest(unittest.TestCase):
    """Editing a course from the console, including the one field that is
    a data migration rather than a field edit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)
        (self.dir / "settings.toml").write_text(BASE_SETTINGS.format(secrets=secrets))
        self.store = Store(str(self.dir / "data"))
        settings = make_settings(courses=(make_course(shortname="yoga", weekday="wed"),))
        self.app = App(settings, self.store, settings_path=str(self.dir / "settings.toml"))
        # No CalDAV here: the calendar half of a rename has its own tests
        # in test_calendar_sync.py.
        self.app._href = lambda name: "/caldav/Calendar/"
        self.app._client = lambda: None
        self.admin = {"HTTP_COOKIE": f"session={webapp._new_session({'kind': 'admin'})}"}

    def _save(self, **overrides):
        form = {"old_shortname": "yoga", "shortname": "yoga", "title": "Yoga",
                "location": "Studio", "weekday": "wed", "start_time": "18:00",
                "duration_minutes": "60", "capacity": "10", "audience": "private",
                "order_in_all_courses": "0", "subtitle": "", "description": ""}
        form.update(overrides)
        body = urlencode(form).encode()
        env = dict(self.admin)
        env.update({"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})
        return self.app.admin_settings_course("POST", env)

    def _course(self, shortname="yoga"):
        return self.app.settings.course(shortname)

    def test_a_field_edit_is_written_and_live(self):
        self._save(title="Yoga, renamed", capacity="12")
        self.assertEqual(self._course().title, "Yoga, renamed")
        self.assertEqual(self._course().capacity, 12)
        written = config.load_web_editable(self.dir / "settings.toml")
        self.assertEqual(written["course"][0]["capacity"], 12)

    def test_the_locked_file_is_never_written(self):
        before = (self.dir / "settings.toml").read_text()
        self._save(title="Changed")
        self.assertEqual((self.dir / "settings.toml").read_text(), before)

    def test_a_bad_value_is_refused_and_nothing_is_written(self):
        for bad in ({"capacity": "many"}, {"start_time": "25:00"},
                    {"weekday": "funday"}, {"audience": "secret"}):
            with self.subTest(bad=bad):
                _s, headers, _b = self._save(**bad)
                self.assertIn("err=", dict(headers)["Location"])
        self.assertFalse((self.dir / "settings.web-editable.toml").exists())

    def test_description_markup_is_sanitized(self):
        self._save(description="ok<script>alert(1)</script><b>bold</b>")
        self.assertEqual(self._course().description, "ok<b>bold</b>")

    def test_a_system_macro_is_refused_in_a_plain_field(self):
        _s, headers, _b = self._save(title="{{!password_hash_file}}")
        self.assertIn("err=", dict(headers)["Location"])

    def _console_owned_course(self):
        """A course that lives ONLY in the web-editable file -- the only
        kind whose key the console may move."""
        courses = self.app.settings.courses + (make_course(shortname="pilates", weekday="fri"),)
        self.app.settings = config.replace(self.app.settings, courses=courses)
        config.write_web_editable(self.dir / "settings.toml", {}, courses[1:])
        self.app._reload_settings_file()

    def test_renaming_a_console_owned_course_moves_the_bookings(self):
        self._console_owned_course()
        self.store.add_registration_checking_capacity(
            "pilates", "2026-09-04", "user-1", "hash", 10)
        _s, headers, _b = self._save(old_shortname="pilates", shortname="pilates-fri",
                                     weekday="fri")
        self.assertIsNotNone(self._course("pilates-fri"))
        self.assertIsNone(self._course("pilates"))
        rows = self.store.read_registrations(scope="all")
        self.assertEqual([r["course_shortname"] for r in rows], ["pilates-fri"])
        # The operator is told what moved, not just that it saved.
        self.assertIn("booking", dict(headers)["Location"])

    def test_renaming_a_course_from_the_locked_file_is_refused(self):
        # It would exist twice afterwards: settings.toml still defines it
        # under the old name, and this console cannot write that file.
        _s, headers, _b = self._save(shortname="yoga-wed")
        location = dict(headers)["Location"]
        self.assertIn("err=", location)
        self.assertIn("settings.toml", location)
        self.assertIsNotNone(self._course("yoga"))
        self.assertIsNone(self._course("yoga-wed"))

    def test_a_rename_onto_an_existing_shortname_is_refused(self):
        courses = self.app.settings.courses + (make_course(shortname="taken"),)
        self.app.settings = config.replace(self.app.settings, courses=courses)
        _s, headers, _b = self._save(shortname="taken")
        self.assertIn("err=", dict(headers)["Location"])
        self.assertIsNotNone(self._course("yoga"))

    def test_an_uppercase_shortname_is_refused(self):
        _s, headers, _b = self._save(shortname="Yoga")
        self.assertIn("err=", dict(headers)["Location"])

    def test_a_calendar_failure_does_not_undo_the_data_move(self):
        self._console_owned_course()
        # Best-effort by design: a booking row that moved with an event
        # that did not is a stale entry in the operator's own calendar --
        # visible and fixable. Config and data disagreeing is not.
        self.store.add_registration_checking_capacity(
            "pilates", "2026-09-04", "user-1", "hash", 10)
        def boom(name):
            raise RuntimeError("caldav down")
        self.app._href = boom
        _s, headers, _b = self._save(old_shortname="pilates", shortname="pilates-fri",
                                     weekday="fri")
        rows = self.store.read_registrations(scope="all")
        self.assertEqual([r["course_shortname"] for r in rows], ["pilates-fri"])
        self.assertIn("CALENDAR", dict(headers)["Location"])


class PatternsAreValidInBrowsersTest(unittest.TestCase):
    """Every pattern= must compile under the `v` flag.

    2026-08-28, from the live console: Firefox refused
    pattern="[a-z0-9][a-z0-9-]*" with "character class escape cannot be
    used in class range" -- browsers now compile pattern= as a unicode-
    sets regex, where a `-` following a range must be escaped or come
    first. The field then validated NOTHING, silently."""

    def _patterns(self):
        """From the RENDERED page, not the source: in the source these
        live inside f-strings, where the escape is doubled -- checking
        that text would be checking Python's view, not the browser's."""
        import io
        import re
        import tempfile
        from pathlib import Path
        from app import webapp
        from app.storage import Store
        from .helpers import make_course, make_settings
        tmp = tempfile.mkdtemp()
        app = webapp.App(make_settings(courses=(make_course(shortname="yoga"),)),
                         Store(tmp), settings_path=str(Path(tmp) / "settings.toml"))
        env = {"HTTP_COOKIE": f"admin_session={webapp._new_session({'kind': 'admin'})}"}
        _s, _h, body = app.admin_settings("GET", env)
        return re.findall(r'pattern="([^"]+)"', body)

    def test_there_are_patterns_to_check(self):
        self.assertTrue(self._patterns())

    def test_every_hyphen_inside_a_class_is_escaped(self):
        """Under the `v` flag a literal `-` is RESERVED anywhere in a
        character class, not merely after a range -- the first fix moved
        it to the front and Firefox still refused it ("invalid character
        in class"). Escaped is the only form that compiles."""
        import re
        for pattern in self._patterns():
            for body in re.findall(r"\[([^\]]*)\]", pattern):
                with self.subTest(pattern=pattern, cls=body):
                    stripped = re.sub(r"\\.", "", body)          # drop escapes
                    stripped = re.sub(r"\w-\w", "", stripped)      # keep real ranges
                    self.assertNotIn("-", stripped,
                                     "a literal - in a class must be written \\-")

    def test_they_still_compile_in_python(self):
        import re
        for pattern in self._patterns():
            with self.subTest(pattern=pattern):
                re.compile(pattern)


class AdminBannerReadsTheAdminSessionTest(unittest.TestCase):
    """The banner must look at the ADMIN cookie.

    2026-08-28, from the live site: with both cookies present -- which is
    the whole point of separating them -- the banner found the GUEST
    session first and announced "Not logged in" on a page that was
    plainly serving admin content."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.app = App(make_settings(courses=(make_course(shortname="yoga"),)), self.store)
        self.user = self.store.upsert_user_for_booking("g@example.org", "G")

    def _cookies(self, **kinds) -> dict:
        parts = []
        if "guest" in kinds:
            parts.append("session=" + webapp._new_session(
                {"kind": "guest", "user_id": self.user.user_id}))
        if "admin" in kinds:
            parts.append("admin_session=" + webapp._new_session({"kind": "admin"}))
        return {"HTTP_COOKIE": "; ".join(parts)}

    def test_admin_only(self):
        self.assertIn("Admin", self.app._admin_banner_html(self._cookies(admin=1)))

    def test_both_cookies_still_says_admin(self):
        banner = self.app._admin_banner_html(self._cookies(guest=1, admin=1))
        self.assertIn(">Admin<", banner)
        self.assertNotIn("Not logged in", banner)

    def test_guest_only_is_honestly_not_an_admin(self):
        self.assertIn("Not logged in", self.app._admin_banner_html(self._cookies(guest=1)))


class UnsavedChangesTest(unittest.TestCase):
    """Leaving a half-edited course must not lose it silently.

    The tabs are links, so clicking one is a full navigation -- an
    unsaved description was simply gone. The page now asks (save /
    discard / stay) for a click on a link IN the page, and carries where
    the operator was heading in a `next` field so Save lands them there."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        secrets = self.dir / "secrets"
        secrets.mkdir()
        for name in ("caldav", "smtp", "admin"):
            (secrets / name).write_text("x")
        (secrets / "pepper").write_text("00" * 32)
        (self.dir / "settings.toml").write_text(BASE_SETTINGS.format(secrets=secrets))
        self.store = Store(str(self.dir / "data"))
        self.app = App(make_settings(courses=(make_course(shortname="yoga", weekday="wed"),)),
                       self.store, settings_path=str(self.dir / "settings.toml"))
        self.admin = {"HTTP_COOKIE": f"admin_session={webapp._new_session({'kind': 'admin'})}"}

    def _save(self, **overrides):
        form = {"old_shortname": "yoga", "shortname": "yoga", "title": "Yoga",
                "location": "Studio", "weekday": "wed", "start_time": "18:00",
                "duration_minutes": "60", "capacity": "10", "audience": "private",
                "order_in_all_courses": "0", "subtitle": "", "description": ""}
        form.update(overrides)
        body = urlencode(form).encode()
        env = dict(self.admin)
        env.update({"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})
        return self.app.admin_settings_course("POST", env)

    def test_the_page_offers_the_three_choices(self):
        _s, _h, body = self.app.admin_settings("GET", self.admin)
        self.assertIn('id="unsaved-dialog"', body)
        for label in ("Yes, save and continue", "No, discard them", "Stay here"):
            self.assertIn(label, body)

    def test_save_returns_to_where_the_operator_was_heading(self):
        _s, headers, _b = self._save(next="/admin/settings?tab=other")
        self.assertEqual(dict(headers)["Location"], "/admin/settings?tab=other")

    def test_without_a_next_it_stays_on_the_saved_course(self):
        _s, headers, _b = self._save()
        self.assertIn("tab=yoga", dict(headers)["Location"])

    def test_a_next_outside_admin_is_refused(self):
        # It arrives in a form field, so it is an open redirect the
        # moment it is trusted.
        for target in ("https://evil.example/x", "//evil.example", "/my",
                       "javascript:alert(1)"):
            with self.subTest(target=target):
                _s, headers, _b = self._save(next=target)
                self.assertNotIn("evil", dict(headers)["Location"])
                self.assertNotIn("javascript", dict(headers)["Location"])
                self.assertTrue(dict(headers)["Location"].startswith("/admin/settings"))
