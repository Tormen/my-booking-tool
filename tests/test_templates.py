import unittest

from app.templates import _SUBMIT_FEEDBACK_SCRIPT, page


class SubmitFeedbackScriptTest(unittest.TestCase):
    """2026-07-11, the operator (screenshot of a Cancel submission sitting at 2.05s
    in devtools, buttons still clickable, no feedback at all): every page()
    render now carries a small global script that disables every button and
    labels the clicked one "Please wait..." the instant ANY form on the page
    is submitted -- see _SUBMIT_FEEDBACK_SCRIPT's own docstring in
    app/templates.py for the full rationale. Deliberately page()-level (not
    opt-in per page like app/webapp.py's _DIALOG_WIRING_SCRIPT), so it's a
    my-booking-tool-wide default covering every current and future form,
    Cancel/Reinstate/booking/settings/delete-account alike -- exactly what
    the operator asked for ("make it a my-booking wide default for all forms")."""

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
    """2026-07-08, the operator (screenshot of /admin/login with a browser-console
    404 for /favicon.ico, comparing it against site/index.html which
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
    """2026-07-08, the operator (screenshot of /admin/login's password field
    stretched across the full-width 1000px body, section 14's own width
    change): "the Name, Email, Password fields should not be that wide
    ... wide enough for really long passwords (maybe 50 chars) and emails
    like firstname.doublebarrelled-name@long-company.example" (54
    chars) -- confirmed "50 chars is OK" as the actual target. `.id-input`
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
        self.assertIn('.big-input{font-size:1.25em;width:100%;box-sizing:border-box;padding:.35em .5em}', html)


if __name__ == "__main__":
    unittest.main()
