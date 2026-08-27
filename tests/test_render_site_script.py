"""Tests scripts/render-site.py's resolve_real_or_example() -- the
mechanism that lets a fresh clone of the public template repo (which only
has settings.toml.example / site/*.html.example) still build, while a
real deployment's real files are ALWAYS preferred and NEVER read-modified
or deleted. This is the single most safety-critical function added for
"make this sharable" -- the explicit requirement was that the operator's
real, customized files must never be erased or silently replaced by the
generic examples, so this is tested directly and thoroughly.

scripts/render-site.py has no package (`scripts/` isn't a Python package,
and unlike `scripts/my-bt` it does have a .py extension but lives outside
`app/`) -- import it via importlib from its file path, the same
workaround its own hyphenated filename would need anywhere.
"""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "render_site_script",
    Path(__file__).resolve().parent.parent / "scripts" / "render-site.py",
)
render_site_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_site_script)


class ResolveRealOrExampleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_prefers_real_file_when_present(self):
        real = self.dir / "settings.toml"
        example = self.dir / "settings.toml.example"
        real.write_text("real content -- the operator's own", encoding="utf-8")
        example.write_text("generic placeholder", encoding="utf-8")
        resolved = render_site_script.resolve_real_or_example(real)
        self.assertEqual(resolved, real)
        # Never touched, either file:
        self.assertEqual(real.read_text(), "real content -- the operator's own")
        self.assertEqual(example.read_text(), "generic placeholder")

    def test_falls_back_to_example_when_real_absent(self):
        real = self.dir / "settings.toml"
        example = self.dir / "settings.toml.example"
        example.write_text("generic placeholder", encoding="utf-8")
        resolved = render_site_script.resolve_real_or_example(real)
        self.assertEqual(resolved, example)
        self.assertFalse(real.exists())  # never created as a side effect

    def test_raises_if_neither_exists(self):
        real = self.dir / "settings.toml"
        with self.assertRaises(FileNotFoundError):
            render_site_script.resolve_real_or_example(real)

    def test_real_file_is_never_deleted_or_modified_by_resolution(self):
        real = self.dir / "site" / "privacy.html.tmpl"
        real.parent.mkdir()
        real.write_text("the operator's real, customized wording", encoding="utf-8")
        before = real.stat().st_mtime_ns
        render_site_script.resolve_real_or_example(real)
        render_site_script.resolve_real_or_example(real)  # calling it again changes nothing
        self.assertTrue(real.exists())
        self.assertEqual(real.stat().st_mtime_ns, before)
        self.assertEqual(real.read_text(), "the operator's real, customized wording")


_SAMPLE_INDEX_HTML = """<html><body>
<div class="top-bar" id="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
<div id="schedule-exceptions"></div>
<ul><li><a href="/book/trier">Book your place here.</a></li></ul>
<script>(function () { fetch('/my/session', { credentials: 'same-origin' }); })();</script>
<script>(function () { fetch('/schedule-exceptions', { credentials: 'same-origin' }); })();</script>
</body></html>
"""


class RenderTest(unittest.TestCase):
    """render() (reworked 2026-07-13: index_embedded.html is now DERIVED
    from site/index.html itself, not rendered from its own separate
    .tmpl) end-to-end, against a real REPO_ROOT-relative settings.toml +
    index.html pair -- covers the "falls back to .example when nothing
    real exists" path that's this project's own actual state when checked
    out fresh."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "site").mkdir()
        self.settings_path = self.dir / "settings.toml"
        self.settings_path.write_text(
            "[privacy]\nretention_months = 24\ncanceled_retention_months = 6\n"
            "[site]\ntimezone = \"UTC\"\n"
            "[[course]]\n"
            'shortname = "trier"\ntitle = "Yoga"\nlocation = "Trier"\nweekday = "sat"\n'
            'start_time = "10:45"\nduration_minutes = 120\ncapacity = 10\n'
            "[[course.date_override]]\n"
            'date = "2099-01-01"\nstart_time = "09:45"\nmessage = "Back at 13h."\n',
            encoding="utf-8",
        )
        (self.dir / "site" / "privacy.html.tmpl").write_text(
            "kept for {{!retention_months}} months", encoding="utf-8",
        )
        (self.dir / "site" / "index.html").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")

    def test_renders_privacy_html_and_derives_index_embedded_html(self):
        with patch.object(render_site_script, "REPO_ROOT", self.dir):
            written, values = render_site_script.render(self.settings_path)
        self.assertIn("site/privacy.html", written)
        self.assertIn("site/index_embedded.html", written)
        self.assertEqual(values["retention_months"], 24)
        privacy_out = (self.dir / "site" / "privacy.html").read_text()
        self.assertIn("kept for 24 months", privacy_out)
        embedded_out = (self.dir / "site" / "index_embedded.html").read_text()
        self.assertIn("ATTENTION", embedded_out)
        self.assertIn("Back at 13h.", embedded_out)
        self.assertNotIn("<script>", embedded_out)

    def test_falls_back_to_example_index_html(self):
        (self.dir / "site" / "index.html").unlink()
        (self.dir / "site" / "index.html.example").write_text(_SAMPLE_INDEX_HTML, encoding="utf-8")
        with patch.object(render_site_script, "REPO_ROOT", self.dir):
            written, _values = render_site_script.render(self.settings_path)
        self.assertIn("site/index_embedded.html", written)
        self.assertIn("ATTENTION", (self.dir / "site" / "index_embedded.html").read_text())

    def test_derivation_error_is_a_warning_not_a_fatal_build_failure(self):
        # A customized real index.html missing something this derivation
        # depends on must never break scripts/build-rpm.sh outright.
        broken = _SAMPLE_INDEX_HTML.replace('href="/my"', 'href="/my-account"')
        (self.dir / "site" / "index.html").write_text(broken, encoding="utf-8")
        err = io.StringIO()
        with patch.object(render_site_script, "REPO_ROOT", self.dir), contextlib.redirect_stderr(err):
            written, _values = render_site_script.render(self.settings_path)
        self.assertIn("site/privacy.html", written)
        self.assertNotIn("site/index_embedded.html", written)
        # The warning itself is real and expected (this is the whole point
        # of the test) -- just keep it out of the test runner's own stderr,
        # same as every other CLI-output test in this suite.
        self.assertIn("could not derive site/index_embedded.html", err.getvalue())


if __name__ == "__main__":
    unittest.main()
