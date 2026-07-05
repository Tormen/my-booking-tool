"""wsgiref-based web app -- no framework dependency. Routes:

  GET/POST /book/<shortname>        guest booking form (name+email only)
  GET/POST /cancel/<token>          guest self-cancel (link from email)
  GET/POST /my                      guest login (email+password) / bookings list
  GET/POST /my/confirm/<token>      set password -- first-time account confirmation
                                     AND password reset both land here (same token
                                     mechanism, see storage.User.confirm_token_hash)
  GET/POST /my/reset                request a confirm/reset link by email (always
                                     the same response either way -- doesn't reveal
                                     whether an email is registered)
  POST     /my/cancel/<reg_id>      guest cancels one of their own bookings
  POST     /my/delete-account       guest erases their own account (Art. 17)
  GET/POST /admin/login             admin login
  GET      /admin                   admin overview (today+future by default)
  GET/POST /admin/cancel/<reg_id>   host cancels a registration, optional message

A booking under an email with no confirmed account yet doesn't hold a real
spot or sync to the calendar until the guest clicks the confirmation link --
see storage.STATUS_PENDING_CONFIRMATION and book()/my_confirm() below. This
closes an old hole where the booking form could silently overwrite ANY
existing account's login credential just by resubmitting that email (see
the maintainer's local notes for the incident this was designed against).

Sessions are server-side (in-memory dict: session_id -> {..., "expires":ts}),
referenced by a random cookie -- nothing sensitive is stored client-side.
This is fine for a single small process; if you ever run >1 worker, move
SESSIONS to something shared (e.g. sqlite) -- flagged here so it isn't a
silent surprise later.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from http import cookies
from urllib.parse import parse_qs

from . import calendar_sync
from .caldav_client import CalDAVClient, CalDAVError
from .config import Settings
from .emailer import send_mail
from .erasure import erase_user_by_email
from .security import (
    RateLimiter, hash_secret, hash_token, new_token, sanitize_csv_field,
    tokens_match, verify_admin_password, verify_secret,
)
from .slots import build_occurrences
from .storage import STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Store, now_iso
from .templates import esc, page

log = logging.getLogger("my_booking.webapp")

SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 60 * 60 * 4

login_limiter = RateLimiter(max_attempts=5, window_seconds=3600)


def _client_ip(environ: dict) -> str:
    """Best-effort real client IP, for per-source rate limiting -- NOT for
    anything security-critical beyond that (trivially spoofable if this app
    were ever reachable other than through its own reverse proxy). Trusts
    X-Forwarded-For because this app only ever listens on 127.0.0.1,
    reached exclusively via nginx (see nginx/my-booking.conf's
    proxy_set_header X-Forwarded-For), which sets/appends it on every
    request; falls back to REMOTE_ADDR for direct use without nginx in
    front (e.g. local dev). Takes the left-most entry -- the original
    client -- in case of a longer proxy chain in front of nginx.
    """
    forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "unknown")


def _new_session(data: dict) -> str:
    sid = new_token()
    SESSIONS[sid] = {**data, "expires": time.time() + SESSION_TTL_SECONDS}
    return sid


def _get_session(environ) -> dict | None:
    jar = cookies.SimpleCookie()
    jar.load(environ.get("HTTP_COOKIE", ""))
    if "session" not in jar:
        return None
    sid = jar["session"].value
    session = SESSIONS.get(sid)
    if session and session["expires"] > time.time():
        return {**session, "_sid": sid}
    SESSIONS.pop(sid, None)
    return None


def _session_cookie_header(sid: str, clear: bool = False) -> str:
    parts = [f"session={sid if not clear else ''}", "HttpOnly", "Secure", "SameSite=Lax", "Path=/"]
    if clear:
        parts.append("Max-Age=0")
    return "; ".join(parts)


class App:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.caldav = CalDAVClient(settings.caldav_url, settings.caldav_username, settings.caldav_password)
        self._calendars_cache: dict[str, str] | None = None

    # -- calendar helpers -----------------------------------------------

    def _calendars(self) -> dict[str, str]:
        """One PROPFIND lists every calendar's href at once -- cache that
        single result per-process instead of re-fetching (or worse, issuing
        one PROPFIND per calendar name) on every occurrence/calendar check
        during a page load."""
        if self._calendars_cache is None:
            self._calendars_cache = self.caldav.list_calendars()
        return self._calendars_cache

    def _href(self, display_name: str) -> str:
        calendars = self._calendars()
        if display_name not in calendars:
            raise CalDAVError(
                f"calendar '{display_name}' not found among {list(calendars)} -- "
                "check settings.toml [calendar].booking_calendar / conflict_calendars"
            )
        return calendars[display_name]

    def _conflict_checker(self, exclude_own: bool):
        def check(start: datetime, end: datetime) -> bool:
            # Check EVERY configured conflict calendar, not just the first --
            # settings.toml can list several (e.g. a personal calendar plus
            # the booking calendar itself).
            for calendar_name in self.settings.conflict_calendars:
                href = self._href(calendar_name)
                for uid, _ics, _etag in self.caldav.query_events(href, start, end):
                    if exclude_own and calendar_sync.is_own_event(uid, self.settings):
                        continue
                    return True
            return False
        return check

    def _sync(self, course_shortname: str, occurrence_date: date) -> None:
        course = self.settings.course(course_shortname)
        href = self._href(self.settings.booking_calendar)
        calendar_sync.sync_occurrence(self.caldav, href, self.store, self.settings, course, occurrence_date)

    def _cancel_and_promote(self, course_shortname: str, occurrence_date_str: str) -> None:
        """Call right after store.cancel(): if that freed a confirmed spot,
        promote the longest-waiting person on the waitlist and email them,
        then re-sync the calendar event once with the final state."""
        course = self.settings.course(course_shortname)
        promoted = self.store.promote_next_waitlisted(course_shortname, occurrence_date_str, course.capacity)
        if promoted:
            user = self.store.find_user_by_id(promoted.user_id)
            if user:
                send_mail(
                    self.settings, user.email,
                    f"You're in! {course.title} on {occurrence_date_str}",
                    f"A spot opened up for {course.title} on {occurrence_date_str} at "
                    f"{course.start_time}, and you were next on the waitlist -- you're now confirmed.\n",
                )
        self._sync(course_shortname, date.fromisoformat(occurrence_date_str))

    # -- routing -----------------------------------------------------------

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        # DEBUG-only (MY_BOOKING_DEBUG=1, see app/logutil.py): method+path
        # only, never query strings/form bodies/cookies, so this is safe to
        # leave on without leaking anything from a booking form into logs.
        log.debug("%s %s", method, path)
        try:
            status, headers, body = self.route(method, path, environ)
        except Exception:  # noqa: BLE001 - last-resort handler
            # Always logged (ERROR, not gated behind MY_BOOKING_DEBUG) with
            # the full traceback -- this is the one place an unhandled bug
            # anywhere in the app surfaces, and it used to go nowhere but
            # the client's browser. `journalctl -u my-booking.service` is
            # what to grab and share if you hit this.
            log.error("unhandled exception for %s %s", method, path, exc_info=True)
            status = "500 Internal Server Error"
            headers = [("Content-Type", "text/plain")]
            # Deliberately generic to the client -- the real detail is in
            # the log line above, not in the HTTP response body.
            body = "error: something went wrong on our end. Please try again shortly."
        start_response(status, headers)
        return [body.encode("utf-8")]

    def route(self, method: str, path: str, environ) -> tuple[str, list, str]:
        if m := re.fullmatch(r"/book/([a-z0-9-]+)", path):
            return self.book(method, m.group(1), environ)
        if m := re.fullmatch(r"/cancel/([A-Za-z0-9_-]+)", path):
            return self.guest_cancel(method, m.group(1), environ)
        if path == "/my":
            return self.my(method, environ)
        if path == "/my/reset":
            return self.my_reset(method, environ)
        if m := re.fullmatch(r"/my/confirm/([A-Za-z0-9_-]+)", path):
            return self.my_confirm(method, m.group(1), environ)
        if m := re.fullmatch(r"/my/cancel/([0-9a-fA-F-]+)", path):
            return self.my_cancel(method, m.group(1), environ)
        if path == "/my/delete-account":
            return self.my_delete_account(method, environ)
        if path == "/admin/login":
            return self.admin_login(method, environ)
        if path == "/admin":
            return self.admin_overview(method, environ)
        if m := re.fullmatch(r"/admin/cancel/([0-9a-fA-F-]+)", path):
            return self.admin_cancel(method, m.group(1), environ)
        return "404 Not Found", [("Content-Type", "text/plain")], "not found"

    @staticmethod
    def _read_form(environ) -> dict:
        try:
            size = int(environ.get("CONTENT_LENGTH", 0) or 0)
        except ValueError:
            size = 0
        raw = environ["wsgi.input"].read(size).decode("utf-8") if size else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # -- /book ---------------------------------------------------------------

    def book(self, method: str, shortname: str, environ):
        course = self.settings.course(shortname)
        if course is None:
            return "404 Not Found", [("Content-Type", "text/plain")], "unknown course"

        def capacity_lookup(sn, d):
            return self.store.count_confirmed(sn, d.isoformat())

        now = datetime.now(timezone.utc)
        occurrences = build_occurrences(
            course, self.settings, now, capacity_lookup, self._conflict_checker(exclude_own=True)
        )

        if method == "POST":
            form = self._read_form(environ)
            if form.get("agree") != "on":
                return self._book_page(course, occurrences, error="Please acknowledge the participation terms.")
            occ_date = form.get("occurrence_date", "")
            occ = {o.date.isoformat(): o for o in occurrences}.get(occ_date)
            if occ is None:
                return self._book_page(course, occurrences, error="That slot is no longer available.")
            email, name = form.get("email", "").strip(), form.get("name", "").strip()
            if not email or "@" not in email or not name:
                return self._book_page(course, occurrences, error="Please fill in your name and a valid email.")

            rejection = self._late_booking_rejection(occ, now)
            if rejection:
                return self._book_page(course, occurrences, error=rejection)

            # No password is ever collected here -- upsert_user_for_booking
            # only ever touches `name`, leaving any existing account's
            # password_hash (confirmed or still empty) completely alone.
            # This is what closes the old hole where re-submitting someone
            # else's email with a chosen PIN silently took over their
            # account: nothing reachable from this form can change another
            # email's credential anymore.
            user = self.store.upsert_user_for_booking(email, name)

            if user.password_hash:
                # Already-confirmed account: book instantly, exactly as
                # before. Capacity is (re)checked and the row inserted
                # atomically, inside one locked read-modify-write cycle
                # (see Store.add_registration_checking_capacity) --
                # capacity shown in the form is just a hint at page-load
                # time; two people could otherwise both pass a separate
                # "is it full?" check for the last spot and both land as
                # confirmed.
                token = new_token()
                reg = self.store.add_registration_checking_capacity(
                    shortname, occ_date, user.user_id, hash_token(token), course.capacity
                )
                self._sync(shortname, date.fromisoformat(occ_date))
                self._send_booking_result_email(user, course, occ_date, reg.status, token)
                msg = (
                    f"You're on the waitlist for <b>{esc(course.title)}</b> on {esc(occ_date)}."
                    if reg.status == STATUS_WAITLISTED
                    else f"You're booked for <b>{esc(course.title)}</b> on {esc(occ_date)}."
                )
                return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
                    "Booked!", f"<p>{msg} Check your email for confirmation and a cancel link.</p>"
                )

            # Brand-new or still-unconfirmed email: deliberately does NOT
            # hold a real spot or touch the calendar yet -- see
            # storage.STATUS_PENDING_CONFIRMATION's docstring. Re-sending
            # the confirmation email on every such attempt (rather than
            # trying to detect "they already have one pending") is
            # deliberate: simpler, and it's exactly what a "resend" should
            # do anyway.
            self.store.add_registration(
                shortname, occ_date, user.user_id, "", status=STATUS_PENDING_CONFIRMATION
            )
            self._send_confirm_email(user)
            body = (
                f"<p>Almost there -- we've emailed <b>{esc(email)}</b> a link to confirm your account. "
                f"Your spot for <b>{esc(course.title)}</b> on {esc(occ_date)} is held only once you "
                "click it and set a password, not before.</p>"
                '<p>Didn\'t get it? <a href="/my/reset">Resend the confirmation email</a>.</p>'
            )
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page("Almost there", body)

        return self._book_page(course, occurrences)

    def _send_booking_result_email(self, user, course, occ_date: str, status: str, cancel_token: str) -> None:
        """The guest-facing booked/waitlisted email + admin notification --
        shared by the instant-booking path above and by my_confirm()'s
        promotion of a newly-confirmed account's pending registrations, so
        the two paths can never drift out of sync in wording."""
        cancel_url = f"{self.settings.base_url}/cancel/{cancel_token}"
        if status == STATUS_WAITLISTED:
            send_mail(
                self.settings, user.email, f"Waitlisted: {course.title} on {occ_date}",
                f"{course.title} on {occ_date} at {course.start_time} is full. You've been added "
                "to the waitlist and will be confirmed automatically by email if a spot opens up.\n\n"
                f"Leave the waitlist any time: {cancel_url}\n",
            )
        else:
            send_mail(
                self.settings, user.email, f"Booking confirmed: {course.title} on {occ_date}",
                f"Your spot for {course.title} on {occ_date} at {course.start_time} is confirmed.\n\n"
                f"Cancel any time: {cancel_url}\n",
            )
        send_mail(
            self.settings, self.settings.admin_email,
            f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
            f"{user.name} <{user.email}> {'joined the waitlist for' if status == STATUS_WAITLISTED else 'booked'} "
            f"{course.shortname} on {occ_date}.",
        )

    def _send_confirm_email(self, user) -> None:
        """(Re)generates a confirm-or-reset token and emails the "set your
        password" link -- called from a booking under a not-yet-confirmed
        email, and from /my/reset's unified resend/forgot-password flow.
        Regenerating unconditionally on every call is deliberate: simpler
        than trying to detect/reuse an already-outstanding token, and it
        invalidates any earlier link, which is exactly the right behavior
        for a "resend" anyway."""
        token = new_token()
        self.store.set_confirm_token(user.user_id, hash_token(token), now_iso())
        confirm_url = f"{self.settings.base_url}/my/confirm/{token}"
        first_time = not user.password_hash
        subject = "Confirm your account" if first_time else "Reset your password"
        verb = "confirm your account and set a password" if first_time else "set a new password"
        send_mail(
            self.settings, user.email, subject,
            f"Click below to {verb}:\n\n{confirm_url}\n\n"
            "If you didn't request this, you can safely ignore this email.",
        )

    def _late_booking_rejection(self, occ, now: datetime) -> str | None:
        """None if `occ` can be booked normally right now; otherwise a
        short, guest-facing rejection message.

        Only ever rejects a LATE booking (within min_notice_hours of
        start), and only when this booking would still leave the course
        under min_required_participants -- if quorum's already met, or
        this booking is the one that reaches it, it's allowed like any
        other booking. Never rejects once `occ` is already full (that
        booking only joins the waitlist, which can't affect whether the
        course runs). Default min_required_participants=1 makes this
        always return None, since a single booking already reaches 1.
        """
        if occ.is_full:
            return None
        is_late = occ.start < now + timedelta(hours=self.settings.min_notice_hours)
        if not is_late:
            return None
        would_be_confirmed = occ.spots_taken + 1
        if would_be_confirmed >= self.settings.min_required_participants:
            return None
        return (
            f"Too late to book: fewer than {self.settings.min_required_participants} "
            "people are signed up, so this session needs more notice to run. "
            f"Please book at least {self.settings.min_notice_hours}h ahead next time."
        )

    def _spots_left_text(self, o) -> str:
        """Text shown next to the date on the booking page -- e.g.
        "3 spots left"/"1 spot left" or "FULL, join waitlist".
        `spots_left_offset` (settings.toml [defaults], default 0) can shift
        the *displayed* number for A/B-testing whether perceived scarcity
        changes booking behaviour -- deliberately display-only:

        - The real confirmed-vs-waitlisted decision always uses the true
          count (Store.add_registration_checking_capacity), completely
          independent of this text -- faking this number can never cause
          over-booking or a wrongly-waitlisted guest.
        - An occurrence that's genuinely full always says so here, offset
          or not -- what "join waitlist" promises has to stay true, since
          that's exactly what happens if someone submits the form. Only
          the number shown while there's real room left is adjustable
          (floored at 1, so it's never "0 spots left" while a booking
          from here would in fact be confirmed, not waitlisted).
        """
        if not self.settings.show_spots_left:
            return ""
        if o.is_full:
            return "FULL, join waitlist"
        shown = o.spots_left - self.settings.spots_left_offset
        shown = max(1, min(shown, o.capacity))
        unit = "spot" if shown == 1 else "spots"
        return f"{shown} {unit} left"

    def _policy_note(self) -> str:
        """Short note on the booking page about the late-booking quorum
        rule -- shown only when it can actually matter. With the default
        min_required_participants=1 this returns "" (a single booking
        always meets that on its own), which is also what keeps the
        DEFAULT install's booking page accurate without any manual edits."""
        if self.settings.min_required_participants <= 1:
            return ""
        return (
            f"This session needs {self.settings.min_required_participants}+ people to run. "
            f"Booking within {self.settings.min_notice_hours}h of start only goes through "
            "if that's still reachable."
        )

    def _book_page(self, course, occurrences, error: str | None = None):
        # course.subtitle is optional: unset (None, the default -- key
        # omitted in settings.toml) auto-derives "<Weekday>s -- <location>";
        # set to "" explicitly suppresses the subtitle entirely; any other
        # string overrides the auto-derived one. Always plain text (esc()'d)
        # -- unlike `description` below, this isn't meant to hold rich HTML.
        subtitle_text = course.subtitle if course.subtitle is not None else f"{course.weekday_label()}s -- {course.location}"
        subtitle = f'<p class="subtitle">{esc(subtitle_text)}</p>' if subtitle_text else ""
        # course.description is operator-authored (settings.toml, edited by
        # whoever runs this install), not guest-submitted -- unlike every
        # other value on this page it's deliberately rendered as raw HTML,
        # not passed through esc(), so it can hold rich text (bold/italic/
        # underline, links, bullet lists -- see settings.toml.example).
        # Same trust boundary as the hand-authored site/*.html pages
        # elsewhere in this project: whoever can edit settings.toml already
        # has full control of the server, so this isn't a new privilege
        # boundary, just a place raw HTML is intentionally allowed through.
        desc_html = f'<div class="description">{course.description}</div>' if course.description else ""
        if not occurrences:
            body = subtitle + desc_html + "<p>No upcoming slots -- if a date isn't listed, that session isn't happening.</p>"
        else:
            date_buttons = "".join(
                '<label class="date-btn">'
                f'<input type="radio" name="occurrence_date" value="{esc(o.date.isoformat())}" '
                f'data-date="{esc(o.date.isoformat())}" data-full="{"1" if o.is_full else "0"}"'
                + (" checked" if i == 0 else "")
                + '><span><span class="d-date">' + esc(o.date.isoformat()) + "</span>"
                + (f'<span class="d-spots">{esc(text)}</span>' if (text := self._spots_left_text(o)) else "")
                + "</span></label>"
                for i, o in enumerate(occurrences)
            )
            first_label = "Join waitlist" if occurrences[0].is_full else self.settings.book_button_label
            note_html = f'<p class="note">{esc(note)}</p>' if (note := self._policy_note()) else ""
            err_html = f'<p class="err">{esc(error)}</p>' if error else ""
            body = f"""
            {subtitle}
            {desc_html}
            {note_html}
            {err_html}
            <form method="post" class="card" id="book-form" data-book-label="{esc(self.settings.book_button_label)}">
              <label>Date
                <div class="dates" role="radiogroup" aria-label="Date">{date_buttons}</div>
              </label>
              <div class="selected-box">Selected date: <strong id="selected-date-text">{esc(occurrences[0].date.isoformat())}</strong></div>
              <label>Your name <span class="req">(required)</span>
                <input class="big-input" name="name" required></label>
              <label>Your email <span class="req">(required)</span>
                <input class="big-input" name="email" type="email" required></label>
              <p class="hint">First time booking with this email? We'll send a link to confirm your
                account and set a password -- your spot is held once you click it, not before.
                Booked with this email before? You're booked instantly, same as always.</p>
              <label><input type="checkbox" name="agree" required> I acknowledge the
                <a href="/terms.html" target="_blank">participation terms</a> (voluntary, at my own risk)
                <span class="req">(required)</span>.</label>
              <div class="submit-row">
                <button type="submit" id="book-submit">{esc(first_label)}</button>
              </div>
            </form>
            <script>
            (function() {{
              // Progressive enhancement only: the button starts enabled and
              // the required/pattern attributes above already block an
              // invalid submit without any JS at all -- this just adds a
              // nicer disabled state + live date/label switching on top for
              // guests with JS enabled.
              var form = document.getElementById("book-form");
              var radios = form.querySelectorAll('input[name="occurrence_date"]');
              var selText = document.getElementById("selected-date-text");
              var submitBtn = document.getElementById("book-submit");
              var nameEl = form.querySelector('[name="name"]');
              var emailEl = form.querySelector('[name="email"]');
              var agreeEl = form.querySelector('[name="agree"]');

              function currentRadio() {{
                for (var i = 0; i < radios.length; i++) {{ if (radios[i].checked) return radios[i]; }}
                return null;
              }}
              var bookLabel = form.dataset.bookLabel || "Book";
              function refresh() {{
                var r = currentRadio();
                if (r && selText) selText.textContent = r.dataset.date;
                if (r && submitBtn) submitBtn.textContent = r.dataset.full === "1" ? "Join waitlist" : bookLabel;
                var ok = !!r && nameEl.value.trim() !== "" && emailEl.value.indexOf("@") > 0 && agreeEl.checked;
                if (submitBtn) submitBtn.disabled = !ok;
              }}
              for (var i = 0; i < radios.length; i++) {{ radios[i].addEventListener("change", refresh); }}
              [nameEl, emailEl, agreeEl].forEach(function(el) {{
                el.addEventListener("input", refresh);
                el.addEventListener("change", refresh);
              }});
              refresh();
            }})();
            </script>"""
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(course.title, body)

    # -- /cancel/<token> (guest, from email) ---------------------------------

    def guest_cancel(self, method: str, token: str, environ):
        reg = self.store.find_by_guest_token_hash(hash_token(token))
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page("Not found", "<p>This link is invalid or already used.</p>")
        course = self.settings.course(reg.course_shortname)
        if method == "POST":
            self.store.cancel(reg.registration_id, canceled_by="guest")
            self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
            send_mail(
                self.settings, self.settings.admin_email,
                f"Cancellation: {course.title} on {reg.occurrence_date}",
                "Canceled by guest via their email link.",
            )
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Your booking has been canceled.</p>")
        body = f"""<p>Cancel your booking for <b>{esc(course.title)}</b> on {esc(reg.occurrence_date)}?</p>
        <form method="post"><button type="submit">Yes, cancel it</button></form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Cancel booking", body)

    # -- /my (guest self-service) --------------------------------------------

    def my(self, method: str, environ):
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            regs = self.store.registrations_for_user(session["user_id"])
            rows = "".join(
                f"<tr><td>{esc(r.course_shortname)}</td><td>{esc(r.occurrence_date)}</td>"
                f"<td>{esc(r.status)}</td>"
                f'<td><form method="post" action="/my/cancel/{esc(r.registration_id)}">'
                f'<button {"disabled" if r.status != STATUS_CONFIRMED else ""}>Cancel</button></form></td></tr>'
                for r in regs
            )
            body = f"""
            <table border="1" cellpadding="6"><tr><th>Course</th><th>Date</th><th>Status</th><th>Actions</th></tr>{rows}</table>
            <form method="post" action="/my/delete-account" onsubmit="return confirm('Delete your account and all booking history? This cancels any future bookings too.');">
              <button type="submit">Delete my account &amp; data</button>
            </form>"""
            return "200 OK", [("Content-Type", "text/html")], page("My bookings", body)

        error = None
        if method == "POST":
            form = self._read_form(environ)
            email, password = form.get("email", "").strip(), form.get("password", "").strip()
            if not login_limiter.allow(f"guest:{email.lower()}"):
                error = "Too many attempts -- try again later."
            else:
                user = self.store.find_user_by_email(email)
                # user.password_hash is empty for a not-yet-confirmed
                # account -- bail out before verify_secret rather than
                # feeding it an empty hash/salt.
                if user and user.password_hash and verify_secret(password, user.password_hash, user.password_salt):
                    sid = _new_session({"kind": "guest", "user_id": user.user_id})
                    self.store.touch_login(user.user_id)
                    return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header(sid))], ""
                error = "Email/password didn't match."
        err_html = f'<p class="err">{esc(error)}</p>' if error else ""
        body = f"""{err_html}<form method="post" class="card">
          <label>Email <input name="email" type="email" required></label>
          <label>Password <input name="password" type="password" required></label>
          <button type="submit">View my bookings</button>
        </form>
        <p><a href="/my/reset">Forgot your password, or still need to confirm your account?</a></p>"""
        return "200 OK", [("Content-Type", "text/html")], page("My bookings", body)

    def my_reset(self, method: str, environ):
        """Unified "forgot password" + "resend confirmation" flow -- both
        reduce to the same thing (email a link to /my/confirm/<token>), so
        one form/route covers both instead of two near-duplicates. Always
        shows the exact same response regardless of whether the email is
        registered, confirmed, or unknown -- this endpoint must never leak
        which emails exist in the system."""
        if method == "POST":
            form = self._read_form(environ)
            email = form.get("email", "").strip()
            if login_limiter.allow(f"reset:{email.lower()}"):
                user = self.store.find_user_by_email(email)
                if user:
                    self._send_confirm_email(user)
            # Same body whether or not login_limiter allowed it, whether or
            # not the email exists -- an attacker probing for registered
            # emails, or trying to spam one inbox with reset emails, learns
            # nothing from the response either way.
            body = "<p>If that email has an account with us, we've just sent a link to set/reset your password.</p>"
            return "200 OK", [("Content-Type", "text/html")], page("Check your email", body)
        body = """<form method="post" class="card">
          <label>Email <input name="email" type="email" required></label>
          <button type="submit">Send me a link</button>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Forgot your password?", body)

    def _set_password_form(self, token: str) -> str:
        return f"""<form method="post" class="card">
          <label>New password <span class="req">(required)</span>
            <input class="big-input" name="password" type="password" minlength="4" required></label>
          <button type="submit">Set password</button>
        </form>"""

    def my_confirm(self, method: str, token: str, environ):
        """Landing page for BOTH the first-time account-confirmation link
        and a later password-reset link -- see storage.User.confirm_token_hash.
        Setting a password here also promotes every STATUS_PENDING_CONFIRMATION
        registration for this user, re-checking capacity fresh at this exact
        moment (it may have filled up while the account sat unconfirmed) --
        see Store.confirm_pending_registration."""
        user = self.store.find_user_by_confirm_token_hash(hash_token(token))
        if user is None:
            body = ('<p>This link is invalid or has already been used. '
                    '<a href="/my/reset">Request a new one</a>.</p>')
            return "200 OK", [("Content-Type", "text/html")], page("Link invalid", body)

        pending = [
            r for r in self.store.registrations_for_user(user.user_id)
            if r.status == STATUS_PENDING_CONFIRMATION
        ]
        pending_note = (
            f"<p>You have {len(pending)} pending booking(s) that will be confirmed "
            "once you set your password.</p>" if pending else ""
        )

        if method == "POST":
            form = self._read_form(environ)
            password = form.get("password", "").strip()
            if len(password) < 4:
                err = '<p class="err">Please choose a password at least 4 characters long.</p>'
                return "200 OK", [("Content-Type", "text/html")], page(
                    "Set your password", err + pending_note + self._set_password_form(token)
                )
            pw_hash, pw_salt = hash_secret(password)
            self.store.set_password(user.user_id, pw_hash, pw_salt)

            confirmed_lines = []
            for reg in pending:
                course = self.settings.course(reg.course_shortname)
                if course is None:
                    continue  # course removed from settings.toml since booking -- nothing to promote into
                new_cancel_token = new_token()
                updated = self.store.confirm_pending_registration(
                    reg.registration_id, course.capacity, hash_token(new_cancel_token)
                )
                if updated is None:
                    continue  # no longer pending (already handled, e.g. a stale duplicate link)
                self._sync(reg.course_shortname, date.fromisoformat(reg.occurrence_date))
                self._send_booking_result_email(user, course, reg.occurrence_date, updated.status, new_cancel_token)
                confirmed_lines.append(f"{course.title} on {reg.occurrence_date} ({updated.status})")

            sid = _new_session({"kind": "guest", "user_id": user.user_id})
            self.store.touch_login(user.user_id)
            summary = (
                "<ul>" + "".join(f"<li>{esc(line)}</li>" for line in confirmed_lines) + "</ul>"
                if confirmed_lines else ""
            )
            body = f"<p>Your password is set.</p>{summary}<p><a href=\"/my\">View my bookings</a></p>"
            return (
                "200 OK",
                [("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", _session_cookie_header(sid))],
                page("Account confirmed!", body),
            )

        return "200 OK", [("Content-Type", "text/html")], page(
            "Set your password", pending_note + self._set_password_form(token)
        )

    def my_cancel(self, method: str, registration_id: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return "403 Forbidden", [("Content-Type", "text/plain")], "log in first"
        reg = self.store.find_by_id(registration_id)
        if reg and reg.user_id == session["user_id"]:
            self.store.cancel(registration_id, canceled_by="guest")
            self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
        return "302 Found", [("Location", "/my")], ""

    def my_delete_account(self, method: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return "403 Forbidden", [("Content-Type", "text/plain")], "log in first"
        user = self.store.find_user_by_id(session["user_id"])
        if user:
            future_regs = [
                r for r in self.store.registrations_for_user(user.user_id)
                if r.status in (STATUS_CONFIRMED, STATUS_WAITLISTED)
            ]
            erase_user_by_email(self.store, self.settings, user.email)
            for r in future_regs:
                self._cancel_and_promote(r.course_shortname, r.occurrence_date)
        SESSIONS.pop(session["_sid"], None)
        return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header("", clear=True))], ""

    # -- /admin ---------------------------------------------------------------

    def admin_login(self, method: str, environ):
        error = None
        if method == "POST":
            form = self._read_form(environ)
            password = form.get("password", "")
            # Keyed by client IP, not a single global "admin" bucket --
            # otherwise anyone, unauthenticated, could lock the real admin
            # out of /admin/login for up to an hour with 5 wrong guesses
            # from any IP (a self-inflicted DoS the old global key allowed;
            # see the maintainer's local notes).
            if not login_limiter.allow(f"admin:{_client_ip(environ)}"):
                error = "Too many attempts -- try again later."
            elif verify_admin_password(password, self.settings.admin_password_hash):
                sid = _new_session({"kind": "admin"})
                return "302 Found", [("Location", "/admin"), ("Set-Cookie", _session_cookie_header(sid))], ""
            else:
                error = "Wrong password."
        err_html = f'<p class="err">{esc(error)}</p>' if error else ""
        body = f"""{err_html}<form method="post" class="card">
          <label>Admin password <input name="password" type="password" required></label>
          <button type="submit">Log in</button>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Admin login", body)

    def admin_overview(self, method: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "admin":
            return "302 Found", [("Location", "/admin/login")], ""
        show_past = "past=1" in environ.get("QUERY_STRING", "")
        today = datetime.now(timezone.utc).date()
        regs = self.store.all_registrations()
        if not show_past:
            regs = [r for r in regs if date.fromisoformat(r.occurrence_date) >= today]
        regs.sort(key=lambda r: (r.occurrence_date, r.course_shortname))
        rows = []
        for r in regs:
            user = self.store.find_user_by_id(r.user_id)
            times = self.store.times_registered(r.user_id) if user else 0
            rows.append(
                f"<tr><td>{esc(r.status)}</td><td>{esc(r.course_shortname)}</td>"
                f"<td>{esc(r.occurrence_date)}</td><td>{esc(user.name if user else '(erased)')}</td>"
                f"<td>{esc(user.email if user else '(erased)')}</td><td>{esc(r.registered_at)}</td>"
                f"<td>{times}</td>"
                f'<td><a href="/admin/cancel/{esc(r.registration_id)}">cancel</a></td></tr>'
            )
        toggle = '<a href="/admin">today + future only</a>' if show_past else '<a href="/admin?past=1">include past</a>'
        body = f"""<p>{toggle}</p>
        <table border="1" cellpadding="6">
        <tr><th>Status</th><th>Course</th><th>Date</th><th>Name</th><th>Email</th><th>Registered</th><th>Times booked</th><th>Actions</th></tr>
        {''.join(rows)}</table>"""
        return "200 OK", [("Content-Type", "text/html")], page("Admin overview", body)

    def admin_cancel(self, method: str, registration_id: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "admin":
            return "302 Found", [("Location", "/admin/login")], ""
        reg = self.store.find_by_id(registration_id)
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/plain")], "not found"
        user = self.store.find_user_by_id(reg.user_id)
        course = self.settings.course(reg.course_shortname)
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            self.store.cancel(registration_id, canceled_by="host", host_message=message)
            self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
            if user:
                send_mail(
                    self.settings, user.email, f"Canceled: {course.title} on {reg.occurrence_date}",
                    f"Your booking for {course.title} on {reg.occurrence_date} was canceled by the host."
                    + (f"\n\nMessage: {message}" if message else ""),
                )
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Registration canceled and guest notified.</p>")
        body = f"""
        <p>About to cancel <b>{esc(user.name if user else '(erased)')}</b>
        ({esc(user.email if user else '(erased)')}) for
        <b>{esc(course.title)}</b> on {esc(reg.occurrence_date)}.</p>
        <form method="post">
          <label>Optional message to them <textarea name="message" rows="3" cols="40"></textarea></label>
          <button type="submit">Cancel this booking</button>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Cancel registration", body)
