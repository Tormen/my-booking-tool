import tempfile
import unittest
from unittest.mock import patch

from app.cli_cancel import cancel_registration
from app.security import hash_token, new_token
from app.storage import STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED, Store

from .helpers import make_course, make_settings


class CancelRegistrationTest(unittest.TestCase):
    """`my-bt cancel`'s underlying logic (scripts/my-bt has no .py
    extension, so it's tested here -- see app/cli_cancel.py's own
    docstring). Must behave IDENTICALLY, email-wise, to
    app/webapp.py::App.admin_cancel -- both call the exact same
    app.cancellation.send_cancellation_emails function, so these tests
    mirror test_webapp.py's AdminOverviewTest.test_admin_cancel_notifies_both_sides_with_message."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        course = make_course(shortname="yoga-class-1", title="Yoga")
        self.settings = make_settings(courses=(course,))

        self.sent_emails: list[tuple[str, str, str]] = []
        patcher = patch(
            "app.cancellation.send_mail",
            side_effect=lambda settings, to, subject, body: self.sent_emails.append((to, subject, body)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _book(self, email: str, name: str, status: str = STATUS_CONFIRMED, occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration(
            "yoga-class-1", occurrence_date, user.user_id, hash_token(new_token()), status=status,
        )
        return user, reg

    # -- happy path -----------------------------------------------------

    def test_cancels_confirmed_registration_and_notifies_both_sides(self):
        user, reg = self._book("guest@example.org", "Guest")
        result = cancel_registration(self.store, self.settings, reg.registration_id, message="course canceled")

        self.assertTrue(result.ok)
        self.assertEqual(result.status_before, STATUS_CONFIRMED)
        self.assertEqual(result.course_shortname, "yoga-class-1")
        self.assertEqual(result.occurrence_date, "2026-08-01")
        self.assertEqual(result.user_email, "guest@example.org")
        self.assertTrue(result.emailed)

        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        self.assertEqual(reloaded.canceled_by, "host")
        self.assertEqual(reloaded.host_message, "course canceled")

        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("guest@example.org", to_addrs)
        self.assertIn("admin@example.org", to_addrs)

        participant_mail = next(b for t, s, b in self.sent_emails if t == "guest@example.org")
        self.assertIn("The host canceled this booking:", participant_mail)
        self.assertIn("Message: course canceled", participant_mail)
        self.assertIn("What: Yoga", participant_mail)

        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org")
        self.assertIn("You canceled this booking:", admin_mail)
        self.assertIn("Message: course canceled", admin_mail)

    def test_cancels_waitlisted_registration(self):
        user, reg = self._book("guest@example.org", "Guest", status=STATUS_WAITLISTED)
        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_before, STATUS_WAITLISTED)
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

    def test_without_message_omits_message_line_and_reports_empty(self):
        user, reg = self._book("guest@example.org", "Guest")
        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertEqual(result.message, "")
        participant_mail = next(b for t, s, b in self.sent_emails if t == "guest@example.org")
        self.assertNotIn("Message:", participant_mail)

    # -- error paths ------------------------------------------------------

    def test_nonexistent_registration_id_reports_clearly_no_exception(self):
        result = cancel_registration(self.store, self.settings, "no-such-id")
        self.assertFalse(result.ok)
        self.assertIn("no registration", result.reason)
        self.assertEqual(self.sent_emails, [])

    def test_already_canceled_registration_is_not_recanceled(self):
        user, reg = self._book("guest@example.org", "Guest")
        cancel_registration(self.store, self.settings, reg.registration_id)
        self.sent_emails.clear()

        result = cancel_registration(self.store, self.settings, reg.registration_id)
        self.assertFalse(result.ok)
        self.assertIn("not cancelable", result.reason)
        self.assertIn("canceled_by_host", result.reason)
        self.assertEqual(self.sent_emails, [])
        # Still canceled (unchanged), not double-processed.
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)

    def test_course_removed_from_settings_still_cancels_but_does_not_email(self):
        # Mirrors admin_cancel()'s own "if course:" guard -- a registration
        # for a course shortname no longer in settings.toml still gets
        # canceled (the status transition doesn't depend on course config),
        # it just can't compose an email without a title/location/etc.
        user, reg = self._book("guest@example.org", "Guest")
        settings_without_course = make_settings(courses=())
        result = cancel_registration(self.store, settings_without_course, reg.registration_id)
        self.assertTrue(result.ok)
        self.assertFalse(result.emailed)
        self.assertEqual(self.sent_emails, [])
        reloaded = self.store.find_by_id(reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)


if __name__ == "__main__":
    unittest.main()
