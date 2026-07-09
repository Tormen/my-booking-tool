import tempfile
import unittest
from pathlib import Path

from app import maintenance


class ReadStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_no_flag_file_is_off(self):
        state = maintenance.read_state(self.data_dir)
        self.assertFalse(state.enabled)

    def test_corrupt_flag_file_is_treated_as_off(self):
        # Fail open, not closed -- a broken flag file must never itself
        # become the reason bookings stay blocked forever.
        maintenance.flag_path(self.data_dir).write_text("not json{{{", encoding="utf-8")
        state = maintenance.read_state(self.data_dir)
        self.assertFalse(state.enabled)

    def test_enable_then_read_round_trips(self):
        maintenance.enable(self.data_dir, message="back Monday")
        state = maintenance.read_state(self.data_dir)
        self.assertTrue(state.enabled)
        self.assertEqual(state.message, "back Monday")
        self.assertTrue(state.set_at)  # non-empty ISO timestamp

    def test_enable_creates_data_dir_if_missing(self):
        nested = self.data_dir / "not" / "yet" / "created"
        maintenance.enable(nested, message="")
        self.assertTrue(maintenance.read_state(nested).enabled)

    def test_disable_removes_the_flag_file(self):
        maintenance.enable(self.data_dir, message="x")
        maintenance.disable(self.data_dir)
        self.assertFalse(maintenance.read_state(self.data_dir).enabled)

    def test_disable_when_never_enabled_is_a_silent_noop(self):
        maintenance.disable(self.data_dir)  # must not raise
        self.assertFalse(maintenance.read_state(self.data_dir).enabled)

    def test_enable_leaves_no_temp_file_behind(self):
        # 2026-07-15: enable() writes via atomic_io.atomic_write_text
        # (temp file + fsync + rename), not a bare write_text() -- confirm
        # the temp file it creates along the way doesn't linger.
        maintenance.enable(self.data_dir, message="x")
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name != "maintenance.json"]
        self.assertEqual(leftovers, [])

    def test_disable_fsyncs_the_data_dir_after_unlinking(self):
        # 2026-07-15: the deletion itself should be as durable as the
        # write was -- see maintenance.disable()'s own comment.
        from unittest import mock

        maintenance.enable(self.data_dir, message="x")
        with mock.patch("app.maintenance.fsync_dir") as m_fsync_dir:
            maintenance.disable(self.data_dir)
        m_fsync_dir.assert_called_once_with(self.data_dir)


class MessageHtmlTest(unittest.TestCase):
    def test_includes_mailto_link_and_teams_note(self):
        html = maintenance.message_html("admin@example.org")
        self.assertIn('<a href="mailto:admin@example.org">admin@example.org</a>', html)
        self.assertIn("Teams", html)
        self.assertIn("DBG Lux", html)

    def test_custom_message_is_included_and_escaped(self):
        html = maintenance.message_html("a@example.org", "Back <b>Monday</b>")
        self.assertIn("&lt;b&gt;Monday&lt;/b&gt;", html)
        self.assertNotIn("<b>Monday</b>", html)

    def test_no_custom_message_omits_the_extra_paragraph(self):
        with_msg = maintenance.message_html("a@example.org", "hi")
        without_msg = maintenance.message_html("a@example.org")
        self.assertIn("<p>hi</p>", with_msg)
        self.assertNotIn("<p>hi</p>", without_msg)


class BannerInsertRemoveTest(unittest.TestCase):
    """insert_banner()/remove_banner() operate on raw html strings --
    apply_banner_to_file() (tested separately below) is the file-level
    wrapper `my-bt maintenance` actually calls."""

    def _page(self):
        return "<html><head><title>x</title></head>\n<body>\n<h1>hi</h1>\n</body></html>"

    def test_insert_places_banner_right_after_body_tag(self):
        html = maintenance.insert_banner(self._page(), maintenance.banner_html("a@example.org"))
        body_idx = html.index("<body>")
        banner_idx = html.index(maintenance._BANNER_START)
        h1_idx = html.index("<h1>")
        self.assertTrue(body_idx < banner_idx < h1_idx)

    def test_insert_is_idempotent(self):
        once = maintenance.insert_banner(self._page(), maintenance.banner_html("a@example.org", "x"))
        twice = maintenance.insert_banner(once, maintenance.banner_html("a@example.org", "x"))
        self.assertEqual(once, twice)
        self.assertEqual(once.count(maintenance._BANNER_START), 1)

    def test_insert_replaces_an_existing_banner_rather_than_duplicating(self):
        first = maintenance.insert_banner(self._page(), maintenance.banner_html("a@example.org", "old message"))
        updated = maintenance.insert_banner(first, maintenance.banner_html("a@example.org", "new message"))
        self.assertEqual(updated.count(maintenance._BANNER_START), 1)
        self.assertIn("new message", updated)
        self.assertNotIn("old message", updated)

    def test_remove_strips_the_banner_entirely(self):
        with_banner = maintenance.insert_banner(self._page(), maintenance.banner_html("a@example.org"))
        removed = maintenance.remove_banner(with_banner)
        self.assertNotIn(maintenance._BANNER_START, removed)
        self.assertNotIn(maintenance._BANNER_END, removed)

    def test_remove_when_no_banner_present_is_a_noop(self):
        page = self._page()
        self.assertEqual(maintenance.remove_banner(page), page)

    def test_on_then_off_round_trips_to_byte_identical_original(self):
        # Regression coverage: an earlier version of insert_banner() added
        # one stray leading newline per on/off cycle that remove_banner()
        # never cleaned back up, so repeated toggling slowly accumulated
        # blank lines above the banner's insertion point forever.
        original = self._page()
        on = maintenance.insert_banner(original, maintenance.banner_html("a@example.org", "msg"))
        off = maintenance.remove_banner(on)
        self.assertEqual(off, original)

    def test_no_body_tag_falls_back_to_prepending(self):
        bare = "<h1>no body tag here</h1>"
        html = maintenance.insert_banner(bare, maintenance.banner_html("a@example.org"))
        self.assertTrue(html.startswith(maintenance._BANNER_START))
        self.assertIn("no body tag here", html)


class ApplyBannerToFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "index.html"

    def test_missing_file_is_a_noop_returns_false(self):
        result = maintenance.apply_banner_to_file(self.path, True, "a@example.org")
        self.assertFalse(result)
        self.assertFalse(self.path.exists())

    def test_enabling_writes_the_banner_and_returns_true(self):
        self.path.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
        result = maintenance.apply_banner_to_file(self.path, True, "a@example.org", "back soon")
        self.assertTrue(result)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(maintenance._BANNER_START, text)
        self.assertIn("back soon", text)

    def test_enabling_again_with_same_message_reports_no_change(self):
        self.path.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
        maintenance.apply_banner_to_file(self.path, True, "a@example.org", "back soon")
        result = maintenance.apply_banner_to_file(self.path, True, "a@example.org", "back soon")
        self.assertFalse(result)

    def test_disabling_removes_the_banner_and_returns_true(self):
        self.path.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
        maintenance.apply_banner_to_file(self.path, True, "a@example.org")
        result = maintenance.apply_banner_to_file(self.path, False, "a@example.org")
        self.assertTrue(result)
        self.assertNotIn(maintenance._BANNER_START, self.path.read_text(encoding="utf-8"))

    def test_disabling_when_already_clean_reports_no_change(self):
        self.path.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
        result = maintenance.apply_banner_to_file(self.path, False, "a@example.org")
        self.assertFalse(result)

    def test_writes_via_atomic_write_text_leaving_no_temp_file_behind(self):
        # 2026-07-15: this rewrites the LIVE, publicly-served homepage in
        # place -- must go through atomic_io.atomic_write_text (temp file
        # + fsync + rename), not a bare write_text(), since a torn write
        # here has no fail-open fallback the way the maintenance flag
        # file does.
        self.path.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
        maintenance.apply_banner_to_file(self.path, True, "a@example.org")
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != "index.html"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
