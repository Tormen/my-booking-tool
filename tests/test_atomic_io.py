"""app/atomic_io.py -- the shared crash-safe write helper extracted from
app/storage.py's own _LockedCsv._atomic_write so every OTHER module that
writes a file to disk (config, secrets, marker/state files, rendered
static pages) gets the same temp-file+fsync+rename+dir-fsync pattern,
not just the CSV storage layer. 2026-07-15, the operator: "yes please ALL
writes linked to my-booking-tool, my-bt and the site."

See tests/test_storage.py::AtomicWriteDirFsyncIntegrationTest for the
CSV-specific integration point (a real _LockedCsv write calling this)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.atomic_io import (
    SERVICE_GROUP,
    SERVICE_USER,
    atomic_write_text,
    fsync_dir,
    probe_dir_fsync_support,
    secure_data_path,
)


class ProbeDirFsyncSupportTest(unittest.TestCase):
    """2026-07-15, the operator, on fsync_dir()'s own best-effort/never-raises
    design: "that's the correct call for availability ... but it's also
    the kind of failure that's invisible until the one time it matters
    ... worth a one-time capability probe at startup". probe_dir_fsync_
    support() is that probe -- same underlying operation as fsync_dir(),
    but meant to be called once and reacted to loudly on a False result
    (see app.cli_checks.check_directory_fsync_support and
    app.serve.main's startup check), not silently logged and moved past."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_path = Path(self._tmp.name)

    def test_true_when_fsync_actually_works(self):
        self.assertTrue(probe_dir_fsync_support(self.dir_path))

    def test_false_when_directory_is_missing(self):
        self.assertFalse(probe_dir_fsync_support(self.dir_path / "does-not-exist"))

    def test_false_when_fsync_itself_fails(self):
        # e.g. the ENOTSUP/EINVAL an unusual mount (some network
        # filesystems, certain container overlay setups) could raise --
        # this is exactly the silent-degradation scenario the probe
        # exists to catch instead of just logging a WARNING nobody reads.
        with mock.patch("app.atomic_io.os.fsync", side_effect=OSError("Operation not supported")):
            self.assertFalse(probe_dir_fsync_support(self.dir_path))

    def test_never_raises_even_on_failure(self):
        with mock.patch("app.atomic_io.os.fsync", side_effect=OSError("nope")):
            try:
                probe_dir_fsync_support(self.dir_path)
            except Exception as exc:
                self.fail(f"probe_dir_fsync_support must not raise, raised {exc!r}")


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
            result = fsync_dir(missing)
        except Exception as exc:
            self.fail(f"fsync_dir must swallow a missing directory, raised {exc!r}")
        self.assertFalse(result)

    def test_fsync_failure_is_best_effort_not_a_crash(self):
        with mock.patch("app.atomic_io.os.fsync", side_effect=OSError("nope")):
            try:
                result = fsync_dir(self.dir_path)
            except Exception as exc:
                self.fail(f"fsync_dir must swallow an os.fsync OSError, raised {exc!r}")
        self.assertFalse(result)

    def test_success_returns_true(self):
        # 2026-07-15: the return value is what probe_dir_fsync_support()
        # (below) is built on -- a silent None here would make that
        # capability probe meaningless.
        self.assertTrue(fsync_dir(self.dir_path))


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

    def test_secure_true_secures_before_the_rename_not_after(self):
        # Same ordering requirement as _LockedCsv._atomic_write always had:
        # permissions/ownership must be right at the instant the new
        # content becomes visible at `path`, not as a separate step the
        # rename could race ahead of.
        calls = []
        real_replace = os.replace

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        def spy_secure(path, mode=0o640):
            calls.append("secure")

        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.os.replace", side_effect=spy_replace), \
                mock.patch("app.atomic_io.secure_data_path", side_effect=spy_secure):
            atomic_write_text(path, "hi", secure=True)
        self.assertEqual(calls, ["secure", "replace"])

    def test_secure_false_never_calls_secure_data_path(self):
        # The default -- most atomic_write_text callers (settings.toml,
        # secrets, rendered static pages) don't share this app's
        # my-booking group model at all and must be left alone.
        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.secure_data_path") as m_secure:
            atomic_write_text(path, "hi")
        m_secure.assert_not_called()

    def test_secure_true_passes_the_given_mode_through(self):
        path = self.dir_path / "state.json"
        with mock.patch("app.atomic_io.secure_data_path") as m_secure:
            atomic_write_text(path, "hi", secure=True, mode=0o600)
        m_secure.assert_called_once_with(mock.ANY, mode=0o600)


class SecureDataPathTest(unittest.TestCase):
    """2026-07-09: real production incident on the operator's own VPS -- he ran
    `my-bt cancel` directly as root, leaving registrations.csv root:root
    mode 0600 -- completely unreadable by my-booking-watchdog.service (runs
    as the unprivileged my-booking user/group), which then crashed with
    PermissionError on its very next scheduled read. secure_data_path is
    the self-healing fix _LockedCsv (and now every other data-directory
    write, via atomic_write_text's secure= param) applies on every
    write/creation -- these tests exercise it directly rather than needing
    an actual multi-user setup with a real "my-booking" system group to
    reproduce the original bug.

    2026-07-10: a SECOND real incident -- chgrp-only wasn't enough for a
    read-WRITE service. See secure_data_path's own docstring for the full
    story; the tests below patch os.geteuid explicitly (rather than relying
    on incidentally not running the suite as root) so the root/non-root
    branch is deterministic regardless of who/what runs these tests.

    2026-07-10: moved here from tests/test_storage.py when secure_data_path
    itself moved from app/storage.py into app/atomic_io.py, so it's shared
    by the CSV write path AND every other data-directory write (calendar
    markers, the maintenance flag) instead of being CSV-only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registrations.csv"
        self.path.touch()

    def tearDown(self):
        self._tmp.cleanup()

    def test_chmod_grants_group_read_not_just_owner(self):
        os.chmod(self.path, 0o600)
        with mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()), \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000):
            secure_data_path(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_directory_mode_gets_the_execute_bit_when_asked(self):
        directory = Path(self._tmp.name) / "archived"
        directory.mkdir()
        with mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()), \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000):
            secure_data_path(directory, mode=0o750)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o750)

    def test_chgrps_to_the_service_group_when_it_exists(self):
        fake_gid = 424242
        with mock.patch("app.atomic_io.grp.getgrnam") as m_getgrnam, \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000), \
                mock.patch("app.atomic_io.os.chown") as m_chown:
            m_getgrnam.return_value = mock.Mock(gr_gid=fake_gid)
            secure_data_path(self.path)
        m_getgrnam.assert_called_once_with(SERVICE_GROUP)
        # -1 as the uid arg: a NON-root process never touches ownership,
        # only the group -- see secure_data_path's own docstring on why
        # that's still correct (only root can safely reassign an owner).
        m_chown.assert_called_once_with(self.path, -1, fake_gid)

    def test_missing_service_group_does_not_raise_and_chmod_still_applies(self):
        # e.g. a dev checkout or this very test suite, where no system
        # group named "my-booking" exists at all.
        with mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError("no such group")), \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000):
            try:
                secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"secure_data_path must swallow a missing group, raised {exc!r}")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_chown_permission_error_does_not_raise(self):
        # e.g. the calling process isn't root and isn't a member of the
        # target group -- POSIX refuses the chgrp, but the write itself
        # must still succeed.
        with mock.patch("app.atomic_io.grp.getgrnam") as m_getgrnam, \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000), \
                mock.patch("app.atomic_io.os.chown", side_effect=PermissionError("not allowed")):
            m_getgrnam.return_value = mock.Mock(gr_gid=1)
            try:
                secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"secure_data_path must swallow a chown PermissionError, raised {exc!r}")

    def test_chmod_failure_does_not_raise(self):
        with mock.patch("app.atomic_io.os.chmod", side_effect=OSError("nope")), \
                mock.patch("app.atomic_io.os.geteuid", return_value=1000), \
                mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()):
            try:
                secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"secure_data_path must swallow a chmod OSError, raised {exc!r}")

    # -- root-only owner chown (2026-07-10 fix) -----------------------------

    def test_root_chowns_the_owner_back_to_the_service_user(self):
        fake_uid = 131313
        with mock.patch("app.atomic_io.os.geteuid", return_value=0), \
                mock.patch("app.atomic_io.pwd.getpwnam") as m_getpwnam, \
                mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()), \
                mock.patch("app.atomic_io.os.chown") as m_chown:
            m_getpwnam.return_value = mock.Mock(pw_uid=fake_uid)
            secure_data_path(self.path)
        m_getpwnam.assert_called_once_with(SERVICE_USER)
        m_chown.assert_called_once_with(self.path, fake_uid, -1)

    def test_non_root_never_attempts_the_owner_chown(self):
        with mock.patch("app.atomic_io.os.geteuid", return_value=1000), \
                mock.patch("app.atomic_io.pwd.getpwnam") as m_getpwnam, \
                mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()):
            secure_data_path(self.path)
        m_getpwnam.assert_not_called()

    def test_missing_service_user_does_not_raise_when_root(self):
        # e.g. a dev checkout run under fakeroot/a container where uid 0
        # exists but no "my-booking" system user was ever created.
        with mock.patch("app.atomic_io.os.geteuid", return_value=0), \
                mock.patch("app.atomic_io.pwd.getpwnam", side_effect=KeyError("no such user")), \
                mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()):
            try:
                secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"secure_data_path must swallow a missing service user, raised {exc!r}")

    def test_root_owner_chown_permission_error_does_not_raise(self):
        with mock.patch("app.atomic_io.os.geteuid", return_value=0), \
                mock.patch("app.atomic_io.pwd.getpwnam") as m_getpwnam, \
                mock.patch("app.atomic_io.grp.getgrnam", side_effect=KeyError()), \
                mock.patch("app.atomic_io.os.chown", side_effect=PermissionError("not allowed")):
            m_getpwnam.return_value = mock.Mock(pw_uid=1)
            try:
                secure_data_path(self.path)
            except Exception as exc:
                self.fail(f"secure_data_path must swallow an owner-chown PermissionError, raised {exc!r}")

    def test_root_chowns_owner_then_still_chgrps(self):
        # Both the 2026-07-09 (group) and 2026-07-10 (owner) fixes must
        # apply together on a root-run write -- neither should short-circuit
        # the other.
        fake_uid, fake_gid = 131313, 424242
        with mock.patch("app.atomic_io.os.geteuid", return_value=0), \
                mock.patch("app.atomic_io.pwd.getpwnam") as m_getpwnam, \
                mock.patch("app.atomic_io.grp.getgrnam") as m_getgrnam, \
                mock.patch("app.atomic_io.os.chown") as m_chown:
            m_getpwnam.return_value = mock.Mock(pw_uid=fake_uid)
            m_getgrnam.return_value = mock.Mock(gr_gid=fake_gid)
            secure_data_path(self.path)
        m_chown.assert_any_call(self.path, fake_uid, -1)
        m_chown.assert_any_call(self.path, -1, fake_gid)


if __name__ == "__main__":
    unittest.main()
