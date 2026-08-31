"""`my-bt admin backup` -- app/backup.py.

The one thing a backup must never do is look complete while missing
something, so these tests are mostly about what is in the archive and
what the manifest says about what is not.
"""
import io
import os
import stat
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import backup

SETTINGS = """
[site]
base_url = "https://booking.example.org"
timezone = "Europe/Berlin"
admin_email = "admin@example.org"
static_site_dir = "{site}"

[booking_calendar]
caldav_url = "https://dav.example.org/caldav/"
username = "cal@example.org"
password_file = "{secrets}/caldav_password"
calendar = "Calendar"

[smtp]
host = "smtp.example.org"
username = "cal@example.org"
password_file = "{secrets}/smtp_password"
from_address = "cal@example.org"

[admin]
password_hash_file = "{secrets}/admin_password_hash"

[privacy]
erasure_pepper_file = "{secrets}/erasure_pepper"

[[course]]
shortname = "yoga"
title = "Yoga"
location = "Hall"
weekday = "wed"
start_time = "17:15"
duration_minutes = 60
capacity = 10
"""

NOW = datetime(2026, 9, 1, 0, 4, tzinfo=timezone.utc)


class BackupFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.etc = self.root / "etc"
        self.secrets = self.etc / "secrets"
        self.data = self.root / "data"
        self.site = self.root / "site"
        self.out = self.root / "out"
        for d in (self.etc / "web-editable", self.secrets, self.data, self.site, self.out):
            d.mkdir(parents=True)
        self.settings = self.etc / "settings.toml"
        self.settings.write_text(
            SETTINGS.format(site=self.site, secrets=self.secrets), encoding="utf-8")
        for name in ("caldav_password", "smtp_password",
                     "admin_password_hash", "erasure_pepper"):
            (self.secrets / name).write_text(f"secret-{name}\n", encoding="utf-8")
        (self.etc / "web-editable" / "settings.web-editable.toml").write_text(
            "[macros]\nhello = \"hi\"\n", encoding="utf-8")
        (self.data / "registrations.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.data / "users.csv").write_text("a\n", encoding="utf-8")
        (self.site / "index.html").write_text("<html></html>", encoding="utf-8")

    def create(self, **kwargs):
        kwargs.setdefault("target", self.out)
        return backup.create(self.settings, self.data, now=NOW, **kwargs)

    def names(self, result):
        with tarfile.open(result.archive) as tar:
            return sorted(tar.getnames())


class BackupContentsTest(BackupFixture):
    def test_the_name_carries_the_timestamp_the_operator_asked_for(self):
        self.assertEqual(self.create().archive.name,
                         "my-booking-backup-2026-09-01_0004.tar.gz")

    def test_both_halves_of_the_configuration_are_in_it(self):
        names = self.names(self.create())
        self.assertIn("config/settings.toml", names)
        self.assertIn("config/web-editable/settings.web-editable.toml", names)

    def test_the_data_the_site_cannot_be_rebuilt_without(self):
        names = self.names(self.create())
        self.assertIn("data/registrations.csv", names)
        self.assertIn("data/users.csv", names)

    def test_the_deployed_static_pages(self):
        self.assertIn("site/index.html", self.names(self.create()))

    def test_the_conflict_feed_cache_is_left_out(self):
        # It refetches itself within minutes, and a stale copy restored
        # over a live one is worse than no copy at all.
        cache = self.data / "conflict_cache"
        cache.mkdir()
        (cache / "work.ics").write_text("BEGIN:VCALENDAR\n", encoding="utf-8")
        self.assertNotIn("data/conflict_cache/work.ics", self.names(self.create()))

    def test_the_data_dirs_own_git_history_is_left_out(self):
        git = self.data / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.assertFalse([n for n in self.names(self.create()) if ".git" in n])

    def test_the_log_is_left_out(self):
        # History, not state -- and it is the biggest file in the data dir.
        (self.data / "my-booking.log").write_text("WARNING x\n", encoding="utf-8")
        self.assertNotIn("data/my-booking.log", self.names(self.create()))


class BackupSecretsTest(BackupFixture):
    def test_secrets_are_included_by_default(self):
        # A backup that cannot restore a working service is a trap: it
        # looks like insurance and is not.
        names = self.names(self.create())
        for name in ("caldav_password", "smtp_password",
                     "admin_password_hash", "erasure_pepper"):
            self.assertIn(f"secrets/{name}", names)

    def test_the_archive_is_created_unreadable_to_anyone_else(self):
        result = self.create()
        mode = stat.S_IMODE(os.stat(result.archive).st_mode)
        self.assertEqual(oct(mode), oct(0o600))

    def test_no_secrets_leaves_them_out_and_says_so(self):
        result = self.create(with_secrets=False)
        self.assertFalse([n for n in self.names(result) if n.startswith("secrets/")])
        with tarfile.open(result.archive) as tar:
            manifest = tar.extractfile("MANIFEST.txt").read().decode()
        self.assertIn("NOT included", manifest)
        self.assertIn("cannot restore a working service", manifest)


class BackupManifestTest(BackupFixture):
    def manifest(self, **kwargs):
        with tarfile.open(self.create(**kwargs).archive) as tar:
            return tar.extractfile("MANIFEST.txt").read().decode()

    def test_it_lists_everything_that_went_in(self):
        text = self.manifest()
        self.assertIn("config/settings.toml", text)
        self.assertIn("secrets/erasure_pepper", text)

    def test_it_says_what_was_left_out_and_why(self):
        # The half that matters on the bad day: not what is there, but
        # what is not, so nobody looks for it in vain.
        text = self.manifest()
        self.assertIn("NOT INCLUDED", text)
        self.assertIn("nginx vhost", text)

    def test_it_says_how_to_put_it_back(self):
        text = self.manifest()
        self.assertIn("RESTORE", text)
        self.assertIn("chmod 600", text)

    def test_it_records_the_version_it_came_from(self):
        self.assertIn("my-booking-tool ", self.manifest())


class BackupPlanTest(BackupFixture):
    """plan() is separate from create() so a caller can print the
    decisions -- and so these can be checked without writing anything."""

    def test_a_missing_console_file_is_reported_not_silently_dropped(self):
        (self.etc / "web-editable" / "settings.web-editable.toml").unlink()
        _items, skipped = backup.plan(self.settings, self.data)
        self.assertTrue([why for label, why in skipped
                         if label == "settings.web-editable.toml"])

    def test_a_missing_data_dir_is_reported(self):
        _items, skipped = backup.plan(self.settings, self.root / "gone")
        self.assertTrue([why for label, why in skipped if label == "data"])

    def test_nothing_is_written_by_planning(self):
        backup.plan(self.settings, self.data)
        self.assertEqual(list(self.out.iterdir()), [])


class BackupTargetTest(BackupFixture):
    """The optional TARGET: a directory, a filename, or "-" for stdout.

    The operator's own use for the third one:
    `ssh ovh sudo my-bt admin backup - > 2026-09-01_0014.my-bt-backup.tar.gz`."""

    def test_no_target_writes_the_standard_name_here(self):
        self.assertEqual(backup.resolve_target(None, NOW),
                         Path(".") / "my-booking-backup-2026-09-01_0004.tar.gz")

    def test_a_directory_takes_the_standard_name_inside_it(self):
        self.assertEqual(backup.resolve_target(self.out, NOW),
                         self.out / "my-booking-backup-2026-09-01_0004.tar.gz")

    def test_anything_else_is_the_filename(self):
        target = self.out / "2026-09-01_0014.my-bt-backup.tar.gz"
        self.assertEqual(backup.resolve_target(target, NOW), target)

    def test_a_path_that_does_not_exist_yet_is_a_filename_not_a_directory(self):
        # Creating directories on the way to writing a backup turns a
        # typo into a file nobody will ever look in again.
        target = self.out / "nope" / "b.tar.gz"
        self.assertEqual(backup.resolve_target(target, NOW), target)

    def test_dash_means_stdout(self):
        self.assertIsNone(backup.resolve_target("-", NOW))

    def test_the_stream_is_a_valid_archive_and_nothing_else(self):
        # Anything else printed on stdout corrupts the file the caller is
        # redirecting into, so this checks the bytes ARE the archive:
        # gzip magic first, every member present, manifest readable.
        buf = io.BytesIO()
        result = backup.create(self.settings, self.data, to_stdout=True,
                               stdout=buf, now=NOW)
        data = buf.getvalue()
        self.assertEqual(data[:2], b"\x1f\x8b")
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            names = tar.getnames()
            self.assertIn("config/settings.toml", names)
            self.assertIn("MANIFEST.txt", names)
            self.assertIn(b"RESTORE", tar.extractfile("MANIFEST.txt").read())
        self.assertIsNone(result.archive)

    def test_streaming_writes_no_file_anywhere(self):
        backup.create(self.settings, self.data, to_stdout=True,
                      stdout=io.BytesIO(), now=NOW)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_the_manifest_says_stdout_when_there_is_no_file(self):
        buf = io.BytesIO()
        backup.create(self.settings, self.data, to_stdout=True, stdout=buf, now=NOW)
        with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tar:
            self.assertIn(b"(stdout)", tar.extractfile("MANIFEST.txt").read())
