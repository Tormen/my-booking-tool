import tempfile
import unittest
from pathlib import Path

from app import site_render
from app.config import Course, CourseDateOverride


class ExtractScriptBodiesTest(unittest.TestCase):
    """extract_script_bodies() -- used by app.cli_checks.expected_csp_hashes()
    to compute index.html's own CSP hashes automatically instead of by hand
    (2026-07-13, after this exact class of bug -- a forgotten hash update --
    hit production multiple times)."""

    def test_returns_each_script_body_in_document_order(self):
        html = "<script>one();</script><div></div><script>two();</script>"
        self.assertEqual(site_render.extract_script_bodies(html), ["one();", "two();"])

    def test_no_scripts_returns_empty_list(self):
        self.assertEqual(site_render.extract_script_bodies("<html><body>hi</body></html>"), [])

    def test_tags_and_attributes_are_excluded_from_the_body(self):
        html = '<script type="text/javascript">real_body();</script>'
        self.assertEqual(site_render.extract_script_bodies(html), ["real_body();"])

    def test_html_comment_mentioning_script_is_not_mistaken_for_a_real_tag(self):
        # Real incident (2026-07-13): a literal "<script>" mentioned in
        # developer-authored prose inside an HTML comment was mistaken for
        # a real opening tag by a naive regex, which then matched all the
        # way to the NEXT real closing tag, silently swallowing everything
        # in between (including a whole page's worth of markup). Stripping
        # comments FIRST (same as derive_index_embedded_html() above) fixes
        # this for good.
        html = (
            "<!-- the real <script> further down does the thing -->"
            "<script>real();</script>"
        )
        self.assertEqual(site_render.extract_script_bodies(html), ["real();"])


class RenderPrivacyHtmlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpl_path = Path(self._tmp.name) / "privacy.html.tmpl"
        self.tmpl_path.write_text(
            "<html><body>kept for ${retention_months} months "
            "(${canceled_retention_months} if canceled)</body></html>",
            encoding="utf-8",
        )

    def test_substitutes_both_placeholders(self):
        out = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        self.assertIn("kept for 24 months", out)
        self.assertIn("6 if canceled", out)

    def test_output_carries_managed_marker(self):
        out = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        self.assertIn("MANAGED BY my-bt", out)
        self.assertTrue(out.startswith(site_render.MANAGED_MARKER))

    def test_different_settings_produce_different_output(self):
        default = site_render.render_privacy_html(self.tmpl_path, 24, 6)
        custom = site_render.render_privacy_html(self.tmpl_path, 36, 12)
        self.assertNotEqual(default, custom)
        self.assertIn("36 months", custom)

    def test_write_privacy_html_writes_rendered_content(self):
        out_path = Path(self._tmp.name) / "privacy.html"
        site_render.write_privacy_html(self.tmpl_path, 24, 6, out_path)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("kept for 24 months", content)
        self.assertIn("MANAGED BY my-bt", content)

    def test_write_privacy_html_leaves_no_temp_file_behind(self):
        # 2026-07-15: this is the live, publicly-served privacy.html --
        # goes through atomic_io.atomic_write_text (temp file + fsync +
        # rename), not a bare write_text().
        out_path = Path(self._tmp.name) / "privacy.html"
        site_render.write_privacy_html(self.tmpl_path, 24, 6, out_path)
        leftover_tmps = [p.name for p in Path(self._tmp.name).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftover_tmps, [])


def _course(shortname="sat-trier", **overrides):
    kwargs = dict(
        shortname=shortname, title="Yoga", location="Trier", weekday="sat",
        start_time="10:45", duration_minutes=120, capacity=10,
    )
    kwargs.update(overrides)
    return Course(**kwargs)


_SAMPLE_INDEX_HTML = """<html><body>
<div class="top-bar" id="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
<div id="schedule-exceptions"></div>
<h1>Site</h1>
<ul>
<li><a href="/book/sat-trier">Book your place here.</a></li>
</ul>
<footer>
<a href="/terms.html">Terms</a>
<a href="/privacy.html">Privacy</a>
<a href="/impressum.html">Impressum</a>
<a href="https://example.org/unrelated">Unrelated external link</a>
<a href="mailto:someone@example.org">Email us</a>
</footer>
<script>
(function () { fetch('/my/session', { credentials: 'same-origin' }); })();
</script>
<script>
(function () { fetch('/schedule-exceptions', { credentials: 'same-origin' }); })();
</script>
</body></html>
"""


class DeriveIndexEmbeddedHtmlTest(unittest.TestCase):
    def test_strips_every_script_block(self):
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        self.assertNotIn("<script>", out)
        self.assertNotIn("fetch(", out)

    def test_retargets_known_app_links_new_tab_by_default(self):
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        self.assertIn('href="/my" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="/book/sat-trier" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="/terms.html" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="/privacy.html" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="/impressum.html" target="_blank" rel="noopener noreferrer"', out)

    def test_retargets_to_same_tab_when_new_tab_links_false(self):
        out = site_render.derive_index_embedded_html(
            _SAMPLE_INDEX_HTML, (), "2026-07-10", new_tab_links=False,
        )
        self.assertIn('href="/my" target="_top"', out)
        self.assertIn('href="/book/sat-trier" target="_top"', out)
        self.assertNotIn("rel=\"noopener noreferrer\"", out)

    def test_leaves_unrelated_links_untouched(self):
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        self.assertIn('<a href="https://example.org/unrelated">Unrelated external link</a>', out)
        self.assertIn('<a href="mailto:someone@example.org">Email us</a>', out)

    def test_base_url_prefixed_links_also_match(self):
        html = _SAMPLE_INDEX_HTML.replace('href="/my"', 'href="https://example.org/my"')
        out = site_render.derive_index_embedded_html(
            html, (), "2026-07-10", base_url="https://example.org",
        )
        self.assertIn('href="https://example.org/my" target="_blank" rel="noopener noreferrer"', out)

    def test_no_upcoming_overrides_leaves_empty_marker_div(self):
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        self.assertIn('<div id="schedule-exceptions"></div>', out)
        self.assertNotIn("ATTENTION", out)

    def test_upcoming_override_splices_attention_banner(self):
        course = _course(date_overrides=(
            CourseDateOverride(date="2026-07-18", start_time="09:45", message="Back at 13h."),
        ))
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (course,), "2026-07-10")
        self.assertIn("ATTENTION", out)
        self.assertIn("2026-07-18", out)
        self.assertIn("9h45 - 11h45", out)
        self.assertIn("Back at 13h.", out)
        self.assertIn('href="/book/sat-trier" target="_blank" rel="noopener noreferrer">details', out)

    def test_banner_leads_with_bold_weekday_in_a_bullet_list(self):
        # 2026-07-13: the site-wide banner (unlike the per-course one on
        # /book/<shortname>) leads each line with the WEEKDAY, bold, then
        # the date, then the course name -- and always uses a <ul>/<li>
        # list, even for a single item, so several exceptions read as a
        # clean list rather than several stacked boxes.
        course = _course(date_overrides=(
            CourseDateOverride(date="2026-07-18", start_time="09:45", message="Back at 13h."),
        ))
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (course,), "2026-07-10")
        self.assertIn("<ul><li><b>Saturday</b>, 2026-07-18: Yoga starts at", out)
        # Exactly one .attention box, not one per item.
        self.assertEqual(out.count('<div class="attention">'), 1)

    def test_multiple_exceptions_share_one_box_as_separate_list_items(self):
        course_a = _course(shortname="sat-trier", date_overrides=(
            CourseDateOverride(date="2026-07-18", start_time="09:45"),
        ))
        course_b = _course(shortname="wed-lux", weekday="wed", date_overrides=(
            CourseDateOverride(date="2026-07-15", start_time="17:00"),
        ))
        html = _SAMPLE_INDEX_HTML.replace(
            '<li><a href="/book/sat-trier">Book your place here.</a></li>',
            '<li><a href="/book/sat-trier">Book your place here.</a></li>'
            '<li><a href="/book/wed-lux">Book your place here.</a></li>',
        )
        out = site_render.derive_index_embedded_html(html, (course_a, course_b), "2026-07-10")
        self.assertEqual(out.count('<div class="attention">'), 1)
        self.assertEqual(out.count("<li><b>"), 2)
        self.assertIn("<b>Wednesday</b>, 2026-07-15", out)
        self.assertIn("<b>Saturday</b>, 2026-07-18", out)

    def test_custom_attention_message_appended_after_hr_when_items_exist(self):
        course = _course(date_overrides=(
            CourseDateOverride(date="2026-07-18", start_time="09:45"),
        ))
        out = site_render.derive_index_embedded_html(
            _SAMPLE_INDEX_HTML, (course,), "2026-07-10",
            custom_attention_message="On vacation from <b>2026-08-01</b>.",
        )
        self.assertEqual(out.count('<div class="attention">'), 1)
        self.assertIn("</ul><hr>On vacation from <b>2026-08-01</b>.", out)

    def test_custom_attention_message_alone_with_no_items_no_hr(self):
        out = site_render.derive_index_embedded_html(
            _SAMPLE_INDEX_HTML, (), "2026-07-10",
            custom_attention_message="On vacation.",
        )
        self.assertIn('<div class="attention"><b>⚠ ATTENTION:</b> On vacation.</div>', out)
        self.assertNotIn("<hr>", out)

    def test_banner_link_follows_new_tab_links_setting_too(self):
        course = _course(date_overrides=(
            CourseDateOverride(date="2026-07-18", start_time="09:45"),
        ))
        out = site_render.derive_index_embedded_html(
            _SAMPLE_INDEX_HTML, (course,), "2026-07-10", new_tab_links=False,
        )
        self.assertIn('target="_top">details', out)

    def test_past_override_is_filtered_out(self):
        course = _course(date_overrides=(
            CourseDateOverride(date="2026-07-01", start_time="09:45"),
        ))
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (course,), "2026-07-10")
        self.assertNotIn("ATTENTION", out)

    def test_output_carries_embedded_managed_marker(self):
        out = site_render.derive_index_embedded_html(_SAMPLE_INDEX_HTML, (), "2026-07-10")
        self.assertTrue(out.startswith(site_render.EMBEDDED_MANAGED_MARKER))
        self.assertIn("index.html", site_render.EMBEDDED_MANAGED_MARKER)

    def test_missing_schedule_exceptions_marker_raises(self):
        html = _SAMPLE_INDEX_HTML.replace('<div id="schedule-exceptions"></div>', "")
        with self.assertRaises(site_render.IndexEmbeddedDerivationError):
            site_render.derive_index_embedded_html(html, (), "2026-07-10")

    def test_missing_both_expected_scripts_raises(self):
        html = _SAMPLE_INDEX_HTML.replace("/my/session", "/x").replace("/schedule-exceptions", "/y")
        # Remove the marker div too so the schedule-exceptions check (which
        # runs first) doesn't mask this one -- keep it, just retarget the
        # substrings the script-detection check looks for.
        with self.assertRaises(site_render.IndexEmbeddedDerivationError):
            site_render.derive_index_embedded_html(html, (), "2026-07-10")

    def test_missing_my_link_raises(self):
        html = _SAMPLE_INDEX_HTML.replace('href="/my"', 'href="/my-account"')
        with self.assertRaises(site_render.IndexEmbeddedDerivationError):
            site_render.derive_index_embedded_html(html, (), "2026-07-10")

    def test_missing_book_link_raises(self):
        html = _SAMPLE_INDEX_HTML.replace('href="/book/sat-trier"', 'href="/reserve/sat-trier"')
        with self.assertRaises(site_render.IndexEmbeddedDerivationError):
            site_render.derive_index_embedded_html(html, (), "2026-07-10")

    def test_comment_mentioning_script_does_not_swallow_the_page_body(self):
        # Real bug, 2026-07-13: the actual production index.html has a
        # top-of-file HTML comment that mentions "<script>" in plain prose
        # (documenting what the real script further down does) -- the
        # naive version of this derivation saw that as a real opening tag
        # and matched (non-greedily) all the way to the FIRST real
        # </script> it could find, silently eating every booking link in
        # between. HTML comments must be stripped before any of that.
        html = (
            "<!-- see the <script> further down for what it does -->\n"
            + _SAMPLE_INDEX_HTML
        )
        out = site_render.derive_index_embedded_html(html, (), "2026-07-10")
        self.assertIn('href="/book/sat-trier" target="_blank" rel="noopener noreferrer"', out)
        self.assertIn('href="/my" target="_blank" rel="noopener noreferrer"', out)

    def test_write_derived_index_embedded_html_writes_rendered_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "index_embedded.html"
            course = _course(date_overrides=(
                CourseDateOverride(date="2026-07-18", start_time="09:45"),
            ))
            site_render.write_derived_index_embedded_html(
                _SAMPLE_INDEX_HTML, (course,), "2026-07-10", "", True, out_path,
            )
            content = out_path.read_text(encoding="utf-8")
            self.assertIn("ATTENTION", content)
            self.assertTrue(content.startswith(site_render.EMBEDDED_MANAGED_MARKER))


if __name__ == "__main__":
    unittest.main()
