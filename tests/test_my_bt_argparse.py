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
        self.assertEqual(args.email, "guest@example.com")
        self.assertTrue(args.yes)


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
        # A real usage error (missing required --email) must still behave
        # as an error -- this fix only covers the "no sub-command given
        # at all" case, not every argparse failure.
        old_argv = sys.argv
        sys.argv = ["my-bt", "admin", "gdpr", "erase"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    my_bt_mod.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("error:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
