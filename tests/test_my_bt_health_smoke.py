"""`my-bt admin health` runs end to end.

Why this exists: the health path is a long branch of a script the suite
never executed, so a name that does not exist in its scope was shipped
and only surfaced on the server, after an install -- `error: name
'settings_path' is not defined`, with every check above it already
printed. Nothing here asserts on the CHECKS themselves (they have their
own tests in test_cli_checks.py); this asserts only that the command
gets through its own code."""
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_script() -> types.ModuleType:
    os.environ["MY_BOOKING_HOME"] = str(REPO)
    mod = types.ModuleType("my_bt_script")
    mod.__file__ = str(REPO / "scripts" / "my-bt")
    argv = sys.argv
    sys.argv = ["my-bt"]
    try:
        exec(compile((REPO / "scripts" / "my-bt").read_text(), mod.__file__, "exec"), mod.__dict__)
    finally:
        sys.argv = argv
    return mod


class HealthSmokeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "data").mkdir()
        (self.dir / "settings.toml").write_text(
            '[site]\ntimezone = "UTC"\nadmin_email = "a@example.org"\n'
            'base_url = "https://booking.example.org"\n'
            '[[course]]\nshortname = "c"\ntitle = "C"\nlocation = "L"\n'
            'weekday = "wed"\nstart_time = "18:00"\nduration_minutes = 60\ncapacity = 5\n'
        )

    def _run(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        mod = _load_script()
        args = types.SimpleNamespace(
            settings=str(self.dir / "settings.toml"), data_dir=str(self.dir / "data"),
            json=False, debug=False, quiet=False,
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                mod.cmd_admin_health(args)
            except SystemExit:
                pass
        return out.getvalue()

    def test_courses_in_the_web_editable_file_count_as_configured(self):
        """After moving courses to the console, settings.toml has none --
        and reporting that as "no courses found" told the operator their
        courses were gone while the site was serving all four."""
        editable = self.dir / "web-editable"
        editable.mkdir()
        (editable / "settings.web-editable.toml").write_text(
            '[[course]]\nshortname = "from-console"\ntitle = "C"\nlocation = "L"\n'
            'weekday = "wed"\nstart_time = "18:00"\nduration_minutes = 60\ncapacity = 5\n')
        (self.dir / "settings.toml").write_text(
            '[site]\ntimezone = "UTC"\nadmin_email = "a@example.org"\n'
            'base_url = "https://booking.example.org"\n')
        report = self._run()
        self.assertIn("from-console", report)
        self.assertNotIn("no [[course]] blocks", report)

    def test_no_courses_anywhere_is_still_a_warning(self):
        (self.dir / "settings.toml").write_text(
            '[site]\ntimezone = "UTC"\nadmin_email = "a@example.org"\n'
            'base_url = "https://booking.example.org"\n')
        self.assertIn("no [[course]] blocks in either settings file", self._run())

    def test_health_runs_without_an_undefined_name(self):
        mod = _load_script()
        args = types.SimpleNamespace(
            settings=str(self.dir / "settings.toml"), data_dir=str(self.dir / "data"),
            json=False, debug=False, quiet=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                mod.cmd_admin_health(args)
            except SystemExit:
                pass          # warnings on a fixture exit non-zero: expected
            except NameError as exc:
                self.fail(f"health hit an undefined name: {exc}")
        # It got far enough to print its own report, not just to blow up.
        self.assertIn("settings.toml", out.getvalue())


if __name__ == "__main__":
    unittest.main()
