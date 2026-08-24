"""The `*.local/` overlay directory: app/local_overlay.py plus
app.cli_checks.check_local_overlay(). Everything here runs against a
throwaway tree, never this checkout's own overlay."""
import tempfile
import unittest
from pathlib import Path

from app import cli_checks, local_overlay


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FindOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_no_overlay_at_all_is_none_not_an_error(self):
        self.assertIsNone(local_overlay.find(self.home))

    def test_finds_a_single_overlay_whatever_its_name(self):
        (self.home / "anything-at-all.local").mkdir()
        found = local_overlay.find(self.home)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "anything-at-all.local")

    def test_two_overlays_raise_rather_than_picking_one(self):
        (self.home / "a.local").mkdir()
        (self.home / "b.local").mkdir()
        with self.assertRaises(local_overlay.LocalOverlayError):
            local_overlay.find(self.home)

    def test_a_local_named_FILE_is_not_mistaken_for_an_overlay(self):
        _touch(self.home / "notes.local")
        self.assertIsNone(local_overlay.find(self.home))

    def test_a_hidden_dotfile_is_never_matched(self):
        (self.home / ".update-LINKS.local").mkdir()
        self.assertIsNone(local_overlay.find(self.home))


class SourceAndOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.overlay = self.home / "my-booking.local"
        self.overlay.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_source_returns_the_overlay_copy_when_it_holds_the_file(self):
        _touch(self.overlay / "site" / "index.html", "real")
        found = local_overlay.source(self.home, "site/index.html")
        self.assertIsNotNone(found)
        self.assertEqual(found.read_text(encoding="utf-8"), "real")

    def test_source_is_none_when_the_overlay_lacks_that_file(self):
        self.assertIsNone(local_overlay.source(self.home, "settings.toml"))

    def test_source_is_none_without_an_overlay_so_callers_fall_through(self):
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        self.assertIsNone(local_overlay.source(plain, "settings.toml"))

    def test_generated_files_are_written_into_the_overlay_when_there_is_one(self):
        out = local_overlay.output(self.home, "site/privacy.html")
        self.assertEqual(out, self.overlay / "site" / "privacy.html")
        self.assertTrue(out.parent.is_dir())          # created for the caller

    def test_generated_files_keep_their_ordinary_path_without_an_overlay(self):
        plain = Path(tempfile.mkdtemp())
        out = local_overlay.output(plain, "site/privacy.html")
        self.assertEqual(out, plain / "site" / "privacy.html")


class CheckLocalOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _levels(self):
        return {level for _, level, _ in cli_checks.check_local_overlay(self.home)}

    def test_no_overlay_reports_nothing_at_all(self):
        # What a fresh clone and every installed system look like.
        self.assertEqual(cli_checks.check_local_overlay(self.home), [])

    def test_two_overlays_are_a_hard_failure(self):
        (self.home / "a.local").mkdir()
        (self.home / "b.local").mkdir()
        self.assertIn("fail", self._levels())

    def test_a_complete_overlay_is_ok(self):
        overlay = self.home / "my-booking.local"
        for rel, _ in cli_checks._OVERLAY_REAL_FILES:
            _touch(overlay / rel)
        self.assertEqual(self._levels(), {"ok"})

    def test_the_same_file_in_both_places_warns_about_the_stale_copy(self):
        overlay = self.home / "my-booking.local"
        for rel, _ in cli_checks._OVERLAY_REAL_FILES:
            _touch(overlay / rel)
        _touch(self.home / "site" / "index.html")     # left behind by a half-done move
        checks = cli_checks.check_local_overlay(self.home)
        both = [c for c in checks if c[0] == "local overlay (site/index.html)"]
        self.assertEqual(len(both), 1)
        self.assertEqual(both[0][1], "warn")
        self.assertIn("BOTH", both[0][2])

    def test_a_missing_real_file_warns_that_the_example_would_be_packaged(self):
        (self.home / "my-booking.local").mkdir()
        _touch(self.home / "settings.toml.example")
        checks = cli_checks.check_local_overlay(self.home)
        missing = [c for c in checks if c[0] == "local overlay (settings.toml)"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0][1], "warn")
        self.assertIn("settings.toml.example", missing[0][2])

    def test_a_generated_file_with_no_example_is_not_reported_as_missing(self):
        # site/privacy.html is rendered from privacy.html.tmpl; there is no
        # placeholder for it, so "would package the .example" cannot apply.
        (self.home / "my-booking.local").mkdir()
        labels = [label for label, _, _ in cli_checks.check_local_overlay(self.home)]
        self.assertNotIn("local overlay (site/privacy.html)", labels)


if __name__ == "__main__":
    unittest.main()
