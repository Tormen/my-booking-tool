"""app/emailer.py's own send_mail() -- previously untested directly (every
other test in this suite patches send_mail itself at whichever module calls
it, e.g. app.webapp.send_mail, since nothing else needed to verify its
actual SMTP/MIME mechanics). Added 2026-07-09 alongside the optional
html_body param (the operator: "format description in email as on page ... box the
description and put the background color (as on the page)") -- genuinely
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


if __name__ == "__main__":
    unittest.main()
