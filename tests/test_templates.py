import unittest

from app.templates import _SUBMIT_FEEDBACK_SCRIPT, page


class SubmitFeedbackScriptTest(unittest.TestCase):
    """2026-07-11: a screenshot of a Cancel submission sitting at 2.05s
    in devtools, buttons still clickable, no feedback at all, showed every page()
    render now carries a small global script that disables every button and
    labels the clicked one "Please wait..." the instant ANY form on the page
    is submitted -- see _SUBMIT_FEEDBACK_SCRIPT's own docstring in
    app/templates.py for the full rationale. Deliberately page()-level (not
    opt-in per page like app/webapp.py's _DIALOG_WIRING_SCRIPT), so it's a
    my-booking-tool-wide default covering every current and future form,
    Cancel/Reinstate/booking/settings/delete-account alike -- exactly what
    was asked for -- a my-booking wide default for all forms."""

    def test_every_page_carries_the_script_exactly_once(self):
        html = page("Some title", "<p>body</p>")
        self.assertEqual(html.count(_SUBMIT_FEEDBACK_SCRIPT), 1)

    def test_script_is_a_plain_constant_with_no_interpolation(self):
        # The whole point: ONE sha256 CSP hash must cover this script on
        # every page it appears on, forever -- so two independent page()
        # calls (different title/body/banner) must produce byte-identical
        # script text, never a per-page-customized copy.
        a = page("Title A", "<p>A</p>")
        b = page("Title B", "<p>B</p>", banner="<p>a banner</p>")

        def extract(html: str) -> str:
            start = html.index("<script>\n(function() {\n  document.addEventListener(\"submit\"")
            end = html.index("</script>", start) + len("</script>")
            return html[start:end]

        self.assertEqual(extract(a), extract(b))

    def test_disables_every_button_and_labels_the_submitter_on_submit(self):
        self.assertIn('document.querySelectorAll("button").forEach', _SUBMIT_FEEDBACK_SCRIPT)
        self.assertIn("b.disabled = true", _SUBMIT_FEEDBACK_SCRIPT)
        self.assertIn('ev.submitter.textContent = "Please wait...";', _SUBMIT_FEEDBACK_SCRIPT)

    def test_respects_a_legacy_onsubmit_confirm_cancel(self):
        # A pre-<dialog> browser's onsubmit="confirm(...)" can still call
        # preventDefault() synchronously during the same dispatch (guest
        # answers "No") -- the deferred check must not disable every button
        # on the page for a submission that never actually happened.
        self.assertIn("if (ev.defaultPrevented) return;", _SUBMIT_FEEDBACK_SCRIPT)


class PageFaviconTest(unittest.TestCase):
    """2026-07-08: a screenshot of /admin/login with a browser-console
    404 for /favicon.ico, compared against site/index.html which
    explicitly declares favicon <link> tags under /favicon/): page() --
    every dynamically-rendered page (courses/book/my/admin/admin-login
    alike) -- had NO <link rel="icon"> of its own. Absent one, a browser
    falls back to its own implicit default probe of /favicon.ico at the
    site root, which 404s since the real files live under /favicon/ (see
    nginx-locations.conf's root /var/www/booking.example.org/public_html -- static,
    unmatched paths like /favicon/* fall through to that root and are
    served directly, so a root-relative link resolves correctly from any
    app page regardless of host)."""

    def test_page_declares_the_same_favicon_set_as_index_html(self):
        html = page("Some title", "<p>body</p>")
        self.assertIn('<link rel="icon" type="image/png" href="/favicon/favicon-96x96.png" sizes="96x96">', html)
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon/favicon.svg">', html)
        self.assertIn('<link rel="shortcut icon" href="/favicon/favicon.ico">', html)
        self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/favicon/apple-touch-icon.png">', html)
        self.assertIn('<link rel="manifest" href="/favicon/site.webmanifest">', html)

    def test_favicon_links_are_root_relative_not_domain_absolute(self):
        # Deliberately NOT hardcoding booking.example.org (unlike site/index.html's
        # own Word-pasted absolute URLs) -- app/templates.py has no
        # business knowing its own domain, and doesn't need to: same-origin
        # root-relative links work regardless of host.
        html = page("Some title", "<p>body</p>")
        self.assertNotIn("https://", html.split("<style>")[0])


class IdInputWidthTest(unittest.TestCase):
    """2026-07-08: a screenshot of /admin/login's password field
    stretched across the full-width 1000px body, section 14's own width
    change, showed the Name, Email, Password fields should not be that
    wide -- but wide enough for really long passwords (maybe 50 chars) and
    emails like firstname.doublebarrelled-name@long-company.example (51
    chars); 50 chars was confirmed as the actual target. `.id-input`
    caps `.big-input`'s own width:100% at a character-count width (ch
    scales with .big-input's own font-size) rather than a fixed pixel
    value, applied alongside (not instead of) `.big-input` on every
    single-line Name/Email/Password field app-wide -- see app/webapp.py's
    Name/Email/Password `<input>` call sites."""

    def test_id_input_caps_width_at_50_characters(self):
        html = page("Some title", "<p>body</p>")
        self.assertIn(".id-input{max-width:50ch}", html)

    def test_id_input_does_not_override_big_input_but_narrows_it(self):
        # .id-input must be a NARROWER cap layered on top of .big-input's
        # own width:100%, not a replacement for its font-size/padding.
        html = page("Some title", "<p>body</p>")
        self.assertIn(
            '.big-input{font-size:1.25em;width:100%;box-sizing:border-box;padding:.1em .5em;display:block}',
            html,
        )


class BigInputDisplayBlockTest(unittest.TestCase):
    """2026-07-09: a screenshot of /my's login form showed the Email box
    visibly wider/further left than the Password box below it -- the 2
    boxes should be aligned, and this was a regression from previously
    nice alignment. Root cause: `label{display:block;margin-top:.6em}` (see
    templates.py's own <style> block) puts each "Email <input>"/"Password
    <input>" label on its own block, but the INPUT itself defaults to
    inline-block, sitting on the SAME line as its label's text -- so
    "Email" (5 chars) vs "Password" (8 chars) push their respective inputs
    to different starting x-positions. Before `.id-input{max-width:50ch}`
    existed (2026-07-08), `.big-input`'s uncapped `width:100%` was too wide
    to fit next to ANY label text and always wrapped onto its own line
    below it -- both fields' inputs ended up flush-left and equal-width by
    accident. Capping the width let the (now narrower) input fit on the
    same line as its label, un-masking this latent misalignment. Fix:
    `display:block` forces every `.big-input` onto its own line
    unconditionally, regardless of label text length or width, restoring
    the pre-regression alignment for good rather than by accident."""

    def test_big_input_is_forced_onto_its_own_line(self):
        html = page("Some title", "<p>body</p>")
        self.assertIn("display:block", html.split(".big-input{", 1)[1].split("}", 1)[0])


class DateBoxSizingTest(unittest.TestCase):
    """2026-07-14, from two live screenshots: date boxes with different
    line counts (1-line Booked badge, 2-line date+spots, 3-line
    date+spots+override-time) rendered at different heights in the same
    row, and the short badge's own overflow:hidden clipped the diagonal
    ribbon mid-word. EVERY box now stretches to its grid row's height
    (shared .date-btn > span rule) with content vertically centered; the
    badge additionally keeps a two-line min-height so an all-badge row
    still gives the ribbon room."""

    def test_every_date_box_stretches_to_row_height(self):
        html = page("Some title", "<p>body</p>")
        rule = html.split(".date-btn > span{", 1)[1].split("}", 1)[0]
        self.assertIn("height:100%", rule)
        self.assertIn("box-sizing:border-box", rule)
        self.assertIn("justify-content:center", rule)

    def test_badge_keeps_a_two_line_minimum_for_its_ribbon(self):
        html = page("Some title", "<p>body</p>")
        rule = html.split(".date-badge>span{", 1)[1].split("}", 1)[0]
        self.assertIn("min-height:", rule)


class GuestsSectionSeparatorTest(unittest.TestCase):
    """2026-07-14: the '+ Add participant' link must sit ABOVE the
    horizontal separator line, with the acknowledge checkbox and Book
    button below it (explicit request, from a live screenshot of the real
    booking page) -- implemented by flipping .guests-section's separator
    border from top to bottom. Locked in here so a future CSS cleanup
    can't silently flip it back."""

    def test_separator_is_a_bottom_border_below_the_guests_block(self):
        html = page("Some title", "<p>body</p>")
        rule = html.split(".guests-section{", 1)[1].split("}", 1)[0]
        self.assertIn("border-bottom:1px solid", rule)
        self.assertNotIn("border-top", rule)


if __name__ == "__main__":
    unittest.main()
