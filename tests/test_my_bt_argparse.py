"""Tests for scripts/my-bt's own argparse wiring -- specifically the
2026-07-09 fix for bare `my-bt` / `my-bt admin` (or any other group that
needs a further sub-command): these must behave exactly like `-h`/`--help`
(full help text, exit 0), not argparse's default "the following arguments
are required" error (exit 2) -- when --help is displayed, this is not
an error, so no error message needs to be written.

scripts/my-bt has no .py extension and lives outside app/, so it can't be
imported with a plain `import` statement -- loaded here via
importlib.machinery.SourceFileLoader, the same approach used for ad hoc
manual smoke-testing of this script during development; this is the first
time that loader pattern is checked into a committed test file.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY_BT_PATH = os.path.join(REPO_ROOT, "scripts", "my-bt")

_loader = importlib.machinery.SourceFileLoader("my_bt_argparse_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


class BareCommandHelpParserTest(unittest.TestCase):
    """Unit-level checks on build_parser()'s own parsed Namespace, no
    process/exit involved -- see BareCommandMainBehaviorTest below for the
    actual main()/exit-code/stdout behavior."""

    def test_bare_invocation_has_no_func_and_top_level_help_parser(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args([])
        self.assertFalse(hasattr(args, "func"))
        self.assertIsNone(args.command)
        self.assertIs(args._help_parser, parser)

    def test_bare_admin_has_no_func_and_admin_group_help_parser(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin"])
        self.assertFalse(hasattr(args, "func"))
        self.assertEqual(args.command, "admin")
        self.assertIsNone(args.admin_command)
        self.assertIsNot(args._help_parser, parser)
        self.assertIn("hash-password", args._help_parser.format_help())

    def test_real_leaf_command_still_sets_func(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "health"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_health)

    def test_csp_violations_leaf_command_sets_func(self):
        # 2026-07-13: full-detail CSP-violation view, added alongside
        # cli_checks.check_csp_violations/find_csp_violations.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "csp-violations"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_csp_violations)

    def test_resync_calendar_leaf_command_sets_func(self):
        # 2026-07-09: existing (future)
        # calendar invites are also updated -- see
        # app.calendar_sync.resync_all_future_calendar_events's own
        # docstring for the full story; this just confirms the new
        # subcommand is wired up the same way every other admin leaf
        # command is.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "resync-calendar"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_resync_calendar)

    def test_admin_gdpr_group_unaffected_already_had_its_own_func(self):
        # 2026-07-14: `admin gdpr` (formerly the top-level `gdpr-retention`,
        # moved/renamed as part of a restructure -- see
        # cmd_admin_gdpr's docstring) has its own nested subparsers
        # (dest="gdpr_command") that was never required=True -- it already
        # has func set at its own level (cmd_admin_gdpr), so a bare
        # `my-bt admin gdpr` was never part of the original bare-command
        # bug this test class covers. Confirms the fix didn't touch this
        # group.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_gdpr)

    def test_admin_gdpr_bookings_leaf_command_sets_func(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "bookings"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_gdpr_bookings)
        self.assertFalse(args.purge)

    def test_admin_gdpr_accounts_leaf_command_sets_func(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "accounts", "--purge"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_admin_gdpr_accounts)
        self.assertTrue(args.purge)

    def test_admin_gdpr_erase_leaf_command_sets_func(self):
        # 2026-07-14: manually erasing an attendee's data (GDPR Art. 17)
        # moved to `admin gdpr erase`, sitting alongside bookings/accounts.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "erase", "--email", "guest@example.com", "--yes"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_erase)
        self.assertEqual(args.email_flag, "guest@example.com")
        self.assertIsNone(args.email_pos)
        self.assertTrue(args.yes)

    def test_admin_gdpr_erase_accepts_a_positional_email_too(self):
        # 2026-07-16: --email was made optional here -- `my-bt
        # admin erase guest@example.com` now works too, the email can
        # be given as a plain positional argument instead of always
        # needing --email spelled out.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "erase", "guest@example.com", "--yes"])
        self.assertEqual(args.func, my_bt_mod.cmd_erase)
        self.assertEqual(args.email_pos, "guest@example.com")
        self.assertIsNone(args.email_flag)
        self.assertTrue(args.yes)


class AdminLogoutArgsTest(unittest.TestCase):
    """2026-07-10: `my-bt admin logout EMAIL` / `--all` -- the
    force-logout command interactive_setup's restart guard (and the RPM's
    own %pre gate) point you at."""

    def test_email_positional_sets_func_and_email(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "logout", "guest@example.com"])
        self.assertEqual(args.func, my_bt_mod.cmd_admin_logout)
        self.assertEqual(args.email_pos, "guest@example.com")
        self.assertFalse(args.all)

    def test_all_flag_sets_all_true_with_no_email(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "logout", "--all"])
        self.assertEqual(args.func, my_bt_mod.cmd_admin_logout)
        self.assertTrue(args.all)
        self.assertIsNone(args.email_pos)

    def test_bare_logout_has_neither(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "logout"])
        self.assertEqual(args.func, my_bt_mod.cmd_admin_logout)
        self.assertFalse(args.all)
        self.assertIsNone(args.email_pos)

    def test_bare_logout_lists_active_sessions_instead_of_erroring(self):
        # 2026-07-14: a bare `my-bt admin logout` used to print only a
        # terse usage error -- unhelpful exactly when the RPM's %pre gate
        # or `setup -i` just told you to run it. It now shows WHO could be
        # logged out (the same overview those gates print, which itself
        # ends with the exact EMAIL/--all commands to run next).
        args = my_bt_mod.build_parser().parse_args(["admin", "logout"])
        payload = {
            "session_timeout_seconds": 14400,
            "sessions": [{
                "kind": "guest", "who": "test2@example.org", "name": "Mr. Test 2",
                "connected_since": "2026-07-14T13:43:00+00:00",
                "expires_at": "2026-07-14T17:43:00+00:00",
                "last_page": "/book/trier-sat-yoga",
                "last_seen": "2026-07-14T13:43:00+00:00",
            }],
        }
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status", return_value=(payload, None)):
            with contextlib.redirect_stdout(out):
                my_bt_mod.cmd_admin_logout(args)  # must NOT raise/exit
        output = out.getvalue()
        self.assertIn("test2@example.org", output)
        self.assertIn("Mr. Test 2", output)
        self.assertIn("my-bt admin logout EMAIL", output)
        self.assertIn("--all", output)

    def test_bare_logout_with_no_sessions_says_so(self):
        args = my_bt_mod.build_parser().parse_args(["admin", "logout"])
        payload = {"session_timeout_seconds": 14400, "sessions": []}
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status", return_value=(payload, None)):
            with contextlib.redirect_stdout(out):
                my_bt_mod.cmd_admin_logout(args)
        self.assertIn("(no active sessions)", out.getvalue())

    def test_bare_logout_with_unreachable_service_still_errors(self):
        args = my_bt_mod.build_parser().parse_args(["admin", "logout"])
        err = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status", return_value=(None, "connection refused")):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    my_bt_mod.cmd_admin_logout(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("can't check active sessions", err.getvalue())


class UsersLogoutArgsTest(unittest.TestCase):
    """2026-07-10: 'logout <EMAIL>' and 'logout --all' were added
    as possibilities to my-bt users -- discoverable from `my-bt users`
    too, but the same underlying command as `my-bt admin logout`
    (both are supported)."""

    def test_users_logout_email_reaches_the_same_func_as_admin_logout(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users", "logout", "guest@example.com"])
        self.assertEqual(args.func, my_bt_mod.cmd_admin_logout)
        self.assertEqual(args.email_pos, "guest@example.com")
        self.assertFalse(args.all)

    def test_users_logout_all(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users", "logout", "--all"])
        self.assertEqual(args.func, my_bt_mod.cmd_admin_logout)
        self.assertTrue(args.all)
        self.assertIsNone(args.email_pos)

    def test_bare_users_is_unaffected_by_the_nested_logout_subcommand(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users"])
        self.assertEqual(args.func, my_bt_mod.cmd_users)


class GitSnapshotMessageArgTest(unittest.TestCase):
    """2026-07-14: an optional message parameter was requested for
    `my-bt admin git-snapshot -h` -- -m/--message lets a manual snapshot
    get a real, named commit message instead of the auto-generated
    "automatic snapshot: <timestamp>" text. See app/git_snapshot.py's
    snapshot() docstring for how the message is actually used."""

    def test_bare_git_snapshot_has_message_none(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "git-snapshot"])
        self.assertIsNone(args.message)
        self.assertEqual(args.func, my_bt_mod.cmd_git_snapshot)

    def test_message_flag_is_captured(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "git-snapshot", "-m", "before the risky edit"])
        self.assertEqual(args.message, "before the risky edit")

    def test_long_message_flag_is_captured(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "git-snapshot", "--message", "before the risky edit"])
        self.assertEqual(args.message, "before the risky edit")


class AdminBackupArgsTest(unittest.TestCase):
    """`my-bt admin backup` -- one .tar.gz of everything the RPM does not
    carry, written to the current directory by default."""

    def test_bare_backup_keeps_the_secrets_and_has_no_target(self):
        args = my_bt_mod.build_parser().parse_args(["admin", "backup"])
        self.assertFalse(args.no_secrets)
        self.assertIsNone(args.target)
        self.assertEqual(args.func, my_bt_mod.cmd_admin_backup)

    def test_no_secrets_is_opt_in(self):
        args = my_bt_mod.build_parser().parse_args(["admin", "backup", "--no-secrets"])
        self.assertTrue(args.no_secrets)

    def test_a_target_can_be_a_directory_or_a_filename(self):
        parse = my_bt_mod.build_parser().parse_args
        self.assertEqual(parse(["admin", "backup", "/mnt/usb"]).target, "/mnt/usb")
        self.assertEqual(parse(["admin", "backup", "b.tar.gz"]).target, "b.tar.gz")

    def test_only_a_dash_streams_never_a_redirect(self):
        # The operator ruled out inferring stdout from an isatty() check:
        # "this is otherwise not intuitive". `... backup > log.txt` writes
        # the archive as a file and captures the messages.
        with open(MY_BT_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("sys.stdout.isatty()", source)

    def test_a_dash_target_survives_argparse(self):
        # A bare "-" is the shape argparse is most likely to swallow as a
        # flag; it is the whole point of the operator's own use:
        # `ssh ovh sudo my-bt admin backup - > backup.tar.gz`.
        self.assertEqual(
            my_bt_mod.build_parser().parse_args(["admin", "backup", "-"]).target, "-")

    def test_the_help_warns_that_it_holds_credentials(self):
        # The warning has to be where the operator decides where to put
        # the file, not only in a manual they read once.
        parser = my_bt_mod.build_parser()
        admin = parser._subparsers._group_actions[0].choices["admin"]
        self.assertIn("backup", admin.format_help())
        backup_parser = admin._subparsers._group_actions[0].choices["backup"]
        self.assertIn("PLAIN-TEXT CREDENTIALS", backup_parser.description)


class UsersScopeArgsTest(unittest.TestCase):
    """2026-07-14: changing `my-bt users`'s bare-default scope --
    by default my-bt users now shows --live users that
    logged in (that has a last_login date), and --all shows
    both --live and --archived. --live shows ALL live
    users, also the ones that have no login date; --archive shows only
    the archived users; --all shows both.
    --live/--archive keep their old meaning; --all now covers what the
    bare default used to mean; the new bare default (scope=None) is
    handled specially in cmd_users() itself (live + has-logged-in)."""

    def test_bare_users_has_scope_none(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users"])
        self.assertIsNone(args.scope)

    def test_live_flag_sets_scope_live(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users", "--live"])
        self.assertEqual(args.scope, "live")

    def test_archive_flag_sets_scope_archived(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users", "--archive"])
        self.assertEqual(args.scope, "archived")

    def test_all_flag_sets_scope_all(self):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["users", "--all"])
        self.assertEqual(args.scope, "all")

    def test_live_and_all_are_mutually_exclusive(self):
        # argparse itself prints its own usage+error text straight to the
        # real stderr on a mutually-exclusive-group violation, before ever
        # raising -- left uncaptured, this leaks into the %check build log
        # as unrelated-looking noise (seen in a screenshot of a real rpmbuild
        # log where one test was not silent). Same io.StringIO()-via-
        # contextlib.redirect_stderr suppression already used just below in
        # BareCommandMainBehaviorTest.test_genuine_error_still_exits_
        # nonzero_with_error_text for the exact same reason.
        parser = my_bt_mod.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["users", "--live", "--all"])

    def _run_users(self, store_dir, extra_argv):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["--data-dir", store_dir, "users", "--format", "json"] + extra_argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            my_bt_mod.cmd_users(args)
        return json.loads(out.getvalue())

    def test_bare_default_shows_only_live_users_that_have_logged_in(self):
        import tempfile

        from app.storage import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        never_logged_in = store.upsert_user_for_booking("never@example.org", "Never")
        logged_in = store.upsert_user_for_booking("logged@example.org", "Logged")
        from app.storage import USER_FIELDS, _LockedCsv
        with _LockedCsv(store.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == logged_in.user_id:
                    row["last_login_at"] = "2026-07-01T09:30:00+00:00"
            write(rows, "test setup")

        bare = self._run_users(tmp.name, [])
        self.assertEqual([r["email"] for r in bare], ["logged@example.org"])

        live_all = self._run_users(tmp.name, ["--live"])
        self.assertEqual(
            sorted(r["email"] for r in live_all), ["logged@example.org", "never@example.org"],
        )


class UsersSessionColumnTest(unittest.TestCase):
    """2026-07-10: since last_login was already shown, a
    column for "session still active?" was added -- cmd_users wires the live
    /internal/status query (_query_internal_status) into
    app.cli_list.build_clean_user_view's new `active_sessions` param.
    Patches _query_internal_status directly rather than hitting a real
    port, same reasoning as every other _query_internal_status-consuming
    test in this codebase (app/cli_setup.py's own guard tests)."""

    def _make_store(self):
        import tempfile

        from app.storage import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        user = store.upsert_user_for_booking("ada@example.com", "Ada")
        from app.storage import USER_FIELDS, _LockedCsv
        with _LockedCsv(store.users_path, USER_FIELDS) as (rows, write):
            for row in rows:
                if row["user_id"] == user.user_id:
                    row["last_login_at"] = "2026-07-01T09:30:00+00:00"
            write(rows, "test setup")
        return tmp.name

    def _run_users(self, store_dir, extra_argv, status_payload):
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["--data-dir", store_dir, "users", "--format", "json"] + extra_argv)
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status", return_value=(status_payload, None)):
            with contextlib.redirect_stdout(out):
                my_bt_mod.cmd_users(args)
        return json.loads(out.getvalue())

    def test_active_guest_session_shows_active(self):
        store_dir = self._make_store()
        payload = {"sessions": [{"kind": "guest", "who": "ada@example.com"}]}
        rows = self._run_users(store_dir, [], payload)
        self.assertEqual(rows[0]["session"], "active")

    def test_no_matching_session_shows_offline(self):
        store_dir = self._make_store()
        payload = {"sessions": []}
        rows = self._run_users(store_dir, [], payload)
        self.assertEqual(rows[0]["session"], "(offline)")

    def test_admin_session_never_matches_a_user_row(self):
        # An "admin" session has no associated user_id/email at all (see
        # internal_status's own docstring) -- must never accidentally
        # mark a real attendee as active.
        store_dir = self._make_store()
        payload = {"sessions": [{"kind": "admin", "who": "admin"}]}
        rows = self._run_users(store_dir, [], payload)
        self.assertEqual(rows[0]["session"], "(offline)")

    def test_unreachable_service_shows_unknown(self):
        store_dir = self._make_store()
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["--data-dir", store_dir, "users", "--format", "json"])
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status", return_value=(None, "connection refused")):
            with contextlib.redirect_stdout(out):
                my_bt_mod.cmd_users(args)
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]["session"], "(unknown)")

    def test_raw_mode_never_queries_internal_status(self):
        # -r/--raw is a pure CSV dump with no network dependency -- see
        # cmd_users's own docstring on why the query only happens for
        # the clean view.
        store_dir = self._make_store()
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["--data-dir", store_dir, "users", "--format", "json", "-r"])
        out = io.StringIO()
        with mock.patch.object(my_bt_mod, "_query_internal_status") as mocked:
            with contextlib.redirect_stdout(out):
                my_bt_mod.cmd_users(args)
        mocked.assert_not_called()
        rows = json.loads(out.getvalue())
        self.assertNotIn("session", rows[0])


class BareCommandMainBehaviorTest(unittest.TestCase):
    """Drives my_bt_mod.main() directly (patching sys.argv), the same way
    an actual `my-bt ...` invocation would reach it."""

    def setUp(self):
        # main() peeks at --settings (whose default is the INSTALLED
        # /etc/my-booking/settings.toml) before dispatching, so without
        # this the test reads whatever config the machine running the
        # suite happens to have -- and fails on it if that file is
        # malformed. That is exactly what happened during an rpmbuild on
        # the server: a broken live settings.toml turned this test into an
        # error about TOML, nothing to do with argparse. Unit tests must
        # not depend on one machine's configuration; a path that cannot
        # exist keeps this one hermetic.
        patcher = mock.patch.object(
            my_bt_mod, "DEFAULT_SETTINGS", "/nonexistent/my-booking/settings.toml"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["my-bt"] + argv
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    my_bt_mod.main()
        finally:
            sys.argv = old_argv
        return ctx.exception.code, out.getvalue()

    def test_bare_my_bt_prints_help_and_exits_zero(self):
        code, output = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("usage: my-bt", output)
        self.assertIn("positional arguments", output)
        self.assertNotIn("error:", output)

    def test_bare_my_bt_admin_prints_help_and_exits_zero(self):
        code, output = self._run_main(["admin"])
        self.assertEqual(code, 0)
        self.assertIn("hash-password", output)
        self.assertIn("rename-course", output)
        self.assertIn("resync-calendar", output)
        self.assertNotIn("error:", output)

    def test_genuine_error_still_exits_nonzero_with_error_text(self):
        # A real usage error (missing email, positional or --email) must
        # still behave as an error -- this fix only covers the "no
        # sub-command given at all" case, not every argparse/runtime
        # failure. 2026-07-16: since --email is no longer `required=True`
        # at the argparse level (a plain positional EMAIL is now accepted
        # too, see cmd_erase()'s own docstring), a missing email is caught
        # by cmd_erase() itself at runtime (exit 1) rather than by
        # argparse during parsing (exit 2) -- still a clean, non-zero,
        # "error:"-prefixed failure either way, just a different code.
        old_argv = sys.argv
        sys.argv = ["my-bt", "admin", "gdpr", "erase"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    my_bt_mod.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("error:", err.getvalue())


class ListStatusChoicesTest(unittest.TestCase):
    """2026-07-14 (repo-review): `my-bt list --status` offered every status
    EXCEPT pending_confirmation -- a real, first-class status /admin's own
    table displays, with no way to filter on it from the CLI."""

    def test_every_storage_status_is_a_valid_list_status_filter(self):
        from app.storage import STATUS_LABELS

        parser = my_bt_mod.build_parser()
        for status in STATUS_LABELS:
            args = parser.parse_args(["list", "--status", status])
            self.assertEqual(args.status, status)


class NoStaleRemovedCommandReferencesTest(unittest.TestCase):
    """2026-07-14 (repo-wide review, finding G2): `my-bt list --all`'s help
    text and cmd_list's docstring still told operators to run `my-bt admin
    dearchive` -- a command deliberately REMOVED (2026-07-14) as a GDPR
    violation. Directing users to it was doubly wrong. This locks in that
    no user-facing help/docstring in scripts/my-bt ever points at it
    again (historical comments explaining the removal itself are fine --
    only text an operator is shown, or a function's own usage docstring,
    is checked here)."""

    def test_no_help_text_or_docstring_references_dearchive(self):
        parser = my_bt_mod.build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                parser.parse_args(["list", "--help"])
        self.assertNotIn("dearchive", out.getvalue())
        self.assertNotIn("dearchive", my_bt_mod.cmd_list.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
