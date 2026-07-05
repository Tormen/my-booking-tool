"""wsgiref-based web app -- no framework dependency. Routes:

  GET/POST /book/<shortname>        guest booking form
  GET/POST /cancel/<token>          guest self-cancel (link from email)
  GET/POST /my                      guest login (email+PIN) / bookings list
  POST     /my/cancel/<reg_id>      guest cancels one of their own bookings
  POST     /my/delete-account       guest erases their own account (Art. 17)
  GET/POST /admin/login             admin login
  GET      /admin                   admin overview (today+future by default)
  GET/POST /admin/cancel/<reg_id>   host cancels a registration, optional message

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
from .storage import STATUS_CONFIRMED, STATUS_WAITLISTED, Store
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
            email, name, pin = form.get("email", "").strip(), form.get("name", "").strip(), form.get("pin", "").strip()
            if not email or "@" not in email or not name or not re.fullmatch(r"\d{6}", pin):
                return self._book_page(course, occurrences, error="Please fill in name, a valid email, and a 6-digit code.")

            rejection = self._late_booking_rejection(occ, now)
            if rejection:
                return self._book_page(course, occurrences, error=rejection)

            pin_hash, pin_salt = hash_secret(pin)
            user = self.store.upsert_user(email, name, pin_hash, pin_salt)
            # Capacity is (re)checked and the row inserted atomically, inside
            # one locked read-modify-write cycle (see
            # Store.add_registration_checking_capacity) -- capacity shown in
            # the form is just a hint at page-load time; two people could
            # otherwise both pass a separate "is it full?" check for the
            # last spot and both land as confirmed.
            token = new_token()
            reg = self.store.add_registration_checking_capacity(
                shortname, occ_date, user.user_id, hash_token(token), course.capacity
            )
            status = reg.status
            self._sync(shortname, date.fromisoformat(occ_date))

            cancel_url = f"{self.settings.base_url}/cancel/{token}"
            if status == STATUS_WAITLISTED:
                send_mail(
                    self.settings, email, f"Waitlisted: {course.title} on {occ_date}",
                    f"{course.title} on {occ_date} at {course.start_time} is full. You've been added "
                    "to the waitlist and will be confirmed automatically by email if a spot opens up.\n\n"
                    f"Leave the waitlist any time: {cancel_url}\n",
                )
            else:
                send_mail(
                    self.settings, email, f"Booking confirmed: {course.title} on {occ_date}",
                    f"Your spot for {course.title} on {occ_date} at {course.start_time} is confirmed.\n\n"
                    f"Cancel any time: {cancel_url}\n",
                )
            send_mail(
                self.settings, self.settings.admin_email,
                f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
                f"{name} <{email}> {'joined the waitlist for' if status == STATUS_WAITLISTED else 'booked'} "
                f"{shortname} on {occ_date}.",
            )
            msg = (
                f"You're on the waitlist for <b>{esc(course.title)}</b> on {esc(occ_date)}."
                if status == STATUS_WAITLISTED
                else f"You're booked for <b>{esc(course.title)}</b> on {esc(occ_date)}."
            )
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
                "Booked!", f"<p>{msg} Check your email for confirmation and a cancel link.</p>"
            )

        return self._book_page(course, occurrences)

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
        """Text shown after the date in the booking dropdown -- e.g.
        "3 spot(s) left" or "FULL, join waitlist". `spots_left_offset`
        (settings.toml [defaults], default 0) can shift the *displayed*
        number for A/B-testing whether perceived scarcity changes booking
        behaviour -- deliberately display-only:

        - The real confirmed-vs-waitlisted decision always uses the true
          count (Store.add_registration_checking_capacity), completely
          independent of this text -- faking this number can never cause
          over-booking or a wrongly-waitlisted guest.
        - An occurrence that's genuinely full always says so here, offset
          or not -- what "join waitlist" promises has to stay true, since
          that's exactly what happens if someone submits the form. Only
          the number shown while there's real room left is adjustable
          (floored at 1, so it's never "0 spot(s) left" while a booking
          from here would in fact be confirmed, not waitlisted).
        """
        if not self.settings.show_spots_left:
            return ""
        if o.is_full:
            return "FULL, join waitlist"
        shown = o.spots_left - self.settings.spots_left_offset
        shown = max(1, min(shown, o.capacity))
        return f"{shown} spot(s) left"

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
        if not occurrences:
            body = "<p>No upcoming slots -- if a date isn't listed, that session isn't happening.</p>"
        else:
            options = "".join(
                f'<option value="{esc(o.date.isoformat())}">{esc(o.date.isoformat())}'
                + (f" -- {esc(text)}" if (text := self._spots_left_text(o)) else "")
                + "</option>"
                for o in occurrences
            )
            note_html = f'<p class="note">{esc(note)}</p>' if (note := self._policy_note()) else ""
            err_html = f'<p class="err">{esc(error)}</p>' if error else ""
            body = f"""
            {note_html}
            {err_html}
            <form method="post" class="card">
              <label>Date <select name="occurrence_date">{options}</select></label>
              <label>Your name <input name="name" required></label>
              <label>Your email <input name="email" type="email" required></label>
              <label>Pick a 6-digit code (to manage this booking later) <input name="pin" pattern="\\d{{6}}" maxlength="6" required></label>
              <label><input type="checkbox" name="agree"> I acknowledge the
                <a href="/terms.html" target="_blank">participation terms</a> (voluntary, at my own risk).</label>
              <button type="submit">Book / join waitlist</button>
            </form>"""
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
            <table border="1" cellpadding="6"><tr><th>Course</th><th>Date</th><th>Status</th><th></th></tr>{rows}</table>
            <form method="post" action="/my/delete-account" onsubmit="return confirm('Delete your account and all booking history? This cancels any future bookings too.');">
              <button type="submit">Delete my account &amp; data</button>
            </form>"""
            return "200 OK", [("Content-Type", "text/html")], page("My bookings", body)

        error = None
        if method == "POST":
            form = self._read_form(environ)
            email, pin = form.get("email", "").strip(), form.get("pin", "").strip()
            if not login_limiter.allow(f"guest:{email.lower()}"):
                error = "Too many attempts -- try again later."
            else:
                user = self.store.find_user_by_email(email)
                if user and verify_secret(pin, user.pin_hash, user.pin_salt):
                    sid = _new_session({"kind": "guest", "user_id": user.user_id})
                    self.store.touch_login(user.user_id)
                    return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header(sid))], ""
                error = "Email/code didn't match."
        err_html = f'<p class="err">{esc(error)}</p>' if error else ""
        body = f"""{err_html}<form method="post" class="card">
          <label>Email <input name="email" type="email" required></label>
          <label>6-digit code <input name="pin" pattern="\\d{{6}}" maxlength="6" required></label>
          <button type="submit">View my bookings</button>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("My bookings", body)

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
        <tr><th>Status</th><th>Course</th><th>Date</th><th>Name</th><th>Email</th><th>Registered</th><th>Times booked</th><th></th></tr>
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
