"""app/version.py -- `my-bt --version`'s two-source commit resolution
(baked GIT_COMMIT file from an RPM build, falling back to a live
`git rev-parse` on a dev checkout, falling back to a clear "unknown").
2026-07-14 (repo-review): the one app/ module with no test file at all --
cheap to cover, and `--version` crashing would be exactly the wrong
command to ever crash."""
import subprocess
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


if __name__ == "__main__":
    unittest.main()
