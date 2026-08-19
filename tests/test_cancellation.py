"""Direct unit coverage for app/cancellation.py's shared booking-detail
generators -- previously only exercised indirectly through the full email
flows in test_webapp.py/test_book_guests.py/test_cli_cancel.py. Added
2026-07-09 alongside course_recap_html()/html_email_body() (a yoga emoji
was requested for the What line; the description formatting was requested
to match the page, including the boxed background color; and the same code
should generate this for both the page and email),
since those are new, easy-to-drift-apart pieces worth pinning down on
their own rather than only ever asserting on giant end-to-end email
bodies."""
import unittest

from app.cancellation import (
    attention_html, booking_details_text, course_recap_html, greeting_html, host_details_text, host_subject,
    html_email_body, intro_html, message_html,
)
from app.config import CourseDateOverride

from .helpers import make_course


class HostDetailsTextTest(unittest.TestCase):
    """2026-08-19: emails that only ever land in the operator's own inbox
    are plain ASCII and drop the course description -- from comparing two
    real host emails that had drifted apart (one emoji-rich HTML, one
    plain text; one with an occupancy count, one without). Same builder
    as the participant block, so the What/When/Where layout can't drift."""

    def test_no_emoji_anywhere(self):
        course = make_course(description="<p>Some description</p>")
        details = host_details_text(course, "2026-08-19")
        self.assertTrue(details.isascii(), details)

    def test_keeps_what_when_where_in_the_same_order_as_the_guest_block(self):
        course = make_course(description="")
        details = host_details_text(course, "2026-08-19")
        self.assertLess(details.index("What:"), details.index("When:"))
        self.assertLess(details.index("When:"), details.index("Where:"))
        self.assertIn("What: Dynamic Ashtanga Vinyasa Yoga", details)

    def test_course_description_is_not_repeated_back_to_the_host(self):
        course = make_course(description="<p>ONLY FOR DBG COWORKERS</p>")
        self.assertNotIn("ONLY FOR DBG COWORKERS", host_details_text(course, "2026-08-19"))
        # ...but the guest copy still gets it -- this is a host-only trim.
        self.assertIn("ONLY FOR DBG COWORKERS", booking_details_text(course, "2026-08-19"))

    def test_date_override_attention_line_survives(self):
        # An exceptional time change is exactly what the host must still
        # see -- only the emoji and the description are dropped.
        course = make_course(description="", date_overrides=(
            CourseDateOverride(date="2026-08-19", start_time="09:45", message="Starts an hour earlier"),
        ))
        details = host_details_text(course, "2026-08-19")
        self.assertIn("ATTENTION: Starts an hour earlier", details)
        self.assertTrue(details.isascii(), details)

    def test_never_carries_the_optional_message(self):
        # Every host template renders the comment via its own
        # {{message_line}} macro -- if it also rode inside details, it
        # would print twice.
        course = make_course(description="")
        self.assertNotIn("welcome back", host_details_text(course, "2026-08-19"))


class HostSubjectTest(unittest.TestCase):
    def test_shortname_date_and_occupancy(self):
        course = make_course(shortname="lux-wed-yoga", capacity=14)
        self.assertEqual(
            host_subject("New booking", course, "2026-09-09", 1),
            "New booking: lux-wed-yoga on 2026-09-09 [1/14]",
        )

    def test_uses_the_shortname_not_the_long_title(self):
        course = make_course(shortname="lux-wed-yoga", title="DBG-only WED@Lux - Dynamic Ashtanga Vinyasa Yoga")
        self.assertNotIn("Dynamic Ashtanga", host_subject("Canceled", course, "2026-09-09", 0))

    def test_full_course_reads_as_taken_of_capacity(self):
        course = make_course(shortname="trier-sat-yoga", capacity=12)
        self.assertEqual(
            host_subject("New waitlist entry", course, "2026-08-22", 12),
            "New waitlist entry: trier-sat-yoga on 2026-08-22 [12/12]",
        )


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
        # 2026-07-11: a Reinstated email was found with "Message:
        # you are on again" printed AFTER the whole course description --
        # the msg block now goes ABOVE the description, and is left out
        # entirely if there is no message.
        course = make_course(description="Bring your own mat.")
        details = booking_details_text(course, "2026-07-11", message="you are on again")
        self.assertLess(details.index("Where:"), details.index("Message:"))
        self.assertLess(details.index("Message:"), details.index("Bring your own mat."))

    def test_no_message_omits_the_message_line_entirely(self):
        course = make_course(description="Bring your own mat.")
        details = booking_details_text(course, "2026-07-11")
        self.assertNotIn("Message:", details)

    def test_date_override_adds_an_attention_line_above_the_description(self):
        # 2026-07-16: emails concerning this slot with time
        # exceptions should also contain the ATTENTION with optional msg
        # block up in the email -- looked up automatically from
        # Course.date_overrides, no per-call-site plumbing needed.
        course = make_course(
            description="Bring your own mat.",
            date_overrides=(CourseDateOverride(
                date="2026-07-18", start_time="09:45",
                message="I need to be in Kaiserslautern before 13h.",
            ),),
        )
        details = booking_details_text(course, "2026-07-18")
        self.assertIn("ATTENTION: I need to be in Kaiserslautern before 13h.", details)
        self.assertLess(details.index("Where:"), details.index("ATTENTION:"))
        self.assertLess(details.index("ATTENTION:"), details.index("Bring your own mat."))
        # The When: line itself must reflect the shifted time too (default
        # make_course duration is 100min, so 09:45 -> 11:25).
        self.assertIn("9h45 - 11h25", details)

    def test_date_override_with_no_message_omits_the_attention_line(self):
        course = make_course(
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        details = booking_details_text(course, "2026-07-18")
        self.assertNotIn("ATTENTION:", details)

    def test_unrelated_date_gets_no_attention_line(self):
        course = make_course(
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45", message="early"),),
        )
        details = booking_details_text(course, "2026-07-25")
        self.assertNotIn("ATTENTION:", details)

    def test_attention_and_human_message_can_coexist(self):
        # ATTENTION (operator/settings.toml) and Message (guest/host typed,
        # e.g. Reinstate's comment) are separate lines, both possible at once.
        course = make_course(
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45", message="early"),),
        )
        details = booking_details_text(course, "2026-07-18", message="see you there")
        self.assertIn("ATTENTION: early", details)
        self.assertIn("Message: see you there", details)
        self.assertLess(details.index("ATTENTION:"), details.index("Message:"))


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
        # The description is boxed with the background color (as on
        # the page) -- inline-styled (not class-based) so this exact
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
        # See BookingDetailsTextTest's twin test above for the full
        # rationale -- same fix, HTML side.
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

    def test_date_override_adds_a_red_attention_box_above_the_description(self):
        course = make_course(
            description="<p>Bring your own mat.</p>",
            date_overrides=(CourseDateOverride(
                date="2026-07-18", start_time="09:45",
                message="I need to be in Kaiserslautern before 13h.",
            ),),
        )
        html = course_recap_html(course, "2026-07-18")
        self.assertIn("ATTENTION:", html)
        self.assertIn("I need to be in Kaiserslautern before 13h.", html)
        self.assertIn("background:#fdecea", html)  # attention_html()'s red box
        self.assertLess(html.index("Where:"), html.index("ATTENTION:"))
        self.assertLess(html.index("ATTENTION:"), html.index("Bring your own mat."))
        self.assertIn("9h45 - 11h25", html)  # When: line reflects the shift too

    def test_date_override_with_no_message_omits_the_attention_box(self):
        course = make_course(
            date_overrides=(CourseDateOverride(date="2026-07-18", start_time="09:45"),),
        )
        html = course_recap_html(course, "2026-07-18")
        self.assertNotIn("ATTENTION:", html)
        self.assertNotIn("background:#fdecea", html)


class AttentionHtmlTest(unittest.TestCase):
    """2026-07-16: displayed as an 'ATTENTION'-message in red."""

    def test_has_a_red_box_and_the_attention_label(self):
        rendered = attention_html("starts earlier")
        self.assertIn("background:#fdecea", rendered)
        self.assertIn("ATTENTION:", rendered)
        self.assertIn("starts earlier", rendered)

    def test_blank_input_renders_nothing(self):
        self.assertEqual(attention_html(""), "")

    def test_input_is_not_escaped_operator_authored_trust_boundary(self):
        # UNLIKE message_html() (guest/host free text), attention_html()'s
        # input comes from settings.toml -- same trust boundary as
        # Course.description, rendered raw elsewhere in this app too.
        rendered = attention_html("<b>bold</b> stays bold")
        self.assertIn("<b>bold</b> stays bold", rendered)


class HtmlEmailBodyTest(unittest.TestCase):
    def test_wraps_inner_html_in_a_self_contained_document(self):
        wrapped = html_email_body("<p>hello</p>")
        self.assertIn("<html>", wrapped)
        self.assertIn("<p>hello</p>", wrapped)


class IntroHtmlTest(unittest.TestCase):
    """2026-07-10: the first sentence in an email is made more
    visible (bold and a larger font size), consistently across ALL
    emails -- shared by every html_body-carrying email
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
    """2026-07-08: emails should all start with 'Dear <NAME>' --
    closes the gap between _send_confirm_email() (already had
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
    """2026-07-10: the message from the comment field should
    always be displayed like this: light grey background with the
    message -- shared by both send_cancellation_emails() and
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
        # 2026-07-09: send_cancellation_emails's participant copy
        # passes a direction-aware label instead of the plain default.
        rendered = message_html("running late, sorry", label="Message from the host:")
        self.assertIn("<b>Message from the host:</b>", rendered)
        self.assertNotIn("<b>Message:</b>", rendered)

    def test_custom_label_is_escaped(self):
        rendered = message_html("hi", label="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", rendered)


if __name__ == "__main__":
    unittest.main()
