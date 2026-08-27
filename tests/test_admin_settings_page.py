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
