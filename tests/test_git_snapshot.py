"""Tests app/git_snapshot.py -- the hourly auto-commit of the CSV data
directory into its own, separate git repo. Uses a REAL temp git repo
(tempfile.TemporaryDirectory() + real `git` via subprocess), not a mocked
`run`, for the most convincing coverage of the actual staged-diff/commit
behavior -- matches this project's existing preference for real
files/subprocesses over heavy mocking (see tests/test_cli_setup.py,
tests/test_cli_checks.py)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import git_snapshot


def _git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(data_dir), *args], capture_output=True, text=True)


def _commit_count(data_dir: Path) -> int:
    result = _git(data_dir, "log", "--oneline")
    if result.returncode != 0:
        return 0
    return len([ln for ln in result.stdout.strip().splitlines() if ln.strip()])


class SnapshotNotARepoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_plain_directory_reports_not_a_repo_and_creates_nothing(self):
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")
        result = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(result.status, "not_a_repo")
        # snapshot() must NOT create a .git itself -- that's setup's job
        # (see app/git_snapshot.py's module docstring).
        self.assertFalse((self.data_dir / ".git").exists())


class SnapshotRealRepoTest(unittest.TestCase):
    """Real git repo, real subprocess -- init + configure identity here
    (setUp), the same responsibility split as `my-bt setup -i`'s own git
    init step (see app/cli_setup.py), so snapshot() itself is only ever
    exercised against an already-initialized repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        _git(self.data_dir, "init")
        _git(self.data_dir, "config", "user.email", "test@example.com")
        _git(self.data_dir, "config", "user.name", "Test")

    def test_commits_when_there_are_real_changes(self):
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")
        result = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(result.status, "committed")
        self.assertEqual(_commit_count(self.data_dir), 1)
        show = _git(self.data_dir, "show", "--stat", "HEAD")
        self.assertIn("users.csv", show.stdout)
        log = _git(self.data_dir, "log", "-1", "--pretty=%s")
        self.assertIn("automatic snapshot:", log.stdout)

    def test_does_nothing_when_nothing_changed(self):
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")
        first = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(first.status, "committed")
        head_after_first = _git(self.data_dir, "rev-parse", "HEAD").stdout.strip()

        second = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(second.status, "no_changes")
        self.assertEqual(_commit_count(self.data_dir), 1)
        head_after_second = _git(self.data_dir, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head_after_first, head_after_second)

    def test_modifying_a_tracked_file_and_rerunning_commits_again(self):
        p = self.data_dir / "users.csv"
        p.write_text("user_id,email\n1,a@b.com\n")
        first = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(first.status, "committed")

        p.write_text("user_id,email\n1,a@b.com\n2,c@d.com\n")
        second = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(second.status, "committed")
        self.assertEqual(_commit_count(self.data_dir), 2)

    def test_first_run_with_no_files_at_all_is_no_changes(self):
        # A freshly-initialized, still-empty repo -- nothing to add, so
        # `git diff --cached --quiet` sees no staged difference at all.
        result = git_snapshot.snapshot(self.data_dir)
        self.assertEqual(result.status, "no_changes")
        self.assertEqual(_commit_count(self.data_dir), 0)

    def test_custom_message_is_used_verbatim_instead_of_auto_generated_one(self):
        # `my-bt admin git-snapshot -m "..."` -- see app/git_snapshot.py's
        # snapshot() docstring: a custom message replaces the "automatic
        # snapshot: <timestamp>" text entirely rather than being appended
        # to it, since git already records a real commit timestamp on its
        # own.
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")
        result = git_snapshot.snapshot(self.data_dir, message="before the settings.toml rewrite")
        self.assertEqual(result.status, "committed")
        self.assertEqual(result.detail, "before the settings.toml rewrite")
        log = _git(self.data_dir, "log", "-1", "--pretty=%s")
        self.assertEqual(log.stdout.strip(), "before the settings.toml rewrite")

    def test_no_custom_message_falls_back_to_auto_generated_one(self):
        (self.data_dir / "users.csv").write_text("user_id,email\n1,a@b.com\n")
        result = git_snapshot.snapshot(self.data_dir, message=None)
        self.assertIn("automatic snapshot:", result.detail)


if __name__ == "__main__":
    unittest.main()
