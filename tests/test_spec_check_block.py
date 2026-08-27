"""The RPM's %check block (packaging/my-booking-tool.spec).

`%check` runs this suite during every build, printing one dot per test
and an "s" per skip. A lone "s" in a wall of dots is exactly the kind of
thing that gets wondered about at deploy time, so the spec states how
many to expect -- and this test keeps that statement from silently
rotting away when %check is next edited.

It deliberately does NOT assert a particular NUMBER. The expected count
is environment-dependent (root vs. not, Linux vs. macOS), so pinning one
here would just move the rot. What it guarantees is that the claim is
present, sits with the run it describes, and names a figure.

MAINTAINING IT: when a test that can skip is added or removed, update the
count and the reasons in the spec's %check comment. As of 2026-08-27
there is exactly one skip on an ordinary-user Linux build -- the
root-only secrets check in tests/test_real_settings.py -- and none when
built as root.
"""
import re
import unittest
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "packaging" / "my-booking-tool.spec"


class CheckBlockSkipExpectationTest(unittest.TestCase):
    def setUp(self):
        text = SPEC.read_text(encoding="utf-8")
        start = text.index("\n%check")
        self.block = text[start:text.index("\n%install", start)]

    RUN = "python3 -m unittest discover"

    def _run_line_index(self, lines):
        """Where the suite is actually run. Matched by SUBSTRING: the run
        is wrapped in a status-capturing group, so the line is no longer
        exactly the command."""
        return next(i for i, ln in enumerate(lines) if self.RUN in ln and "-v" not in ln)

    def test_the_suite_is_actually_run(self):
        self.assertIn(self.RUN, self.block)

    def test_the_expected_skip_count_is_stated(self):
        self.assertRegex(
            self.block,
            r"expecting\s+(\d+|\$expected_skips)\s+skipped",
            "the %check block must say how many skips to expect -- a bare "
            '"s" among the dots is otherwise unexplained at build time',
        )

    def test_the_statement_is_printed_not_only_commented(self):
        # A comment is invisible in the build log, which is the one place
        # the count is actually needed.
        printed = [
            line for line in self.block.splitlines()
            if line.strip().startswith("echo") and "skipped" in line
        ]
        self.assertTrue(printed, "the expectation must be echoed, not just commented")

    def test_nothing_runs_between_the_statement_and_the_suite(self):
        # The point is that the expectation is the last thing said before
        # the dots start, so it cannot drift away from what it explains.
        # It may be more than one line -- it also says how to avoid the
        # skip -- so what is asserted is that everything between the
        # claim and the run is more of the same message, not a command.
        lines = [
            ln.strip() for ln in self.block.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        run = self._run_line_index(lines)
        claim = max(i for i, ln in enumerate(lines[:run]) if "skipped" in ln)
        for line in lines[claim + 1:run]:
            self.assertTrue(
                line.startswith("echo"),
                f"only more of the message may sit between the expectation and the "
                f"run, found: {line!r}",
            )

    def test_it_says_how_to_avoid_the_skip(self):
        # Knowing a skip is expected is half of it; the other half is what
        # to do about it, which here is "build as root -- or don't".
        self.assertIn("root", self.block)

    def test_the_count_is_enforced_not_merely_announced(self):
        # 2026-08-27, the operator: "the OK should take into consideration
        # how many skipped are expected -- if it is one more than
        # expected it should not be OK". unittest exits 0 whether it ran
        # every test or skipped half of them, so the exit status alone
        # cannot tell a green build from a quietly-shrinking one.
        self.assertIn("actual_skips", self.block)
        self.assertIn("expected_skips", self.block)
        self.assertRegex(
            self.block, r'\[ "\$actual_skips" -ne "\$expected_skips" \]',
            "the block must compare the two and fail the build when they differ",
        )
        self.assertIn("exit 1", self.block)

    def test_the_expectation_adjusts_for_a_root_build(self):
        # Building as root CAN see /etc/my-booking/secrets, so the secrets
        # check runs and the right expectation is zero -- hard-coding 1
        # would fail every root build.
        self.assertRegex(self.block, r'id -u.*\n?.*expected_skips=0|expected_skips=0')

    def test_the_suite_status_still_gates_the_build(self):
        # Counting skips must not accidentally swallow a real failure:
        # the suite's own exit status is checked first, and through a
        # POSIX status file rather than the bash-only PIPESTATUS, since
        # %check runs under /bin/sh.
        self.assertIn("unittest-status", self.block)
        # Mentioned in a comment (explaining why it is not used) is fine;
        # actually USING it is the bashism that would break under
        # /bin/sh.
        self.assertNotIn("${PIPESTATUS", self.block)

    def test_it_points_at_how_to_see_which_test_skipped(self):
        self.assertIn("--- skipped".replace("---", r"\.\.\."), self.block)


if __name__ == "__main__":
    unittest.main()
