"""app/atomic_io.py -- the shared crash-safe write helper extracted from
app/storage.py's own _LockedCsv._atomic_write so every OTHER module that
writes a file to disk (config, secrets, marker/state files, rendered
static pages) gets the same temp-file+fsync+rename+dir-fsync pattern,
not just the CSV storage layer. 2026-07-15, the operator: "yes please ALL
writes linked to my-booking-tool, my-bt and the site."

See tests/test_storage.py::AtomicWriteDirFsyncIntegrationTest for the
CSV-specific integration point (a real _LockedCsv write calling this)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.atomic_io import atomic_write_text, fsync_dir


class FsyncDirTest(unittest.TestCase):
    """A bare "was os.fsync called" mock assertion would pass even if a
    bug fsynced the wrong fd (e.g. the just-renamed file again, instead
    of its directory) -- these tests resolve the real fd back to a path
    via /proc/self/fd (Linux-only, matches this app's only deployment
    target) to prove it's actually the DIRECTORY's fd, not just "fsync
    was called some number of times"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_path = Path(self._tmp.name)

    def test_fsyncs_the_directory_fd_specifically(self):
        # Resolve the fd back to a path via /proc/self/fd WHILE it's
        # still open (inside the spy, before fsync_dir's own finally:
        # closes it).
        resolved_paths = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            resolved_paths.append(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        with mock.patch("app.atomic_io.os.fsync", side_effect=spy_fsync):
            fsync_dir(self.dir_path)

        self.assertEqual(resolved_paths, [os.path.realpath(str(self.dir_path))])

    def test_closes_the_directory_fd_afterwards(self):
        opened_fds = []
        real_open = os.open

        def spy_open(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            opened_fds.append(fd)
            return fd

        with mock.patch("app.atomic_io.os.open", side_effect=spy_open):
            fsync_dir(self.dir_path)

        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])  # closed -- no longer a valid fd

    def test_missing_directory_is_best_effort_not_a_crash(self):
        missing = self.dir_path / "does-not-exist"
        try:
            fsync_dir(missing)
        except Exception as exc:
            self.fail(f"fsync_dir must swallow a missing directory, raised {exc!r}")

    def test_fsync_failure_is_best_effort_not_a_crash(self):
        with mock.patch("app.atomic_io.os.fsync", side_effect=OSError("nope")):
            try:
                fsync_dir(self.dir_path)
            except Exception as exc:
                self.fail(f"fsync_dir must swallow an os.fsync OSError, raised {exc!r}")


class AtomicWriteTextTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_path = Path(self._tmp.name)

    def test_writes_the_given_text(self):
        path = self.dir_path / "state.json"
        atomic_write_text(path, "hello\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_overwrites_existing_content_completely(self):
        path = self.dir_path / "state.json"
        path.write_text("old content that is much longer than the new one", encoding="utf-8")
        atomic_write_text(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_creates_the_parent_directory_if_missing(self):
        path = self.dir_path / "nested" / "sub" / "state.json"
        atomic_write_text(path, "hi")
        self.assertEqual(path.read_text(encoding="utf-8"), "hi")

    def test_no_temp_file_left_behind_on_success(self):
        path = self.dir_path / "state.json"
        atomic_write_text(path, "hi")
        leftovers = [p for p in self.dir_path.iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])

    def test_temp_file_is_cleaned_up_if_the_write_fails(self):
        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                atomic_write_text(path, "hi")
        # No half-written temp file left behind, and the target was never
        # created (os.replace never ran).
        self.assertEqual(list(self.dir_path.iterdir()), [])

    def test_fsyncs_the_temp_file_before_the_rename(self):
        calls = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.os.fsync", side_effect=spy_fsync), \
                mock.patch("app.atomic_io.os.replace", side_effect=spy_replace):
            atomic_write_text(path, "hi")
        # A 2nd "fsync" follows -- that's fsync_dir()'s own directory
        # fsync after the rename (see test_fsyncs_the_directory_after_
        # the_rename below); what this test cares about is that the
        # FIRST fsync (the temp file's content) happens strictly before
        # the rename, not after.
        self.assertEqual(calls[:2], ["fsync", "replace"])

    def test_fsyncs_the_directory_after_the_rename(self):
        calls = []
        real_replace = os.replace
        real_fsync_dir = fsync_dir

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        def spy_fsync_dir(path):
            calls.append("fsync_dir")
            return real_fsync_dir(path)

        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.os.replace", side_effect=spy_replace), \
                mock.patch("app.atomic_io.fsync_dir", side_effect=spy_fsync_dir):
            atomic_write_text(path, "hi")
        self.assertEqual(calls, ["replace", "fsync_dir"])


if __name__ == "__main__":
    unittest.main()
