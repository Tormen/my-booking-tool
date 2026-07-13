"""app/emailer.py's own send_mail() -- previously untested directly (every
other test in this suite patches send_mail itself at whichever module calls
it, e.g. app.webapp.send_mail, since nothing else needed to verify its
actual SMTP/MIME mechanics). Added 2026-07-09 alongside the optional
html_body param (the description formatting was requested to match the
page, including the boxed background color) -- genuinely
new branching logic (plain-text-only vs. multipart/alternative) worth its
own direct coverage, since every other test's mock swallows html_body
without ever exercising what send_mail actually does with it."""
import unittest
from unittest.mock import patch

from app.emailer import send_mail

from .helpers import make_settings


class SendMailTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()

    def test_no_html_body_sends_plain_text_only(self):
        # Backward compatible: every call site not yet updated to pass
        # html_body must keep sending the exact same single-part message
        # as before this param existed.
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(self.settings, "guest@example.org", "Subject", "plain body")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertFalse(sent_msg.is_multipart())
        self.assertEqual(sent_msg.get_content().strip(), "plain body")

    def test_html_body_sends_multipart_alternative_with_both_parts(self):
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(
                self.settings, "guest@example.org", "Subject", "plain body",
                html_body="<p>rich body</p>",
            )
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertTrue(sent_msg.is_multipart())
        parts = list(sent_msg.walk())
        text_part = next(p for p in parts if p.get_content_type() == "text/plain")
        html_part = next(p for p in parts if p.get_content_type() == "text/html")
        self.assertEqual(text_part.get_content().strip(), "plain body")
        self.assertIn("rich body", html_part.get_content())

    def test_ics_attachment_is_attached_with_the_right_content_type(self):
        # 2026-07-09: a calendar invite is also attached to the email
        # that is sent to the participant -- see
        # app/calendar_sync.py::guest_invite_ics/guest_cancel_ics for the
        # only two builders of the (filename, ics_text, method) tuple.
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(
                self.settings, "guest@example.org", "Subject", "plain body",
                html_body="<p>rich body</p>",
                ics_attachment=("invite.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "PUBLISH"),
            )
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertTrue(sent_msg.is_multipart())
        ics_part = next(p for p in sent_msg.walk() if p.get_content_type() == "text/calendar")
        self.assertEqual(ics_part.get_filename(), "invite.ics")
        self.assertEqual(ics_part.get_param("method"), "PUBLISH")
        self.assertIn("BEGIN:VCALENDAR", ics_part.get_content())
        # The plain/html alternative must still be intact alongside it.
        self.assertTrue(any(p.get_content_type() == "text/plain" for p in sent_msg.walk()))
        self.assertTrue(any(p.get_content_type() == "text/html" for p in sent_msg.walk()))

    def test_no_ics_attachment_by_default(self):
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(self.settings, "guest@example.org", "Subject", "plain body")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertFalse(any(p.get_content_type() == "text/calendar" for p in sent_msg.walk()))

    def test_no_bcc_by_default(self):
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(self.settings, "guest@example.org", "Subject", "plain body")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertIsNone(sent_msg["Bcc"])

    def test_bcc_addrs_sets_bcc_header(self):
        # 2026-07-09: a given email address is BCC'd on all
        # mails that go out to the attendees, for a time, to
        # ensure that all is OK -- see
        # app.config.Settings.bcc_attendee_email_list, which every
        # attendee-facing call site reads to build this argument.
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(
                self.settings, "guest@example.org", "Subject", "plain body",
                bcc_addrs=("watcher@example.org",),
            )
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_msg["Bcc"], "watcher@example.org")

    def test_multiple_bcc_addrs_are_comma_joined(self):
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(
                self.settings, "guest@example.org", "Subject", "plain body",
                bcc_addrs=("watcher1@example.org", "watcher2@example.org"),
            )
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_msg["Bcc"], "watcher1@example.org, watcher2@example.org")

    def test_no_reply_to_by_default(self):
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(self.settings, "guest@example.org", "Subject", "plain body")
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertIsNone(sent_msg["Reply-To"])

    def test_reply_to_sets_header(self):
        # 2026-07-16: a reply-to header was added so that if the host
        # replies to a registration of a participant mail, the reply will
        # go to the address of the participant -- every admin-facing
        # notification about one specific participant now passes their
        # email here (see app.webapp/app.cancellation/app.cancel_flow's
        # own call sites).
        with patch("app.emailer.smtplib.SMTP_SSL") as mock_smtp_ssl:
            smtp = mock_smtp_ssl.return_value.__enter__.return_value
            send_mail(
                self.settings, "admin@example.org", "Subject", "plain body",
                reply_to="participant@example.org",
            )
        sent_msg = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_msg["Reply-To"], "participant@example.org")

    # Not separately tested here: whether the "Bcc" header actually reaches
    # the wire. `smtplib.SMTP.send_message()` (the stdlib call send_mail()
    # hands the message to -- mocked out above, since these tests don't
    # want a real SMTP connection) is documented to read recipients off
    # To/Cc/Bcc but always strip Bcc from what it actually serializes/
    # transmits -- verified directly against its source, not this app's
    # own logic to (re-)test.


if __name__ == "__main__":
    unittest.main()
