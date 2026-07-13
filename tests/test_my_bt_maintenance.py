"""cmd_maintenance (scripts/my-bt) -- 2026-07-16: extended to also patch
site/index_embedded.html (an optional second generated page, see
app/site_render.py's own docstring), alongside the existing index.html
target. scripts/my-bt has no .py extension and lives outside `app/`, so
unittest can't import it directly -- same importlib.machinery.SourceFileLoader
workaround tests/test_my_bt_argparse.py already established (see that
file's own docstring)."""
import importlib.machinery
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path

MY_BT_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "my-bt")
_loader = importlib.machinery.SourceFileLoader("my_bt_maintenance_test_mod", MY_BT_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
my_bt_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(my_bt_mod)


class CmdMaintenanceTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.static_dir = self.dir / "live"
        self.static_dir.mkdir()
        self.data_dir = self.dir / "data"
        self.data_dir.mkdir()
        self.settings_path = self.dir / "settings.toml"
        self.settings_path.write_text(
            f'[site]\nadmin_email = "admin@example.org"\n'
            f'static_site_dir = "{self.static_dir}"\n',
            encoding="utf-8",
        )

    def _args(self, maintenance_command: str, message: str = "") -> types.SimpleNamespace:
        return types.SimpleNamespace(
            maintenance_command=maintenance_command, message=message,
            settings=str(self.settings_path), data_dir=str(self.data_dir),
        )

    def test_on_banners_index_embedded_html_alongside_index_html(self):
        (self.static_dir / "index.html").write_text("<html><body>hello</body></html>")
        (self.static_dir / "index_embedded.html").write_text("<html><body>hello embedded</body></html>")
        my_bt_mod.cmd_maintenance(self._args("on", message="back Monday"))
        self.assertIn("MAINTENANCE-BANNER:START", (self.static_dir / "index.html").read_text())
        self.assertIn("MAINTENANCE-BANNER:START", (self.static_dir / "index_embedded.html").read_text())

    def test_off_removes_it_from_both(self):
        (self.static_dir / "index.html").write_text("<html><body>hello</body></html>")
        (self.static_dir / "index_embedded.html").write_text("<html><body>hello embedded</body></html>")
        my_bt_mod.cmd_maintenance(self._args("on", message="back Monday"))
        my_bt_mod.cmd_maintenance(self._args("off"))
        self.assertNotIn("MAINTENANCE-BANNER", (self.static_dir / "index.html").read_text())
        self.assertNotIn("MAINTENANCE-BANNER", (self.static_dir / "index_embedded.html").read_text())

    def test_index_embedded_html_not_existing_yet_is_not_an_error(self):
        # The common case: index_embedded.html is optional and most
        # deployments never generate it -- apply_banner_to_file() no-ops on
        # a missing target, same as it already does for index.html before
        # it's ever been deployed.
        (self.static_dir / "index.html").write_text("<html><body>hello</body></html>")
        my_bt_mod.cmd_maintenance(self._args("on", message="back Monday"))
        self.assertIn("MAINTENANCE-BANNER:START", (self.static_dir / "index.html").read_text())
        self.assertFalse((self.static_dir / "index_embedded.html").exists())


if __name__ == "__main__":
    unittest.main()
