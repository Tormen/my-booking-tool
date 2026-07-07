import tempfile
import unittest
from unittest.mock import patch

from app.cancel_flow import cancel_and_promote
from app.security import hash_token, new_token
from app.storage import STATUS_CONFIRMED, STATUS_WAITLISTED, Store

from .helpers import make_course, make_settings


class CancelAndPromoteCourseRemovedTest(unittest.TestCase):
    """app.cancel_flow.cancel_and_promote's own "if course is None" guard
    (added 2026-07-06 during the App._cancel_and_promote -> cancel_flow
    unification): every caller used to invoke the old App._cancel_and_promote
    unconditionally, so a course_shortname no longer present in
    settings.toml would crash on course.capacity. tests/test_cli_cancel.py's
    test_course_removed_from_settings_still_cancels_but_does_not_email
    covers this at the cancel_registration() wrapper level (via the real
    FakeTransport); this test hits cancel_and_promote() directly with a
    course_shortname settings.course() can't resolve, so it isolates the
    guard itself from cancel_registration()'s own status-transition logic
    and confirms the function is a full no-op (no promotion, no emails, no
    sync_fn call) rather than merely "didn't raise"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        # Deliberately NOT registered in settings -- mirrors a course that
        # existed when the registration was made but has since been removed
        # from settings.toml.
        self.settings = make_settings(courses=())

        self.sent_emails: list[tuple[str, str, str]] = []
        patcher = patch(
            "app.cancel_flow.send_mail",
            side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None: self.sent_emails.append((to, subject, body)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sync_calls: list[tuple[str, str]] = []

    def _book(self, email: str, name: str, status: str, occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration(
            "yoga-class-1", occurrence_date, user.user_id, hash_token(new_token()), status=status,
        )
        return user, reg

    def test_course_removed_is_a_full_no_op_besides_the_warning_log(self):
        # A waitlisted guest is present -- if the guard were missing (or
        # bypassed), a real capacity would let promote_next_waitlisted find
        # and promote them. With the course gone from settings, nothing
        # should happen at all: no promotion, no emails, no calendar sync.
        self._book("confirmed@example.org", "Confirmed", status=STATUS_CONFIRMED)
        _waiter_user, waiter_reg = self._book("waiter@example.org", "Waiter", status=STATUS_WAITLISTED)

        # caldav=None: if the guard didn't return early, the very next line
        # after it (store.promote_next_waitlisted -> course.capacity) would
        # already raise AttributeError on course being None, well before
        # caldav is ever touched -- passing None here makes any accidental
        # fall-through past the guard fail loudly instead of silently
        # succeeding against a real/fake client.
        cancel_and_promote(
            self.store, self.settings, None, "yoga-class-1", "2026-08-01",
            sync_fn=lambda course_shortname, occurrence_date_str: self.sync_calls.append(
                (course_shortname, occurrence_date_str)
            ),
        )

        # No promotion: the waitlisted registration is untouched.
        reloaded = self.store.find_by_id(waiter_reg.registration_id)
        self.assertEqual(reloaded.status, STATUS_WAITLISTED)

        # No emails of any kind (neither the promoted-guest nor admin copy).
        self.assertEqual(self.sent_emails, [])

        # sync_fn (stand-in for calendar_sync) never invoked either.
        self.assertEqual(self.sync_calls, [])


if __name__ == "__main__":
    unittest.main()
