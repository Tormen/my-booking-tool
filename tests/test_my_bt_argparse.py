"""Tests for scripts/my-bt's own argparse wiring -- specifically the
2026-07-09 fix for bare `my-bt` / `my-bt admin` (or any other group that
needs a further sub-command): these must behave exactly like `-h`/`--help`
(full help text, exit 0), not argparse's default "the following arguments
are required" error (exit 2). the operator: "please remove the error message...
as the --help is displayed to me this is not an error, no need to write
this."

scripts/my-bt has no .py extension and lives outside app/, so it can't be
imported with a plain `import` statement -- loaded here via
importlib.machinery.SourceFileLoader, the same approach used for ad hoc
manual smoke-testing of this script during development (see other
the maintainer's local notes notes); this is the first time that loader pattern is
checked into a committed test file.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest

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

    def test_resync_calendar_leaf_command_sets_func(self):
        # 2026-07-09, the operator: "please ensure that the existing (future)
        # calendar invites are updated as well" -- see
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
        # moved/renamed per the operator's own restructure -- see
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
        # 2026-07-14, the operator: "And lets move this to my-admin gdpr please:
        # erase manually erase an attendee's data (GDPR Art. 17)." --
        # moved from `admin erase` to sit alongside bookings/accounts.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "erase", "--email", "guest@example.com", "--yes"])
        self.assertTrue(hasattr(args, "func"))
        self.assertEqual(args.func, my_bt_mod.cmd_erase)
        self.assertEqual(args.email_flag, "guest@example.com")
        self.assertIsNone(args.email_pos)
        self.assertTrue(args.yes)

    def test_admin_gdpr_erase_accepts_a_positional_email_too(self):
        # 2026-07-16, the operator: "--email should be optional here simply, my-bt
        # admin erase operator@example.org should work as well" -- the email can
        # now be given as a plain positional argument instead of always
        # needing --email spelled out.
        parser = my_bt_mod.build_parser()
        args = parser.parse_args(["admin", "gdpr", "erase", "guest@example.com", "--yes"])
        self.assertEqual(args.func, my_bt_mod.cmd_erase)
        self.assertEqual(args.email_pos, "guest@example.com")
        self.assertIsNone(args.email_flag)
        self.assertTrue(args.yes)


class AdminLogoutArgsTest(unittest.TestCase):
    """2026-07-10, the operator: `my-bt admin logout EMAIL` / `--all` -- the
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


class GitSnapshotMessageArgTest(unittest.TestCase):
    """2026-07-14, the operator, pasting `my-bt admin git-snapshot -h` output:
    "add optional message parameter" -- -m/--message lets a manual snapshot
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


class UsersScopeArgsTest(unittest.TestCase):
    """2026-07-14, the operator, changing `my-bt users`'s bare-default scope:
    "actually no: by default my-bt users should show --live users that
    logged in (that has a last_login date) Then you add --all to show
    both --live and --archived" -- confirmed: "--live shows ALL live
    users also the ones that have no login date / --archive shows only
    the archived users / --all shows both / it all makes sense, no?"
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
        # as unrelated-looking noise (the operator, screenshot of a real rpmbuild
        # log: "one test is not silent :)"). Same io.StringIO()-via-
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


class BareCommandMainBehaviorTest(unittest.TestCase):
    """Drives my_bt_mod.main() directly (patching sys.argv), the same way
    an actual `my-bt ...` invocation would reach it."""

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


if __name__ == "__main__":
    unittest.main()
