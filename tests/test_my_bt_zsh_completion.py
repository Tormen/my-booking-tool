"""Tests for scripts/my-bt's zsh completion generator (generate_zsh_
completion, --print-zsh-completion) -- 2026-07-08/09: a zsh compatible
shell auto-complete was requested to be built into the script, shipped via
the rpm package, and to work over all levels -- as much autocomplete as
possible, including recognizing a course-name where possible.

No real zsh is available in this environment to actually source/exercise
the generated script, so these tests are structural: they check the
output is well-formed zsh _arguments/_describe syntax (balanced braces,
every spec line in a multi-line _arguments call backslash-continued
except the last, no unescaped colons in a positional's message field --
see generate_zsh_completion's own docstring for why that specific rule
matters) rather than actually running it. The operator's own
rebuild/install cycle is what actually exercises this against a real zsh
(see feedback_run_targeted_tests_during_dev -- full suite + real usage is
the operator's own job at install time, not this repo's job to re-verify
here).

scripts/my-bt has no .py extension and lives outside app/, so it's loaded
via importlib.machinery.SourceFileLoader, same as test_my_bt_argparse.py.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY_BT_PATH = os.path.join(REPO_ROOT, "scripts", "my-bt")

_loader = importlib.machinery.SourceFileLoader("my_bt_zsh_completion_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


class GenerateZshCompletionStructureTest(unittest.TestCase):
    """Structural well-formedness of the generated script -- see this
    module's own docstring for why these checks are regex-based rather
    than an actual zsh parse."""

    @classmethod
    def setUpClass(cls):
        cls.script = my_bt_mod.generate_zsh_completion(my_bt_mod.build_parser())

    def test_starts_with_compdef_pragma(self):
        self.assertTrue(self.script.startswith("#compdef my-bt\n"))

    def test_braces_are_balanced(self):
        self.assertEqual(self.script.count("{"), self.script.count("}"))

    def test_every_arguments_block_line_is_backslash_continued_except_the_last(self):
        # Scans each `_arguments -C \` / `_arguments -s \` block: every
        # subsequent single-quote-led spec line must end in ` \` EXCEPT
        # the line that ends the block (the next non-spec-shaped line).
        lines = self.script.split("\n")
        i = 0
        violations = []
        while i < len(lines):
            if re.search(r"_arguments (-C|-s) \\$", lines[i]):
                i += 1
                block = []
                while i < len(lines) and re.match(r"^\s*'", lines[i]):
                    block.append(lines[i])
                    i += 1
                for line in block[:-1]:
                    if not line.endswith("\\"):
                        violations.append(line)
                continue
            i += 1
        self.assertEqual(violations, [], f"non-continued lines mid-block: {violations}")

    def test_multi_alias_option_specs_have_unquoted_brace_expansion(self):
        # A REAL bug, caught on the actual VPS (not by this test suite,
        # since there's no zsh here to execute the output against): an
        # earlier version emitted '(-D --debug){-D,--debug}[desc]' -- the
        # {a,b} alias-expansion swallowed into the SAME single-quoted
        # string as the rest of the spec. Quotes suppress shell brace
        # expansion, so zsh's own _arguments received that whole thing as
        # ONE literal, unparseable argument ("invalid argument: (-D
        # --debug){-D,--debug}[...]"). The fix keeps {a,b} OUTSIDE any
        # quotes -- '(-D --debug)'{-D,--debug}'[desc]' -- three
        # concatenated shell words, the middle one a bare brace
        # expansion, exactly matching e.g. git's/brew's own zsh
        # completion functions' idiom for "these aliases share one
        # description".
        bad = re.search(r"'\([^']*\)\{", self.script)
        self.assertIsNone(bad, f"brace-expansion group still inside quotes: {bad.group(0) if bad else ''!r}")
        # And confirm the CORRECT shape actually appears at least once
        # (i.e. this isn't just trivially passing because no multi-alias
        # options exist at all -- --debug/-D is always present).
        self.assertRegex(self.script, r"'\([^']*\)'\{[^}]*\}'\[")

    def test_no_unescaped_colon_in_positional_message_field(self):
        # For every 'N:message:action' positional spec, the message field
        # (between the first and second colon) must have no BARE colons
        # -- only backslash-escaped ones (see _zsh_positional_specs's own
        # docstring on why this differs from the '[description]' bracket
        # syntax option specs use).
        for m in re.finditer(r"'(\d+):((?:[^:\\]|\\.)*):", self.script):
            message = m.group(2)
            unescaped = re.sub(r"\\:", "", message)
            self.assertNotIn(":", unescaped, f"unescaped colon in positional message: {message!r}")

    def test_course_option_gets_value_completion(self):
        self.assertIn(":course:_mb_course_shortnames", self.script)

    def test_maintenance_positional_gets_exact_choices(self):
        self.assertIn("(on off status)", self.script)

    def test_course_shortnames_helper_function_defined(self):
        self.assertIn("_mb_course_shortnames() {", self.script)

    def test_every_top_level_command_appears_in_the_dispatch_case(self):
        # Every real subcommand name from build_parser()'s own choices
        # must show up as a `case` label in the generated script.
        parser = my_bt_mod.build_parser()
        top_choices = my_bt_mod._zsh_subparser_choices(parser)
        for name in top_choices:
            self.assertIn(f"        {name})", self.script, f"missing case label for {name!r}")

    def test_admin_nested_subcommands_all_appear(self):
        parser = my_bt_mod.build_parser()
        admin_sp = my_bt_mod._zsh_subparser_choices(parser)["admin"]
        for name in my_bt_mod._zsh_subparser_choices(admin_sp):
            self.assertIn(f"            {name})", self.script, f"missing admin sub-case label for {name!r}")


class PrintZshCompletionFlagTest(unittest.TestCase):
    """`my-bt --print-zsh-completion` -- the RPM %build hook, see
    packaging/my-booking-tool.spec's own %build section."""

    def test_prints_the_generated_script_and_exits_cleanly(self):
        old_argv = sys.argv
        sys.argv = ["my-bt", "--print-zsh-completion"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                my_bt_mod.main()  # returns normally, no sys.exit -- not a subcommand
        finally:
            sys.argv = old_argv
        self.assertTrue(out.getvalue().startswith("#compdef my-bt\n"))

    def test_not_listed_as_a_real_subcommand(self):
        # Deliberately kept OUT of build_parser()'s own subparsers (see
        # main()'s own comment) -- shouldn't clutter `my-bt -h`'s command
        # listing or be discoverable as a normal subcommand.
        parser = my_bt_mod.build_parser()
        self.assertNotIn("print-zsh-completion", my_bt_mod._zsh_subparser_choices(parser))
        self.assertNotIn("print_zsh_completion", vars(parser.parse_args([])))


if __name__ == "__main__":
    unittest.main()
