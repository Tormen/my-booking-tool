import tempfile
import unittest
from unittest.mock import patch

from app.cancel_flow import cancel_and_promote, cancel_occurrence
from app.security import hash_token, new_token
from app.storage import (
    STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store,
)

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
            side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=(), reply_to=None: self.sent_emails.append((to, subject, body)),
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


class CancelOccurrenceTest(unittest.TestCase):
    """app.cancel_flow.cancel_occurrence() -- "cancel the entire session"
    (added 2026-07-13). Uses `sync_fn` throughout (like
    CancelAndPromoteCourseRemovedTest above) so these tests don't need a
    real/fake CalDAV client -- calendar-sync mechanics are already covered
    by test_calendar_sync.py; what's new here is WHICH rows get touched and
    HOW MANY TIMES the sync happens."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        course = make_course(shortname="yoga-class-1", title="Yoga", capacity=5)
        self.settings = make_settings(courses=(course,))

        self.sent_emails: list[tuple[str, str, str]] = []
        for target in ("app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            patcher = patch(
                target,
                side_effect=lambda settings, to, subject, body, html_body=None, ics_attachment=None, bcc_addrs=(), reply_to=None: self.sent_emails.append((to, subject, body)),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        self.sync_calls: list[tuple[str, str]] = []

    def _sync_fn(self):
        return lambda course_shortname, occurrence_date_str: self.sync_calls.append(
            (course_shortname, occurrence_date_str)
        )

    def _book(self, email: str, name: str, status: str, course_shortname: str = "yoga-class-1", occurrence_date: str = "2026-08-01"):
        user = self.store.upsert_user_for_booking(email, name)
        reg = self.store.add_registration(
            course_shortname, occurrence_date, user.user_id, hash_token(new_token()), status=status,
        )
        return user, reg

    def test_cancels_every_confirmed_waitlisted_and_pending_row_on_the_occurrence(self):
        self._book("confirmed@example.org", "Confirmed", status=STATUS_CONFIRMED)
        self._book("waiter@example.org", "Waiter", status=STATUS_WAITLISTED)
        self._book("pending@example.org", "Pending", status=STATUS_PENDING_CONFIRMATION)

        result = cancel_occurrence(
            self.store, self.settings, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn(),
        )

        self.assertEqual(len(result.canceled), 3)
        self.assertEqual({c.user_email for c in result.canceled},
                          {"confirmed@example.org", "waiter@example.org", "pending@example.org"})
        for reg in self.store.read_registrations(scope="live"):
            self.assertEqual(reg["status"], STATUS_CANCELED_BY_HOST)

    def test_creates_the_blocker_before_touching_any_registration(self):
        # 2026-07-14, verified live: canceling every registration alone
        # did NOT block the date -- it reappeared on the booking page as
        # bookable with full capacity. cancel_occurrence must create the
        # "CANCELED:" blocker event (via blocker_fn / the caldav default
        # path) FIRST, fail-closed: if it raises, no registration may
        # have been canceled yet.
        self._book("confirmed@example.org", "Confirmed", status=STATUS_CONFIRMED)
        blocker_calls = []
        cancel_occurrence(
            self.store, self.settings, None, "yoga-class-1", "2026-08-01",
            message="venue flooded", sync_fn=self._sync_fn(),
            blocker_fn=lambda sn, occ, msg: blocker_calls.append((sn, occ, msg)),
        )
        self.assertEqual(blocker_calls, [("yoga-class-1", "2026-08-01", "venue flooded")])

    def test_blocker_failure_aborts_before_any_registration_is_canceled(self):
        self._book("confirmed@example.org", "Confirmed", status=STATUS_CONFIRMED)

        def failing_blocker(sn, occ, msg):
            raise RuntimeError("CalDAV down")

        with self.assertRaises(RuntimeError):
            cancel_occurrence(
                self.store, self.settings, None, "yoga-class-1", "2026-08-01",
                sync_fn=self._sync_fn(), blocker_fn=failing_blocker,
            )
        # fail-closed: nothing was canceled, no email went out
        for reg in self.store.read_registrations(scope="live"):
            self.assertEqual(reg["status"], STATUS_CONFIRMED)
        self.assertEqual(self.sent_emails, [])

    def test_does_not_touch_a_different_occurrence_or_a_different_course(self):
        self._book("same-course-other-date@example.org", "Other", status=STATUS_CONFIRMED, occurrence_date="2026-08-08")
        other_course = make_course(shortname="other-course", title="Other", capacity=5)
        settings = make_settings(courses=(make_course(shortname="yoga-class-1", capacity=5), other_course))
        self._book("other-course@example.org", "OtherCourse", status=STATUS_CONFIRMED, course_shortname="other-course")
        _target_user, target_reg = self._book("target@example.org", "Target", status=STATUS_CONFIRMED)

        result = cancel_occurrence(
            self.store, settings, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn(),
        )

        self.assertEqual([c.registration_id for c in result.canceled], [target_reg.registration_id])
        statuses = {r["registration_id"]: r["status"] for r in self.store.read_registrations(scope="live")}
        self.assertEqual(statuses[target_reg.registration_id], STATUS_CANCELED_BY_HOST)
        # Everyone else untouched.
        self.assertEqual(sum(1 for s in statuses.values() if s == STATUS_CANCELED_BY_HOST), 1)

    def test_emails_every_participant_with_host_apology_and_next_occurrence_link(self):
        self._book("guest1@example.org", "Guest1", status=STATUS_CONFIRMED)
        self._book("guest2@example.org", "Guest2", status=STATUS_WAITLISTED)

        cancel_occurrence(
            self.store, self.settings, None, "yoga-class-1", "2026-08-01",
            message="venue flooded", sync_fn=self._sync_fn(),
        )

        to_addrs = [t for t, _, _ in self.sent_emails]
        self.assertIn("guest1@example.org", to_addrs)
        self.assertIn("guest2@example.org", to_addrs)
        self.assertEqual(to_addrs.count("admin@example.org"), 2)  # one admin copy per canceled participant

        guest1_mail = next(b for t, s, b in self.sent_emails if t == "guest1@example.org")
        # 2026-07-09: host-initiated cancels label the message
        # from the ATTENDEE's point of view -- it came from the host.
        self.assertIn("Message from the host: venue flooded", guest1_mail)
        self.assertIn("exception rather than the rule", guest1_mail)
        self.assertIn("Book the next occurrence of this course: https://", guest1_mail)
        self.assertIn("/book/yoga-class-1", guest1_mail)
        # 2026-07-09: no reinstate link at all for a host-
        # initiated cancel's participant copy.
        self.assertNotIn("/reinstate/", guest1_mail)

        # The host does not need the link -- admin copy is a receipt, not a
        # re-engagement email.
        admin_mail = next(b for t, s, b in self.sent_emails if t == "admin@example.org")
        self.assertNotIn("/book/yoga-class-1", admin_mail)

    def test_no_promotion_since_everyone_on_the_occurrence_is_canceled_together(self):
        # Even though a waitlisted guest is present, there's nobody left to
        # promote them INTO once everyone (confirmed + waitlisted) is
        # canceled together -- unlike cancel_and_promote(), this never
        # calls store.promote_next_waitlisted at all.
        self._book("confirmed@example.org", "Confirmed", status=STATUS_CONFIRMED)
        self._book("waiter@example.org", "Waiter", status=STATUS_WAITLISTED)

        cancel_occurrence(self.store, self.settings, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn())

        subjects = [s for _, s, _ in self.sent_emails]
        self.assertFalse(any(s.startswith("You're in!") for s in subjects))
        self.assertFalse(any(s.startswith("Promoted from waitlist:") for s in subjects))

    def test_syncs_the_calendar_exactly_once_not_once_per_row(self):
        self._book("guest1@example.org", "Guest1", status=STATUS_CONFIRMED)
        self._book("guest2@example.org", "Guest2", status=STATUS_CONFIRMED)
        self._book("guest3@example.org", "Guest3", status=STATUS_WAITLISTED)

        cancel_occurrence(self.store, self.settings, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn())

        self.assertEqual(self.sync_calls, [("yoga-class-1", "2026-08-01")])

    def test_nobody_live_on_the_occurrence_is_a_clean_no_op(self):
        result = cancel_occurrence(self.store, self.settings, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn())
        self.assertEqual(result.canceled, [])
        self.assertEqual(self.sent_emails, [])
        self.assertEqual(self.sync_calls, [])

    def test_course_removed_from_settings_still_cancels_but_does_not_email_or_sync(self):
        # Same "if course:" guard as cancel_and_promote()/cancel_registration()
        # -- the status transition doesn't depend on course config, but
        # composing an email or syncing a calendar for a course no longer in
        # settings.toml isn't possible.
        self._book("guest@example.org", "Guest", status=STATUS_CONFIRMED)
        settings_without_course = make_settings(courses=())

        result = cancel_occurrence(
            self.store, settings_without_course, None, "yoga-class-1", "2026-08-01", sync_fn=self._sync_fn(),
        )

        self.assertEqual(len(result.canceled), 1)
        reloaded = self.store.find_by_id(result.canceled[0].registration_id)
        self.assertEqual(reloaded.status, STATUS_CANCELED_BY_HOST)
        self.assertEqual(self.sent_emails, [])
        self.assertEqual(self.sync_calls, [])


if __name__ == "__main__":
    unittest.main()
