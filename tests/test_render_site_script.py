"""Tests scripts/render-site.py's resolve_real_or_example() -- the
mechanism that lets a fresh clone of the public template repo (which only
has settings.toml.example / site/*.html.example) still build, while a
real deployment's real files are ALWAYS preferred and NEVER read-modified
or deleted. This is the single most safety-critical function added for
"make this sharable" -- the operator's explicit requirement was that his real,
customized files must never be erased or silently replaced by the
generic examples, so this is tested directly and thoroughly.

scripts/render-site.py has no package (`scripts/` isn't a Python package,
and unlike `scripts/my-bt` it does have a .py extension but lives outside
`app/`) -- import it via importlib from its file path, the same
workaround its own hyphenated filename would need anywhere.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
