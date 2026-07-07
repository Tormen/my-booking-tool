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


if __name__ == "__main__":
    unittest.main()
