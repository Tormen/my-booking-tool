"""Guest bookings ("+ Add participant" on the booking form, 2026-07):
end-to-end coverage of book()'s party path (app.webapp.App._book_with_guests,
Store.add_party_registrations_checking_capacity) -- see SOLUTION-DESIGN.md's
guest-booking entry for the standing rules this enforces:

  - The whole party (leader + guests) is admitted -- confirmed or
    waitlisted -- together, never split.
  - A brand-new guest's email skips the usual STATUS_PENDING_CONFIRMATION
    gate (the leader vouches for them) -- unlike a brand-new SOLO booker,
    who still goes through it (untouched, see BookingFlowTest in
    test_webapp.py).
  - Cancellation is always per-person: canceling one party member never
    affects the others.

Reuses FakeTransport/PROPFIND_BODY from test_webapp.py rather than
redefining them.
"""
import io
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import urlencode

from app import webapp
from app.caldav_client import CalDAVClient
from app.slots import build_occurrences
from app.storage import STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store
from app.webapp import App

from .helpers import make_course, make_settings
from .test_webapp import FakeTransport


class GuestBookingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(self._tmp.name)
        self.course = make_course(shortname="yoga-class-1", weekday="wed", capacity=2)
        self.settings = make_settings(courses=(self.course,), conflict_calendars=("Calendar", "Yoga-Bookings"))
        self.app = App(self.settings, self.store)
        self.transport = FakeTransport()
        self.app.caldav = CalDAVClient(
            self.settings.caldav_url, self.settings.caldav_username, self.settings.caldav_password,
            transport=self.transport,
        )
        self.app._sync = lambda *a, **kw: None  # calendar mechanics covered elsewhere

        self.sent_emails: list[tuple[str, str, str]] = []
        occs = build_occurrences(
            self.course, self.settings, datetime.now(timezone.utc),
            lambda sn, d: 0, lambda start, end: False,
        )
        self.occ_date = occs[0].date.isoformat()

        recorder = lambda settings, to, subject, body, html_body=None, ics_attachment=None: self.sent_emails.append((to, subject, body))
        for target in ("app.webapp.send_mail", "app.cancellation.send_mail", "app.cancel_flow.send_mail"):
            patcher = patch(target, side_effect=recorder)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, form: dict):
        body = urlencode(form).encode()
        environ = {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        return self.app.book("POST", "yoga-class-1", environ)

    def _book(self, email: str, name: str, guests: list[tuple[str, str]], occ_date: str | None = None):
        form = {
            "occurrence_date": occ_date or self.occ_date,
            "name": name, "email": email, "agree": "on",
        }
        for i, (g_email, g_name) in enumerate(guests):
            form[f"guest_email_{i}"] = g_email
            form[f"guest_name_{i}"] = g_name
        return self._post(form)


class PartyAdmissionTest(GuestBookingTestBase):
    def test_leader_and_guest_confirmed_together_when_room(self):
        status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("200", status)
        leader = self.store.find_user_by_email("leader@example.org")
        guest = self.store.find_user_by_email("guest@example.org")
        self.assertIsNotNone(leader)
        self.assertIsNotNone(guest)
        regs = self.store.all_registrations()
        self.assertEqual(len(regs), 2)
        self.assertTrue(all(r.status == STATUS_CONFIRMED for r in regs))
        # shared party_id, guest's invited_by_user_id points at the leader
        self.assertEqual(len({r.party_id for r in regs}), 1)
        guest_reg = next(r for r in regs if r.user_id == guest.user_id)
        leader_reg = next(r for r in regs if r.user_id == leader.user_id)
        self.assertEqual(guest_reg.invited_by_user_id, leader.user_id)
        self.assertEqual(leader_reg.invited_by_user_id, "")

    def test_every_confirmed_party_member_gets_their_own_publish_ics(self):
        # 2026-07-09, the operator: "attach a calendar invite also in the email
        # that is sent to the participant" -- _book_with_guests() sends
        # each party member their own copy of _send_booking_result_guest_email,
        # so each should get their own ics_attachment too, not just the leader.
        captured = []

        def spy(settings, to, subject, body, html_body=None, ics_attachment=None):
            if subject.startswith("Booking confirmed:"):
                captured.append((to, ics_attachment))
            self.sent_emails.append((to, subject, body))

        with patch("app.webapp.send_mail", side_effect=spy):
            self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        self.assertEqual(len(captured), 2)
        for to, ics_attachment in captured:
            self.assertIsNotNone(ics_attachment, f"no ics attached for {to}")
            self.assertEqual(ics_attachment[2], "PUBLISH")

    def test_whole_party_waitlisted_together_when_not_enough_room(self):
        course = make_course(shortname="yoga-class-1", weekday="wed", capacity=1)
        self.settings = make_settings(courses=(course,), conflict_calendars=("Calendar", "Yoga-Bookings"))
        self.app.settings = self.settings
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        regs = self.store.all_registrations()
        self.assertEqual(len(regs), 2)
        self.assertTrue(all(r.status == STATUS_WAITLISTED for r in regs))

    def test_brand_new_guest_skips_pending_confirmation(self):
        # Neither leader nor guest has ever booked before -- a SOLO brand
        # new booker would land as STATUS_PENDING_CONFIRMATION (see
        # BookingFlowTest.test_new_email_books_pending_and_holds_no_capacity
        # in test_webapp.py); a party must not, since admission has to be
        # decided for everyone right now.
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        regs = self.store.all_registrations()
        self.assertTrue(all(r.status != STATUS_PENDING_CONFIRMATION for r in regs))

    def test_solo_booking_unaffected_still_uses_pending_confirmation(self):
        # No guests submitted -- must behave exactly as before this feature.
        self._book("solo@example.org", "Solo", [])
        regs = self.store.all_registrations()
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].status, STATUS_PENDING_CONFIRMATION)
        self.assertEqual(regs[0].party_id, "")


class PartyValidationTest(GuestBookingTestBase):
    def test_duplicate_guest_email_matching_leader_is_rejected(self):
        status, _headers, body = self._book(
            "leader@example.org", "Leader", [("leader@example.org", "Duplicate")]
        )
        self.assertIn("listed more than once", body)
        self.assertEqual(self.store.all_registrations(), [])

    def test_two_guests_with_same_email_rejected(self):
        status, _headers, body = self._book(
            "leader@example.org", "Leader",
            [("guest@example.org", "One"), ("guest@example.org", "Two")],
        )
        self.assertIn("listed more than once", body)
        self.assertEqual(self.store.all_registrations(), [])

    def test_malformed_guest_email_rejected(self):
        status, _headers, body = self._book("leader@example.org", "Leader", [("not-an-email", "X")])
        self.assertIn("look valid", body)
        self.assertEqual(self.store.all_registrations(), [])

    def test_blank_guest_name_falls_back_to_placeholder_for_brand_new_guest(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "")])
        guest = self.store.find_user_by_email("guest@example.org")
        self.assertEqual(guest.name, "Guest")

    def test_blank_guest_name_preserves_existing_accounts_real_name(self):
        self.store.upsert_user_for_booking("guest@example.org", "Real Name")
        self._book("leader@example.org", "Leader", [("guest@example.org", "")])
        guest = self.store.find_user_by_email("guest@example.org")
        self.assertEqual(guest.name, "Real Name")


class PartyDoubleBookingTest(GuestBookingTestBase):
    """2026-07-10, the operator: "double booking possible?" then, on the leader-vs-
    guest distinction: "no we take their booking. if the main person
    already booked, then cannot book again." -- so the LEADER is rejected
    outright if already active for this course+date, but an already-active
    GUEST is silently dropped from the party (their existing booking is
    kept, not duplicated) rather than blocking the whole party."""

    def test_leader_already_booked_solo_is_rejected_entirely(self):
        from app.security import hash_secret
        leader = self.store.upsert_user_for_booking("leader@example.org", "Leader")
        h, s = hash_secret("hunter22")
        self.store.set_password(leader.user_id, h, s)
        self._book("leader@example.org", "Leader", [])  # solo, no guests -- fills their own spot
        self.sent_emails.clear()
        _status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("already booked", body)
        # Nothing new created for either the leader or the guest.
        self.assertEqual(len(self.store.registrations_for_user(leader.user_id)), 1)
        self.assertIsNone(self.store.find_user_by_email("guest@example.org"))
        self.assertEqual(self.sent_emails, [])

    def test_already_booked_guest_is_dropped_not_duplicated(self):
        # guest@example.org already has an active booking for this session
        # (say, booked solo earlier) -- leader now tries to add them again.
        guest = self.store.upsert_user_for_booking("guest@example.org", "Guest One")
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, guest.user_id, "irrelevant-hash", capacity=self.course.capacity
        )
        self.sent_emails.clear()
        status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("200", status)
        self.assertIn("already", body)  # addendum note shown to the leader
        # Only the leader's own row was added -- guest keeps their one, original row.
        guest_regs = self.store.registrations_for_user(guest.user_id)
        self.assertEqual(len(guest_regs), 1)
        leader = self.store.find_user_by_email("leader@example.org")
        self.assertEqual(len(self.store.registrations_for_user(leader.user_id)), 1)
        # No party_id shared -- the leader books alone since their only
        # requested guest got filtered out.
        self.assertEqual(self.store.registrations_for_user(leader.user_id)[0].party_id, "")

    def test_already_booked_guest_note_names_the_guest(self):
        guest = self.store.upsert_user_for_booking("guest@example.org", "Guest One")
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, guest.user_id, "irrelevant-hash", capacity=self.course.capacity
        )
        _status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("guest@example.org", body)

    def test_all_guests_already_booked_falls_back_to_solo_leader_booking(self):
        # Only guest requested, and they're already booked -- leader still
        # gets their own booking (not blocked just because their one guest
        # got filtered out).
        guest = self.store.upsert_user_for_booking("guest@example.org", "Guest One")
        self.store.add_registration_checking_capacity(
            "yoga-class-1", self.occ_date, guest.user_id, "irrelevant-hash", capacity=self.course.capacity
        )
        status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("200", status)
        leader = self.store.find_user_by_email("leader@example.org")
        self.assertIsNotNone(leader)
        leader_regs = self.store.registrations_for_user(leader.user_id)
        self.assertEqual(len(leader_regs), 1)

    def test_a_brand_new_guest_is_unaffected_by_the_check(self):
        # No pre-existing registration anywhere -- has_active_registration
        # must not misfire for a guest email the store has never seen.
        status, _headers, body = self._book(
            "leader@example.org", "Leader", [("guest@example.org", "Guest One")]
        )
        self.assertIn("200", status)
        self.assertNotIn("already", body)
        regs = self.store.all_registrations()
        self.assertEqual(len(regs), 2)


class PartyEmailTest(GuestBookingTestBase):
    def test_each_member_gets_own_email_plus_one_combined_admin_email(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        recipients = [to for to, _subj, _body in self.sent_emails]
        self.assertEqual(recipients.count("leader@example.org"), 1)
        self.assertEqual(recipients.count("guest@example.org"), 1)
        self.assertEqual(recipients.count(self.settings.admin_email), 1)  # combined, not one per person
        self.assertEqual(len(self.sent_emails), 3)

    def test_admin_email_mentions_both_leader_and_guest(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        admin_email = next(body for to, _s, body in self.sent_emails if to == self.settings.admin_email)
        self.assertIn("Leader", admin_email)
        self.assertIn("Guest One", admin_email)


class PartyAccountSetupLinkTest(GuestBookingTestBase):
    """2026-07-06: "The EMAIL sent out to guests should OPTIONALLY allow
    them to create an account for them to access their space and set a
    password!" -- every brand-new party member's booking email should
    embed a /my/confirm/<token> link inline (not a second, separate
    email), and an already-confirmed member's email should not."""

    def test_brand_new_leader_and_guest_both_get_an_account_setup_link(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        leader_email = next(b for to, _s, b in self.sent_emails if to == "leader@example.org")
        guest_email = next(b for to, _s, b in self.sent_emails if to == "guest@example.org")
        self.assertIn("/my/confirm/", leader_email)
        self.assertIn("/my/confirm/", guest_email)
        self.assertIn("Optional", leader_email)
        self.assertIn("Optional", guest_email)

    def test_link_actually_works_to_set_a_password_and_see_the_booking(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        guest_email = next(b for to, _s, b in self.sent_emails if to == "guest@example.org")
        token = guest_email.split("/my/confirm/")[1].split()[0].strip()
        user = self.store.find_user_by_email("guest@example.org")
        from app.security import hash_token
        resolved = self.store.find_user_by_confirm_token_hash(hash_token(token))
        self.assertEqual(resolved.user_id, user.user_id)

    def test_already_confirmed_member_gets_no_setup_link(self):
        # leader already has a password set (e.g. booked solo before) --
        # their guest-booking email should NOT dangle a redundant link.
        from app.security import hash_secret
        leader = self.store.upsert_user_for_booking("leader@example.org", "Leader")
        h, s = hash_secret("hunter2222")
        self.store.set_password(leader.user_id, h, s)
        self.sent_emails.clear()
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        leader_email = next(b for to, _s, b in self.sent_emails if to == "leader@example.org")
        guest_email = next(b for to, _s, b in self.sent_emails if to == "guest@example.org")
        self.assertNotIn("/my/confirm/", leader_email)
        self.assertIn("/my/confirm/", guest_email)  # guest is still brand new


class PartyCancellationTest(GuestBookingTestBase):
    def test_canceling_one_member_does_not_affect_the_other(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        regs = self.store.all_registrations()
        guest_reg = next(r for r in regs if r.user_id != next(
            r2.user_id for r2 in regs if r2.invited_by_user_id == ""
        ))
        self.store.cancel(guest_reg.registration_id, canceled_by="guest")

        remaining = self.store.all_registrations()
        leader_reg = next(r for r in remaining if r.invited_by_user_id == "")
        canceled_reg = next(r for r in remaining if r.registration_id == guest_reg.registration_id)
        self.assertEqual(leader_reg.status, STATUS_CONFIRMED)
        self.assertEqual(canceled_reg.status, "canceled_by_guest")


class PartyAdminOverviewTest(GuestBookingTestBase):
    def _admin_environ(self):
        admin_sid = webapp._new_session({"kind": "admin"})
        return {"HTTP_COOKIE": f"session={admin_sid}"}

    def test_leader_row_shows_guest_count(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertIn("+1 guest", body)

    def test_leader_row_shows_host_prefix(self):
        # 2026-07-08, the operator: "+1 guest" alone doesn't say WHOSE guest --
        # only readable as "the host of this party" by inference. Spell
        # out "Host" the same way "guest of <name>" already does for the
        # other side of the same party.
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertIn("Host (+1 guest)", body)

    def test_leader_row_pluralizes_multiple_guests(self):
        self._book("leader@example.org", "Leader", [
            ("guest1@example.org", "Guest One"), ("guest2@example.org", "Guest Two"),
        ])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertIn("Host (+2 guests)", body)

    def test_guest_row_shows_guest_of_leader(self):
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        # 2026-07-08, the operator: "If we write Host, then please also 'Guest'"
        # -- capitalized to match "Host (+N guest)" on the leader's row.
        self.assertIn("Guest of Leader", body)

    def test_guest_row_falls_back_to_email_when_leader_name_is_placeholder(self):
        # 2026-07-08, the operator (screenshot): "guest of Guest" reads as a bug,
        # even though it was technically correct -- the leader's own
        # `.name` field literally held the "Guest" placeholder (someone
        # left the name field as-is, or it was never resolved). Falling
        # back to the leader's email whenever their name equals the
        # literal placeholder avoids the confusing "of Guest" phrasing
        # without changing the placeholder itself.
        self._book("leader@example.org", "Guest", [("guest@example.org", "Guest One")])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertIn("Guest of leader@example.org", body)
        self.assertNotIn("Guest of Guest", body)

    def test_party_column_header_is_labeled_guests(self):
        # 2026-07-08, the operator: "Party" was unclear -- renamed to "Guests".
        self._book("leader@example.org", "Leader", [("guest@example.org", "Guest One")])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertIn('<th>Guests<span class="sort-indicator"></span></th>', body)
        self.assertNotIn(">Party<", body)

    def test_solo_booking_has_blank_party_cell(self):
        user = self.store.upsert_user_for_booking("solo@example.org", "Solo")
        from app.security import hash_secret
        h, s = hash_secret("hunter22")
        self.store.set_password(user.user_id, h, s)
        self._book("solo@example.org", "Solo", [])
        _status, _headers, body = self.app.admin_overview("GET", self._admin_environ())
        self.assertNotIn("guest of", body)
        self.assertNotIn("+1 guest", body)


if __name__ == "__main__":
    unittest.main()
