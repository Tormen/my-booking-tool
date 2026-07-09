import tempfile
import unittest
from pathlib import Path

from app import site_render


class RenderPrivacyHtmlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpl_path = Path(self._tmp.name) / "privacy.html.tmpl"
        self.tmpl_path.write_text(
            "<html><body>kept for ${retention_months} months "
            "(${canceled_retention_months} if canceled)</body></html>",
            encoding="utf-8",
        )

    def test_substitutes_both_placeholders(self):
        out = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        self.assertIn("kept for 24 months", out)
        self.assertIn("6 if canceled", out)

    def test_output_carries_managed_marker(self):
        out = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        self.assertIn("MANAGED BY my-bt", out)
        self.assertTrue(out.startswith(site_render.MANAGED_MARKER))

    def test_different_settings_produce_different_output(self):
        default = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        custom = site_render.render_privacy_html(self.tmpl_path, 36, 12)
        self.assertNotEqual(default, custom)
        self.assertIn("36 months", custom)

    def test_write_privacy_html_writes_rendered_content(self):
        out_path = Path(self._tmp.name) / "privacy.html"
        site_render.write_privacy_html(self.tmpl_path, 24, 6, out_path)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("kept for 24 months", content)
        self.assertIn("MANAGED BY my-bt", content)

    def test_write_privacy_html_leaves_no_temp_file_behind(self):
        # 2026-07-15: this is the live, publicly-served privacy.html --
        # goes through atomic_io.atomic_write_text (temp file + fsync +
        # rename), not a bare write_text().
        out_path = Path(self._tmp.name) / "privacy.html"
        site_render.write_privacy_html(self.tmpl_path, 24, 6, out_path)
        leftover_tmps = [p.name for p in Path(self._tmp.name).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftover_tmps, [])


if __name__ == "__main__":
    unittest.main()
