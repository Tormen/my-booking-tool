"""Direct unit coverage for app/cancellation.py's shared booking-detail
generators -- previously only exercised indirectly through the full email
flows in test_webapp.py/test_book_guests.py/test_cli_cancel.py. Added
2026-07-09 alongside course_recap_html()/html_email_body() (the operator: "Please
use yoga emoji for the What" / "format description in email as on page ...
box the description and put the background color (as on the page)" /
"Can be always the same code that generates this for the page or email"),
since those are new, easy-to-drift-apart pieces worth pinning down on
their own rather than only ever asserting on giant end-to-end email
bodies."""
import unittest

from app.cancellation import (
    booking_details_text, course_recap_html, greeting_html, html_email_body, intro_html, message_html,
)

from .helpers import make_course


class BookingDetailsTextTest(unittest.TestCase):
    def test_what_uses_a_yoga_emoji_not_a_pushpin(self):
        course = make_course(description="")
        details = booking_details_text(course, "2026-07-11")
        self.assertIn("\U0001F9D8 What:", details)  # person in lotus position
        self.assertNotIn("\U0001F4CC", details)  # old generic pushpin, must be gone

    def test_order_is_what_when_where(self):
        course = make_course(description="")
        details = booking_details_text(course, "2026-07-11")
        self.assertLess(details.index("What:"), details.index("When:"))
        self.assertLess(details.index("When:"), details.index("Where:"))

    def test_message_sits_above_the_description_not_after_it(self):
        # 2026-07-11, the operator (screenshot of a Reinstated email with "Message:
        # you are on again" printed AFTER the whole course description):
        # "please place the msg block ABOVE the description and if there is
        # no message, leave it out."
        course = make_course(description="Bring your own mat.")
        details = booking_details_text(course, "2026-07-11", message="you are on again")
        self.assertLess(details.index("Where:"), details.index("Message:"))
        self.assertLess(details.index("Message:"), details.index("Bring your own mat."))

    def test_no_message_omits_the_message_line_entirely(self):
        course = make_course(description="Bring your own mat.")
        details = booking_details_text(course, "2026-07-11")
        self.assertNotIn("Message:", details)


class CourseRecapHtmlTest(unittest.TestCase):
    def test_same_yoga_emoji_and_order_as_the_text_version(self):
        course = make_course(description="")
        html = course_recap_html(course, "2026-07-11")
        self.assertIn("\U0001F9D8 What:</b>", html)
        self.assertLess(html.index("What:"), html.index("When:"))
        self.assertLess(html.index("When:"), html.index("Where:"))

    def test_labels_are_bold(self):
        course = make_course(description="")
        html = course_recap_html(course, "2026-07-11")
        self.assertIn("<b>\U0001F9D8 What:</b>", html)
        self.assertIn("<b>\U0001F550 When:</b>", html)
        self.assertIn("<b>\U0001F4CD Where:</b>", html)

    def test_description_is_boxed_with_a_background_color(self):
        # the operator: "box the description and put the background color (as on
        # the page)" -- inline-styled (not class-based) so this exact
        # markup also renders correctly embedded in an HTML email, where
        # a <style> block/class isn't reliable across mail clients.
        course = make_course(description="<p>Bring your own mat.</p>")
        html = course_recap_html(course, "2026-07-11")
        self.assertIn("background:#fdf8ef", html)
        self.assertIn("Bring your own mat.", html)

    def test_no_description_omits_the_empty_box(self):
        course = make_course(description="")
        html = course_recap_html(course, "2026-07-11")
        self.assertNotIn("background:#fdf8ef", html)

    def test_escapes_title_and_location(self):
        course = make_course(title="A & B Yoga", location="<Studio>", description="")
        html = course_recap_html(course, "2026-07-11")
        self.assertIn("A &amp; B Yoga", html)
        self.assertIn("&lt;Studio&gt;", html)
        self.assertNotIn("<Studio>", html)

    def test_message_sits_above_the_description_not_after_it(self):
        # See BookingDetailsTextTest's twin test above for the full the operator
        # quote -- same fix, HTML side.
        course = make_course(description="<p>Bring your own mat.</p>")
        html = course_recap_html(course, "2026-07-11", message="you are on again")
        self.assertLess(html.index("Where:"), html.index("Message:"))
        self.assertLess(html.index("Message:"), html.index("Bring your own mat."))
        self.assertIn("background:#f2f2f2", html)  # message_html()'s own box

    def test_no_message_omits_the_message_box_entirely(self):
        course = make_course(description="<p>Bring your own mat.</p>")
        html = course_recap_html(course, "2026-07-11")
        self.assertNotIn("Message:", html)
        self.assertNotIn("background:#f2f2f2", html)


class HtmlEmailBodyTest(unittest.TestCase):
    def test_wraps_inner_html_in_a_self_contained_document(self):
        wrapped = html_email_body("<p>hello</p>")
        self.assertIn("<html>", wrapped)
        self.assertIn("<p>hello</p>", wrapped)


class IntroHtmlTest(unittest.TestCase):
    """2026-07-10, the operator: "please make the first sentance in email a bit
    more visible (bold mayb and for sure larger font size...) ... same of
    course for ALL emails" -- shared by every html_body-carrying email
    (booking confirmed/waitlisted, cancellation participant+admin,
    promoted-from-waitlist guest+admin)."""

    def test_is_bold_and_larger_than_normal_text(self):
        rendered = intro_html("Your spot is confirmed:")
        self.assertIn("font-weight:bold", rendered)
        self.assertIn("font-size:1.25em", rendered)
        self.assertIn("Your spot is confirmed:", rendered)

    def test_escapes_a_guest_supplied_name(self):
        # admin-cancellation/promotion emails interpolate a guest's own
        # name into the intro sentence (e.g. "Jane <jane@x.com> canceled
        # this booking:") -- this must not be a raw HTML-injection hole.
        rendered = intro_html('<script>alert(1)</script> canceled this booking:')
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


class GreetingHtmlTest(unittest.TestCase):
    """2026-07-08, the operator: "they should now all start with 'Dear <NAME>',
    correct?" -- closes the gap between _send_confirm_email() (already had
    this) and the guest-facing booking-result/cancellation/reinstatement
    emails (didn't). Deliberately plain/non-bold, unlike intro_html()'s
    bold status sentence right after it -- a greeting isn't the "most
    important fact" intro_html() exists to spotlight."""

    def test_is_plain_not_bold(self):
        rendered = greeting_html("Regular")
        self.assertNotIn("font-weight:bold", rendered)
        self.assertIn("Dear Regular,", rendered)

    def test_escapes_a_guest_supplied_name(self):
        rendered = greeting_html("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


class MessageHtmlTest(unittest.TestCase):
    """2026-07-10, the operator: "the message from the comment field should
    always be displayed like this: light grey background with the
    message" -- shared by both send_cancellation_emails() and
    send_reinstatement_emails()'s optional comment/reason."""

    def test_has_a_light_grey_background_box(self):
        rendered = message_html("running late, sorry")
        self.assertIn("background:#f2f2f2", rendered)
        self.assertIn("running late, sorry", rendered)

    def test_escapes_a_guest_supplied_message(self):
        rendered = message_html("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_default_label_is_plain_message(self):
        rendered = message_html("running late, sorry")
        self.assertIn("<b>Message:</b>", rendered)

    def test_custom_label_is_used_instead(self):
        # 2026-07-09, the operator (b): send_cancellation_emails's participant copy
        # passes a direction-aware label instead of the plain default.
        rendered = message_html("running late, sorry", label="Message from the host:")
        self.assertIn("<b>Message from the host:</b>", rendered)
        self.assertNotIn("<b>Message:</b>", rendered)

    def test_custom_label_is_escaped(self):
        rendered = message_html("hi", label="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", rendered)


if __name__ == "__main__":
    unittest.main()
