"""End-to-end smoke tests for scripts/my-bt's `cmd_show`/`cmd_cancel` --
loaded via importlib.machinery.SourceFileLoader (see test_my_bt_argparse.py
for why: no .py extension, lives outside app/, can't be `import`ed
directly). Covers three 2026-07-09 fixes together, since all three touch
the same two commands:

1. `my-bt show` with nothing to work with now prints full help (exit 0),
   not a bespoke stderr message (see test_my_bt_argparse.py for the
   analogous bare `my-bt`/`my-bt admin` fix this mirrors).
2. Single-entity `show` results (one registration, one course, a user's
   own profile) now print as a vertical 'key : value' listing instead of
   an illegible one-row horizontal table.
3. `my-bt cancel <query>` auto-detects a registration id or date from one
   positional, same as `show` -- app.cli_cancel.classify_cancel_query's
   own unit tests (tests/test_cli_cancel.py) cover the classification
   logic itself; these confirm cmd_cancel actually WIRES that logic up
   end to end.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.caldav_client import CalDAVClient, Response
from app.security import hash_token, new_token
from app.storage import Store

from .helpers import make_course, make_settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY_BT_PATH = os.path.join(REPO_ROOT, "scripts", "my-bt")

_loader = importlib.machinery.SourceFileLoader("my_bt_show_cancel_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)

EMPTY_REPORT = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>"""
PROPFIND_BODY = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/caldav/Bookings/</D:href>
    <D:propstat><D:prop><D:displayname>Bookings</D:displayname></D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


class _FakeTransport:
    def __call__(self, method, url, body="", extra_headers=None):
        if method == "PROPFIND":
            return Response(207, {}, PROPFIND_BODY)
        if method == "REPORT":
            return Response(207, {}, EMPTY_REPORT)
        if method == "PUT":
            return Response(201, {"etag": '"e1"'}, "")
        if method == "DELETE":
            return Response(204, {}, "")
        raise AssertionError(f"unexpected {method} {url}")


class _MyBtCliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-class-1", capacity=5)
        self.settings = make_settings(courses=(self.course,), booking_calendar="Bookings")

        patcher = patch.object(my_bt_mod.app_config, "load_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sent_emails = []
        for target in ("app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            p = patch(target, side_effect=lambda *a, **k: self.sent_emails.append((a, k)))
            p.start()
            self.addCleanup(p.stop)

        fake_client = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=_FakeTransport(),
        )
        # Two DIFFERENT build_caldav_client references get exercised here:
        # the single-id cancel path goes through app.cli_cancel's own
        # import of it, but scripts/my-bt's _cmd_cancel_occurrence (the
        # --date/mass-cancel path) calls app.cancel_flow.build_caldav_
        # client directly (aliased `app_cancel_flow` there) -- both need
        # patching, or the date-based test hits a real (failing) HTTPS
        # connection attempt.
        for target in ("app.cli_cancel.build_caldav_client", "app.cancel_flow.build_caldav_client"):
            p = patch(target, return_value=fake_client)
            p.start()
            self.addCleanup(p.stop)

    def _args(self, command: str, **overrides):
        base = ["--data-dir", self._tmp.name, "--settings", "unused.toml", command]
        args = my_bt_mod.build_parser().parse_args(base)
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def _book(self, email: str, name: str, occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration(
            "yoga-class-1", occurrence_date, user.user_id, hash_token(new_token()),
        )
        return user, reg

    def _run(self, func, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                func(args)
            except SystemExit as exc:
                return exc.code, out.getvalue()
        return None, out.getvalue()

    def assertKv(self, output: str, key: str, value: str):
        """Asserts a '{key}<padding> : {value}' line is present --
        _print_single_row_kv pads every key to the row's own widest key
        width, so the exact amount of padding varies row to row; only the
        key/separator/value shape is what's actually being verified."""
        pattern = rf"^{re.escape(key)}\s+: {re.escape(value)}$"
        self.assertTrue(
            re.search(pattern, output, re.MULTILINE),
            f"no line matching {pattern!r} in:\n{output}",
        )


class ShowBareInvocationTest(_MyBtCliTestBase):
    def test_bare_show_prints_help_and_exits_zero(self):
        args = self._args("show")
        code, output = self._run(my_bt_mod.cmd_show, args)
        self.assertEqual(code, 0)
        self.assertIn("usage: my-bt show", output)
        self.assertNotIn("nothing to show", output)


class ShowSingleEntityKeyValueOutputTest(_MyBtCliTestBase):
    def test_single_registration_prints_vertical_key_value(self):
        _user, reg = self._book("guest@example.org", "Guest")
        args = self._args("show", query=reg.registration_id)
        _code, output = self._run(my_bt_mod.cmd_show, args)
        self.assertKv(output, "registration_id", reg.registration_id)
        self.assertKv(output, "course_shortname", "yoga-class-1")
        # NOT the old horizontal table shape (header line + dashes rule).
        self.assertNotIn("-" * 20, output)

    def test_single_course_prints_vertical_key_value(self):
        args = self._args("show", query="yoga-class-1")
        _code, output = self._run(my_bt_mod.cmd_show, args)
        self.assertKv(output, "shortname", "yoga-class-1")
        self.assertNotIn("-" * 20, output)

    def test_user_profile_is_key_value_but_booking_history_stays_tabular(self):
        self._book("guest@example.org", "Guest")
        args = self._args("show", query="guest@example.org")
        _code, output = self._run(my_bt_mod.cmd_show, args)
        self.assertIn("-- profile --", output)
        self.assertKv(output.split("-- booking history --", 1)[0], "email", "guest@example.org")
        self.assertIn("-- booking history --", output)
        # The booking-history table still uses the old horizontal shape
        # (a header row followed by a dashed rule) -- only single-row
        # results switch to key:value.
        history_section = output.split("-- booking history --", 1)[1]
        self.assertIn("-" * 20, history_section)

    def test_json_format_is_unaffected(self):
        _user, reg = self._book("guest@example.org", "Guest")
        args = self._args("show", query=reg.registration_id, format="json")
        _code, output = self._run(my_bt_mod.cmd_show, args)
        self.assertTrue(output.strip().startswith("["))
        self.assertIn(reg.registration_id, output)


class CancelSmartQueryTest(_MyBtCliTestBase):
    def _cancel_args(self, query):
        # NOT built via _args("cancel") + attribute overrides: query/--id/
        # --date is a REQUIRED mutually exclusive group (unlike show's,
        # which isn't required), so parsing bare `cancel` with nothing on
        # the command line would itself raise SystemExit(2) before any
        # override could apply. Passing the real positional on argv
        # avoids that entirely, and lets real argparse defaults
        # (id/date/course/message=None) do their normal job.
        argv = ["--data-dir", self._tmp.name, "--settings", "unused.toml", "cancel", query, "--yes"]
        return my_bt_mod.build_parser().parse_args(argv)

    def test_query_recognized_as_registration_id_cancels_it(self):
        _user, reg = self._book("guest@example.org", "Guest")
        args = self._cancel_args(reg.registration_id)
        _code, output = self._run(my_bt_mod.cmd_cancel, args)
        self.assertIn(f"canceled registration {reg.registration_id}", output)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, "canceled_by_host")

    def test_query_recognized_as_short_id_cancels_it(self):
        _user, reg = self._book("guest@example.org", "Guest")
        live_ids = [r["registration_id"] for r in self.store.read_registrations(scope="live")]
        short = my_bt_mod.app_cli_list.assign_short_ids(live_ids)[reg.registration_id]
        args = self._cancel_args(short)
        _code, output = self._run(my_bt_mod.cmd_cancel, args)
        self.assertIn(f"canceled registration {reg.registration_id}", output)

    def test_query_recognized_as_date_cancels_whole_occurrence(self):
        self._book("a@example.org", "A", occurrence_date="2026-08-01")
        self._book("b@example.org", "B", occurrence_date="2026-08-01")
        args = self._cancel_args("2026-08-01")
        _code, output = self._run(my_bt_mod.cmd_cancel, args)
        self.assertIn("canceled 2 registration(s)", output)

    def test_query_recognized_as_bare_course_is_explained_not_silently_ignored(self):
        args = self._cancel_args("yoga-class-1")
        code, output = self._run(my_bt_mod.cmd_cancel, args)
        self.assertEqual(code, 1)

    def test_unrecognized_query_reports_clearly(self):
        args = self._cancel_args("not-a-thing-at-all-zzz")
        code, output = self._run(my_bt_mod.cmd_cancel, args)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
