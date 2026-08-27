"""app/version.py -- `my-bt --version`'s two-source commit resolution
(baked GIT_COMMIT file from an RPM build, falling back to a live
`git rev-parse` on a dev checkout, falling back to a clear "unknown").
2026-07-14 (repo-review): the one app/ module with no test file at all --
cheap to cover, and `--version` crashing would be exactly the wrong
command to ever crash."""
import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.version import PACKAGE_VERSION, _UNKNOWN, git_commit, version_string


class GitCommitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_baked_git_commit_file_wins(self):
        # An installed system (no .git dir) reads the file build-rpm.sh
        # baked in -- never shells out to git at all in that case.
        (self.home / "GIT_COMMIT").write_text("abc123def456\n", encoding="utf-8")
        with mock.patch("app.version.subprocess.run") as run:
            self.assertEqual(git_commit(str(self.home)), "abc123def456")
            run.assert_not_called()

    def test_empty_baked_file_falls_through_to_live_git(self):
        (self.home / "GIT_COMMIT").write_text("", encoding="utf-8")
        fake = subprocess.CompletedProcess([], returncode=0, stdout="deadbeef0000\n", stderr="")
        with mock.patch("app.version.subprocess.run", return_value=fake):
            self.assertEqual(git_commit(str(self.home)), "deadbeef0000")

    def test_unknown_when_neither_source_works(self):
        # No GIT_COMMIT file, and `home` isn't a git checkout either
        # (a plain temp dir) -- must return the clear placeholder, never
        # raise (`--version` should never be the one command that crashes).
        self.assertEqual(git_commit(str(self.home)), _UNKNOWN)

    def test_git_binary_missing_entirely_is_still_unknown_not_a_crash(self):
        with mock.patch("app.version.subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(git_commit(str(self.home)), _UNKNOWN)


class VersionStringTest(unittest.TestCase):
    def test_contains_package_version_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "GIT_COMMIT").write_text("cafe00112233\n", encoding="utf-8")
            s = version_string(tmp)
        self.assertEqual(s, f"my-booking-tool {PACKAGE_VERSION} (commit cafe00112233)")

    def test_the_baked_source_stamp_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "GIT_COMMIT").write_text("cafe00112233\n", encoding="utf-8")
            (Path(tmp) / "SOURCE_STAMP").write_text("2026-08-27_0813\n", encoding="utf-8")
            s = version_string(tmp)
        self.assertEqual(
            s,
            f"my-booking-tool {PACKAGE_VERSION} "
            "(source 2026-08-27_0813 UTC, commit cafe00112233)",
        )

    def test_an_empty_stamp_file_falls_back_rather_than_showing_a_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "GIT_COMMIT").write_text("cafe00112233\n", encoding="utf-8")
            (Path(tmp) / "SOURCE_STAMP").write_text("\n", encoding="utf-8")
            # Nothing source-like in this temp dir, so nothing to compute
            # from either -- the string must still be well formed.
            self.assertNotIn("source  UTC", version_string(tmp))


class ShortVersionOnEveryPageTest(unittest.TestCase):
    """Every app-rendered page carries the build it came from (2026-08-27,
    the operator: "in case someone sends me a screenshot, which --version
    did this refer to?"). Without it, a screenshot cannot be tied to a
    build and every report costs a round-trip to ask."""

    def test_the_page_shell_carries_it(self):
        from app.templates import page
        body = page("Title", "<p>hi</p>")
        self.assertIn('class="version"', body)
        self.assertIn(PACKAGE_VERSION, body)

    def test_it_says_the_same_thing_as_the_cli(self):
        # The page and `my-bt --version` must never disagree about which
        # build is running -- they read the same two files.
        from app import version as v
        short = v.short_version()
        self.assertTrue(short.startswith(PACKAGE_VERSION), short)
        stamp = v.source_stamp(v._HOME)
        if stamp:
            self.assertIn(stamp, short)

    def test_index_html_is_untouched(self):
        # index.html is a hand-authored static page, not rendered through
        # this shell -- the operator asked for the line on every page
        # EXCEPT that one, and it gets it for free by not going through
        # page() at all.
        from app import templates
        self.assertNotIn("short_version", templates.page.__doc__ or "")


class SourceStampTest(unittest.TestCase):
    """compute_source_stamp() dates the CODE, not the build. The two
    properties that matter, and that a build clock gets wrong: rebuilding
    untouched source reports the SAME string, and touching one source
    file moves it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "app").mkdir()
        self.src = self.root / "app" / "webapp.py"
        self.src.write_text("x", encoding="utf-8")
        os.utime(self.src, (1_756_000_000, 1_756_000_000))

    def test_untouched_source_gives_a_stable_stamp(self):
        from app.version import compute_source_stamp
        self.assertEqual(
            compute_source_stamp(self.root), compute_source_stamp(self.root)
        )

    def test_editing_a_source_file_moves_it(self):
        from app.version import compute_source_stamp
        before = compute_source_stamp(self.root)
        os.utime(self.src, (1_756_000_000 + 7200, 1_756_000_000 + 7200))
        self.assertNotEqual(compute_source_stamp(self.root), before)

    def test_regenerated_artefacts_do_not_move_it(self):
        # site/privacy.html and site/index_embedded.html are re-rendered by
        # every single build. If they counted, the stamp would tick on its
        # own and mean nothing -- which is the exact failure this whole
        # rework exists to fix.
        from app.version import compute_source_stamp
        before = compute_source_stamp(self.root)
        (self.root / "site").mkdir()
        generated = self.root / "site" / "privacy.html"
        generated.write_text("generated", encoding="utf-8")
        os.utime(generated, (1_900_000_000, 1_900_000_000))
        self.assertEqual(compute_source_stamp(self.root), before)

    def test_a_site_page_edit_moves_it(self):
        # 2026-08-27, second miss on this stamp: site/ was left out of the
        # whitelist entirely, so a CSS fix to site/index.html -- a real,
        # shipped, visible change -- did not move the version at all.
        from app.version import compute_source_stamp
        before = compute_source_stamp(self.root)
        (self.root / "site").mkdir()
        page = self.root / "site" / "index.html"
        page.write_text("<html>", encoding="utf-8")
        os.utime(page, (1_756_000_000 + 7200, 1_756_000_000 + 7200))
        self.assertNotEqual(compute_source_stamp(self.root), before)

    def test_the_two_regenerated_site_pages_still_do_not_move_it(self):
        # ...but excluding all of site/ to be rid of these two was the
        # wrong cut. They are excluded BY NAME instead.
        from app.version import compute_source_stamp
        before = compute_source_stamp(self.root)
        (self.root / "site").mkdir()
        for name in ("privacy.html", "index_embedded.html"):
            generated = self.root / "site" / name
            generated.write_text("generated", encoding="utf-8")
            os.utime(generated, (1_900_000_000, 1_900_000_000))
        self.assertEqual(compute_source_stamp(self.root), before)

    def test_nothing_to_read_is_none_not_a_crash(self):
        from app.version import compute_source_stamp
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(compute_source_stamp(empty))


if __name__ == "__main__":
    unittest.main()
