"""Nothing that must stay out of this repository is tracked in it.

The repo's own .gitignore carries two rules (`*.local`, `*.local.*`) and
nothing else. Ignore rules are silent and only stop an accidental
`git add`; they say nothing about a file that was force-added, staged
explicitly by tooling, or committed before a rule existed. This test is
the loud half, and unlike a local hook it travels with the repo -- it runs
in every clone and in the RPM's own %check.

scripts/check-repo-hygiene.sh holds the actual rules; the same script
backs .git/hooks/pre-commit (see scripts/install-git-hooks.sh), so a rule
is added in exactly one place.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-repo-hygiene.sh"


class RepoHygieneTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None or not (_REPO_ROOT / ".git").exists():
            # An unpacked source tarball (the RPM build) has the script but
            # no git checkout to inspect -- nothing to assert, not a failure.
            self.skipTest("not a git checkout")

    def test_no_forbidden_path_is_tracked(self):
        result = subprocess.run(
            ["sh", str(_SCRIPT)], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_the_guard_actually_rejects_something(self):
        """A guard that cannot fail is not a guard. Runs the same script
        against a throwaway repository holding one file of each forbidden
        kind, and one that must survive."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
            for rel in ("settings.toml", "site/index.html", "app/__pycache__/x.pyc",
                        "data/registrations.csv", "secrets/caldav_password",
                        ".DS_Store", "notes.local", "NOTES.local.md",
                        "site/index.html.example", "app/webapp.py"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "-Af", "."], cwd=root, check=True,
                           capture_output=True)
            result = subprocess.run(
                ["sh", str(_SCRIPT)], cwd=root, capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            for expected in ("settings.toml", "site/index.html", "x.pyc",
                             "data/registrations.csv", "secrets/caldav_password",
                             ".DS_Store", "notes.local", "NOTES.local.md"):
                with self.subTest(expected=expected):
                    self.assertIn(expected, result.stderr)
            # ...and the tracked, publishable files are not swept up with them.
            self.assertNotIn("index.html.example", result.stderr)
            self.assertNotIn("webapp.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
