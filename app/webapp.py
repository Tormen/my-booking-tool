"""wsgiref-based web app -- no framework dependency. Routes:

  GET      /courses                 overview of every configured course, linking to /book/<shortname>
  GET/POST /book/<shortname>        guest booking form (name+email only)
  GET/POST /cancel/<token>          guest self-cancel (link from email)
  GET/POST /my                      guest login (email+password, "Login"/"Sign up" tabs) / bookings list
  POST     /my/signup               "Sign up" tab's target -- create account + email a confirm link
  GET/POST /my/confirm/<token>      set password -- first-time account confirmation
                                     AND password reset both land here (same token
                                     mechanism, see storage.User.confirm_token_hash)
  GET/POST /my/reset                request a confirm/reset link by email (always
                                     the same response either way -- doesn't reveal
                                     whether an email is registered)
  POST     /my/cancel/<reg_id>      guest cancels one of their own bookings
  POST     /my/logout               guest logout
  POST     /my/delete-account       guest erases their own account (Art. 17)
  GET      /my/session               JSON {"logged_in": bool, "email": ...} for the
                                     STATIC homepage's own JS to check (see my_session_status)
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

import json
import logging
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from http import cookies
from urllib.parse import parse_qs, urlparse

from . import calendar_sync
from .caldav_client import CalDAVClient, CalDAVError
from .cancel_flow import cancel_and_promote
from .cancellation import booking_details_text, html_to_text, send_cancellation_emails
from .config import Settings
from .emailer import _masked, send_mail
from .erasure import erase_user_by_email
from .security import (
    RateLimiter, hash_email_for_erasure, hash_secret, hash_token, is_erased_email, new_token,
    sanitize_csv_field, tokens_match, verify_admin_password, verify_secret,
)
from .slots import build_occurrences
from .storage import (
    STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION, STATUS_WAITLISTED, Registration, Store, User, now_iso,
)
from .templates import esc, page

log = logging.getLogger("my_booking.webapp")

SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 60 * 60 * 4

login_limiter = RateLimiter(max_attempts=5, window_seconds=3600)

# /my/reset is keyed by email (above) to stop one inbox from being bombed
# with reset/confirm emails -- but that alone doesn't slow down someone
# trying many DIFFERENT (mostly nonexistent) addresses from one source to
# probe for registered accounts, since each fresh email string gets its
# own untouched counter. This second, per-IP limiter (looser: 20/hour,
# vs. 5/hour per email) closes that gap -- same idea as admin_login's
# per-IP limiter, just a higher ceiling since a shared IP (office/family)
# resetting several different real accounts in an hour is plausible here
# in a way it isn't for repeated admin-password guesses. Both limiters are
# checked/incremented before find_user_by_email is ever consulted, so
# which one (if either) blocks a request never depends on whether the
# submitted email actually has an account -- see my_reset()'s own comment.
reset_ip_limiter = RateLimiter(max_attempts=20, window_seconds=3600)

# 4 was too low -- NIST SP 800-63B's own minimum recommendation is 8.
# hashlib.scrypt (see .security) has no upper length limit and no
# forbidden-character restriction (any Unicode encodes to bytes just
# fine), so this floor is the only real constraint worth surfacing to
# the guest on the set-password form.
MIN_PASSWORD_LENGTH = 8

# 2026-07-07, the operator: "when will the confirmation links be invalid?" -- until
# this, they never expired at all (confirm_token_created_at was stored but
# nothing ever checked it). A leaked/forwarded/very-old link would work
# forever. 24h matches typical "reset password" link conventions -- see
# my_confirm()'s expiry check and _send_confirm_email()'s email text.
CONFIRM_TOKEN_TTL_HOURS = 24

# Guest bookings (2026-07, "+ Add participant" on the booking form, mirroring
# SimplyMeet.me's own UX): a hard ceiling on how many guest rows book()
# will ever look for (guest_email_0.. guest_email_{MAX_GUESTS-1}), so a
# hand-crafted POST can't make it scan an unbounded number of form fields.
# The form's own JS also stops offering "+ Add participant" once this many
# rows exist -- see _book_page()'s guest-rows script.
MAX_GUESTS = 9

# 2026-07-06: "/my should always show the past 3 courses they scheduled."
# Upcoming bookings are always shown in full (there's rarely more than a
# couple at once); past bookings are capped at this many (most recent
# first) so the page doesn't grow forever for someone who's booked for
# years -- see my()'s docstring.
MY_PAST_BOOKINGS_LIMIT = 3


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


# Client-side-only cooldown layered on top of the real server-side limit
# (login_limiter, 5/hour -- see my_reset()) -- this is just about not
# letting a guest spam-click "resend"/"send me a link" and wonder why
# nothing happens faster. A plain in-memory JS variable wouldn't survive
# a page reload/navigation, so localStorage is the natural fit; one
# shared key across every page that can trigger /my/reset (the "Almost
# there" resend button and /my/reset's own form) means bouncing between
# them doesn't reset the cooldown. Progressive enhancement in both
# variants below: the control starts enabled and (for the navigating
# variant) still works with JS disabled, same principle as _book_page's
# own inline script.
_RESEND_COOLDOWN_SECONDS = 60
_RESEND_COOLDOWN_KEY = "mb_resend_until"


def _resend_cooldown_script(button_id: str, label: str) -> str:
    """For a form that's MEANT to navigate on submit (/my/reset's own
    "Send me a link" form) -- just disables the button with a live
    countdown if a cooldown from an earlier submit (on this page or the
    "Almost there" page) is still running when the page loads."""
    return f"""<script>
    (function() {{
      var btn = document.getElementById({json.dumps(button_id)});
      if (!btn) return;
      var label = {json.dumps(label)};
      function tick() {{
        var until = parseInt(localStorage.getItem({json.dumps(_RESEND_COOLDOWN_KEY)}) || "0", 10);
        var left = Math.ceil((until - Date.now()) / 1000);
        if (left > 0) {{
          btn.disabled = true;
          btn.textContent = label + " (" + left + "s)";
          setTimeout(tick, 250);
        }} else {{
          btn.disabled = false;
          btn.textContent = label;
        }}
      }}
      tick();
    }})();
    </script>"""


def _resend_cooldown_inline_script(form_id: str, button_id: str, status_id: str, label: str) -> str:
    """For the "Almost there" page's resend button specifically: submits
    via fetch() instead of a real form navigation, so clicking it does
    NOT take the guest to /my/reset's own page (confusing right after a
    booking -- that page is branded "Forgot your password?", which this
    guest never had one to forget). Falls back to a real (navigating)
    form submit if fetch isn't available/JS is disabled -- landing on
    /my/reset in that case is a worse experience than no JS at all, but
    still gets the email sent, which matters more."""
    return f"""<script>
    (function() {{
      var form = document.getElementById({json.dumps(form_id)});
      var btn = document.getElementById({json.dumps(button_id)});
      var status = document.getElementById({json.dumps(status_id)});
      if (!form || !btn) return;
      var label = {json.dumps(label)};
      function tick() {{
        var until = parseInt(localStorage.getItem({json.dumps(_RESEND_COOLDOWN_KEY)}) || "0", 10);
        var left = Math.ceil((until - Date.now()) / 1000);
        if (left > 0) {{
          btn.disabled = true;
          btn.textContent = label + " (" + left + "s)";
          setTimeout(tick, 250);
        }} else {{
          btn.disabled = false;
          btn.textContent = label;
        }}
      }}
      form.addEventListener("submit", function(ev) {{
        if (!window.fetch) return;  // no fetch: let the real submit go through
        ev.preventDefault();
        localStorage.setItem({json.dumps(_RESEND_COOLDOWN_KEY)}, String(Date.now() + {_RESEND_COOLDOWN_SECONDS} * 1000));
        tick();
        if (status) status.textContent = " Sending...";
        fetch(form.action, {{method: "POST", body: new URLSearchParams(new FormData(form))}})
          .then(function() {{ if (status) status.textContent = " Sent -- check your email."; }})
          .catch(function() {{ if (status) status.textContent = " Couldn't send -- try again."; }});
      }});
      tick();
    }})();
    </script>"""


def _lockout_countdown_script(seconds: float, button_id: str, label: str) -> str:
    """Disables a login form's submit button and counts down from
    `seconds` back to enabled -- `seconds` is the server-computed,
    authoritative time left on login_limiter's lockout for this key (see
    security.RateLimiter.retry_after), NOT a client guess. Unlike
    _resend_cooldown_script/_resend_cooldown_inline_script above, this
    needs no localStorage: the lockout state already lives server-side in
    login_limiter, so every fresh page load (including a plain refresh)
    already gets the true remaining time recomputed from scratch --
    nothing to persist client-side across a reload. Re-enabling at 0 is
    optimistic (a resubmit right at that instant is still re-checked
    server-side and could show a fresh countdown if the clocks are a
    touch off) -- exactly the same spirit as the resend cooldown above."""
    return f"""<script>
    (function() {{
      var btn = document.getElementById({json.dumps(button_id)});
      if (!btn) return;
      var remaining = {int(seconds) + 1};
      var original = {json.dumps(label)};
      btn.disabled = true;
      function tick() {{
        if (remaining <= 0) {{
          btn.disabled = false;
          btn.textContent = original;
          return;
        }}
        btn.textContent = original + " (" + remaining + "s)";
        remaining -= 1;
        setTimeout(tick, 1000);
      }}
      tick();
    }})();
    </script>"""


# Shared by every page with a confirm-dialog (<dialog class="card">) button:
# /my's Cancel + Delete-account (see my()), and /admin overview's Cancel
# (see admin_overview()) -- pulled out into one constant so both can never
# drift apart the way the booking-confirmed and promoted-from-waitlist
# emails briefly did earlier the same day (see _booking_details_text()).
# No per-page interpolation needed (button/dialog pairing is entirely
# data-attribute driven), so this is a plain string, not an f-string.
_DIALOG_WIRING_SCRIPT = """<script>
(function() {
  document.querySelectorAll(".confirm-dialog-btn").forEach(function(btn) {
    var dlg = document.getElementById(btn.dataset.dialog);
    // No <dialog>/showModal support (old browser) or JS somehow only
    // half-loaded: leave the button/form alone -- for Cancel that's a
    // plain immediate submit (as before this feature existed), for
    // Delete that's the native onsubmit="confirm(...)" already on the
    // form, still a real (if plainer) confirmation either way.
    if (!dlg || typeof dlg.showModal !== "function") return;
    // The dialog now handles confirmation -- clear any
    // onsubmit="confirm(...)" on the form so the guest isn't asked
    // twice (once by the dialog, once natively) when the dialog's own
    // submit button actually submits it.
    if (btn.form) btn.form.onsubmit = null;
    btn.addEventListener("click", function(ev) {
      ev.preventDefault();
      dlg.showModal();
    });
  });
  document.querySelectorAll(".dialog-close-btn").forEach(function(btn) {
    btn.addEventListener("click", function() {
      var dlg = document.getElementById(btn.dataset.dialog);
      if (dlg) dlg.close();
    });
  });
})();
</script>"""


def _course_subtitle_html(course) -> str:
    """Shared by _book_page() and courses() (2026-07-06) so the two never
    drift apart on this. course.subtitle is optional: unset (None, the
    default -- key omitted in settings.toml) auto-derives "<Weekday>s
    <from>h<mm> - <till>h<mm> -- <location>" (e.g. "Saturdays 10h45 -
    12h45 -- Ayur Yoga Center Trier Nord"); set to "" explicitly
    suppresses the subtitle entirely; any other string overrides the
    auto-derived one. Always plain text (esc()'d) -- unlike `description`,
    this isn't meant to hold rich HTML."""
    text = (
        course.subtitle if course.subtitle is not None
        else f"{course.weekday_label()}s {course.time_range_label()} -- {course.location}"
    )
    return f'<p class="subtitle">{esc(text)}</p>' if text else ""


def _sortable_filterable_table_script(table_id: str) -> str:
    """Client-side filter (substring, across every cell) + click-a-header-
    to-sort for a <table id="{table_id}"> with a <thead>/<tbody> and a
    sibling <input type="search" id="{table_id}-filter">. Standing default
    for every table in the app now (2026-07-05, see SOLUTION-DESIGN.md) --
    both /my's bookings table and /admin's overview table use this, so a
    future third table gets the same behavior for free rather than a
    one-off. Deliberately vanilla JS/no library: these tables are small
    (one operator's own bookings, or one small deployment's admin view),
    so a full data-grid dependency would be more weight than the problem
    needs. Sorting is index-based (numeric-aware, falls back to a
    locale-aware string compare) -- every row in a given table must have
    the same number of cells as the header for column indexes to line up
    (see admin_overview()'s comment on why an erased row's hash goes in
    the Email cell rather than a colspan, specifically because of this)."""
    return f"""<script>
(function() {{
  var table = document.getElementById({json.dumps(table_id)});
  if (!table || !table.tHead || !table.tBodies.length) return;
  var tbody = table.tBodies[0];
  var headerCells = Array.prototype.slice.call(table.tHead.rows[0].cells);
  var rows = Array.prototype.slice.call(tbody.rows);
  headerCells.forEach(function(th, idx) {{
    th.style.cursor = "pointer";
    var indicator = th.querySelector(".sort-indicator");
    th.addEventListener("click", function() {{
      var dir = th.dataset.dir === "asc" ? "desc" : "asc";
      headerCells.forEach(function(h) {{
        h.dataset.dir = "";
        var i = h.querySelector(".sort-indicator");
        if (i) i.textContent = "";
      }});
      th.dataset.dir = dir;
      if (indicator) indicator.textContent = dir === "asc" ? " ▲" : " ▼";
      var sorted = rows.slice().sort(function(a, b) {{
        var av = a.cells[idx] ? a.cells[idx].textContent.trim() : "";
        var bv = b.cells[idx] ? b.cells[idx].textContent.trim() : "";
        var an = parseFloat(av), bn = parseFloat(bv);
        var bothNumeric = av !== "" && bv !== "" && !isNaN(an) && !isNaN(bn)
          && /^-?[0-9.]+$/.test(av) && /^-?[0-9.]+$/.test(bv);
        var cmp = bothNumeric ? (an - bn) : av.localeCompare(bv, undefined, {{sensitivity: "base"}});
        return dir === "asc" ? cmp : -cmp;
      }});
      sorted.forEach(function(r) {{ tbody.appendChild(r); }});
    }});
  }});
  var filterInput = document.getElementById({json.dumps(table_id + "-filter")});
  if (filterInput) {{
    filterInput.addEventListener("input", function() {{
      var q = filterInput.value.trim().toLowerCase();
      rows.forEach(function(r) {{
        r.style.display = (!q || r.textContent.toLowerCase().indexOf(q) !== -1) ? "" : "none";
      }});
    }});
  }}
}})();
</script>"""


# Moved to app/cancellation.py (2026-07-06) so `my-bt cancel` (scripts/my-bt)
# can reuse it without importing the whole App/WSGI machinery -- re-exported
# under its old name here so nothing that imports webapp._html_to_text
# (existing tests included) needs to change.
_html_to_text = html_to_text


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
        """Thin wrapper around app.cancel_flow.cancel_and_promote (moved
        there 2026-07-06 so `my-bt cancel`/`my-bt erase`, which have no App
        instance, run the exact same promote+calendar-sync logic instead of
        a smaller reimplementation of it) -- kept as an App method since
        every existing call site here already has `self.store`/
        `self.settings`/`self.caldav`. Passes self._sync as the sync_fn
        override so this keeps using App's own cached calendar-href lookup
        (self._calendars/self._href) exactly as before, instead of the
        standalone function's one-off PROPFIND -- also what lets tests keep
        stubbing self.app._sync to a no-op. See cancel_and_promote's own
        docstring for the full rationale."""
        cancel_and_promote(
            self.store, self.settings, self.caldav, course_shortname, occurrence_date_str,
            sync_fn=lambda sn, occ_str: self._sync(sn, date.fromisoformat(occ_str)),
        )

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
        if path == "/courses":
            return self.courses(method, environ)
        if m := re.fullmatch(r"/book/([a-z0-9-]+)", path):
            return self.book(method, m.group(1), environ)
        if m := re.fullmatch(r"/cancel/([A-Za-z0-9_-]+)", path):
            return self.guest_cancel(method, m.group(1), environ)
        if path == "/my":
            return self.my(method, environ)
        if path == "/my/signup":
            return self.my_signup(method, environ)
        if path == "/my/reset":
            return self.my_reset(method, environ)
        if m := re.fullmatch(r"/my/confirm/([A-Za-z0-9_-]+)", path):
            return self.my_confirm(method, m.group(1), environ)
        if m := re.fullmatch(r"/my/cancel/([0-9a-fA-F-]+)", path):
            return self.my_cancel(method, m.group(1), environ)
        if path == "/my/logout":
            return self.my_logout(method, environ)
        if path == "/my/delete-account":
            return self.my_delete_account(method, environ)
        if path == "/my/session":
            return self.my_session_status(method, environ)
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

    # -- /courses --------------------------------------------------------------

    def courses(self, method: str, environ):
        """Overview of every configured course (2026-07-06: "an overview
        page as simplymeet.me... that shows all my possible courses", and
        the destination for /my's "New booking" button, since /my itself
        has no way to know which course a returning guest wants next).
        Lists every `[[course]]` in settings.toml, each linking to its own
        /book/<shortname> -- `audience` ("private"/"public") is display-
        only per settings.toml.example, so this deliberately does NOT
        filter by it; every configured course is bookable via a direct
        /book/<shortname> link already, so hiding one here would just make
        it harder to find, not actually more private."""
        banner = self._session_banner_html(environ)
        if not self.settings.courses:
            body = "<p>No courses are configured yet.</p>"
        else:
            cards = []
            for course in self.settings.courses:
                subtitle = _course_subtitle_html(course)
                desc_html = f'<div class="description">{course.description}</div>' if course.description else ""
                cards.append(
                    '<div class="course-card">'
                    f"<h2>{esc(course.title)}</h2>{subtitle}{desc_html}"
                    '<div class="submit-row">'
                    f'<a href="/book/{esc(course.shortname)}"><button type="button">View &amp; book</button></a>'
                    "</div></div>"
                )
            body = "".join(cards)
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page("Courses", body, banner=banner)

    # -- /book ---------------------------------------------------------------

    def book(self, method: str, shortname: str, environ):
        course = self.settings.course(shortname)
        if course is None:
            return "404 Not Found", [("Content-Type", "text/plain")], "unknown course"

        # Computed once per request and threaded through every response
        # this method (and its helpers _book_page/_book_with_guests) can
        # return -- see _session_banner_html's own docstring.
        banner = self._session_banner_html(environ)

        def capacity_lookup(sn, d):
            return self.store.count_confirmed(sn, d.isoformat())

        now = datetime.now(timezone.utc)
        occurrences = build_occurrences(
            course, self.settings, now, capacity_lookup, self._conflict_checker(exclude_own=True)
        )

        if method == "POST":
            form = self._read_form(environ)
            if form.get("agree") != "on":
                return self._book_page(course, occurrences, error="Please acknowledge the participation terms.", banner=banner)
            occ_date = form.get("occurrence_date", "")
            occ = {o.date.isoformat(): o for o in occurrences}.get(occ_date)
            if occ is None:
                return self._book_page(course, occurrences, error="That slot is no longer available.", banner=banner)
            email, name = form.get("email", "").strip(), form.get("name", "").strip()
            if not email or "@" not in email or not name:
                return self._book_page(course, occurrences, error="Please fill in your name and a valid email.", banner=banner)

            rejection = self._late_booking_rejection(occ, now)
            if rejection:
                return self._book_page(course, occurrences, error=rejection, banner=banner)

            guests, guest_error = self._parse_guest_entries(form, email)
            if guest_error:
                return self._book_page(course, occurrences, error=guest_error, banner=banner)

            # No password is ever collected here -- upsert_user_for_booking
            # only ever touches `name`, leaving any existing account's
            # password_hash (confirmed or still empty) completely alone.
            # This is what closes the old hole where re-submitting someone
            # else's email with a chosen PIN silently took over their
            # account: nothing reachable from this form can change another
            # email's credential anymore.
            user = self.store.upsert_user_for_booking(email, name)

            if guests:
                return self._book_with_guests(course, shortname, occ_date, user, guests, banner=banner)

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
                    "Booked!", f"<p>{msg} Check your email for confirmation and a cancel link.</p>", banner=banner
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
            resend_label = "Resend the confirmation email"
            body = (
                f"<p>Almost there -- we've emailed <b>{esc(email)}</b> a link to confirm your account.</p>"
                f"<p>Your spot for <b>{esc(course.title)}</b> on {esc(occ_date)} will only be reserved "
                "for you,<br>once you click the link in the email and set a password.</p>"
                '<div class="hint">Didn\'t get it? '
                '<form method="post" action="/my/reset" id="resend-form" style="display:inline">'
                f'<input type="hidden" name="email" value="{esc(email)}">'
                f'<button type="submit" id="resend-btn" class="link-button">{esc(resend_label)}</button>'
                '</form><span id="resend-status"></span>.</div>'
                + _resend_cooldown_inline_script("resend-form", "resend-btn", "resend-status", resend_label)
            )
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page("Almost there", body, banner=banner)

        return self._book_page(course, occurrences, banner=banner)

    def _booking_details_text(self, course, occ_date: str) -> str:
        """Thin wrapper around app.cancellation.booking_details_text (moved
        there 2026-07-06 so `my-bt cancel`, which has no App instance, can
        call the exact same logic) -- kept as an App method since every
        existing call site here already has `self`. See that function's
        docstring for what/why."""
        return booking_details_text(course, occ_date)

    def _send_cancellation_emails(
        self, course, occ_date: str, user, canceled_by: str, message: str
    ) -> None:
        """Thin wrapper around app.cancellation.send_cancellation_emails
        (moved there 2026-07-06 so `my-bt cancel` triggers IDENTICAL emails
        to the web admin's /admin/cancel, instead of reimplementing this)
        -- kept as an App method since every existing call site here
        already has `self.settings`. See that function's docstring for the
        full rationale (notify-both-sides, etc.)."""
        send_cancellation_emails(self.settings, course, occ_date, user, canceled_by, message)

    def _send_booking_result_guest_email(self, user, course, occ_date: str, status: str, cancel_token: str) -> None:
        """Just the guest-facing booked/waitlisted email (no admin copy) --
        factored out of _send_booking_result_email() so a guest-booking
        party (see _book_with_guests()) can send this exact same, unchanged
        wording to every party member individually while sending only ONE
        combined admin email for the whole party (see
        _send_party_admin_email()), instead of the admin getting one nearly
        identical email per person. Every guest gets an account (see
        my_confirm()), so this also invites them to /my rather than only
        handing them a one-shot cancel link.

        If this person has no password set yet (2026-07-06: true for every
        brand-new guest -- see Store.add_party_registrations_checking_capacity's
        docstring for why guest bookings deliberately skip the usual
        confirm-by-email gate, so a guest never gets THAT separate email --
        but also true for a solo party leader who happened to be new too),
        an OPTIONAL account-setup link is appended inline, reusing
        _confirm_url() rather than firing _send_confirm_email() as a
        second email: "OPTIONALLY allow them to create an account for
        them to access their space and set a password". Clicking it (or
        not) has no effect on the booking itself -- it's already
        confirmed/waitlisted regardless, unlike the STATUS_PENDING_CONFIRMATION
        gate a solo brand-new booking goes through."""
        cancel_url = f"{self.settings.base_url}/cancel/{cancel_token}"
        my_url = f"{self.settings.base_url}/my"
        details = self._booking_details_text(course, occ_date)
        account_line = ""
        if not user.password_hash:
            confirm_url = self._confirm_url(user)
            account_line = (
                f"Optional: set up a password to view/manage this from {my_url} anytime: "
                f"{confirm_url}\n"
            )
        if status == STATUS_WAITLISTED:
            send_mail(
                self.settings, user.email, f"Waitlisted: {course.title} on {occ_date}",
                "You're on the waitlist -- full for now, but you'll be confirmed automatically "
                "by email if a spot opens up:\n\n"
                f"{details}\n"
                f"Manage your bookings any time: {my_url}\n"
                f"Leave the waitlist directly: {cancel_url}\n"
                f"{account_line}",
            )
        else:
            send_mail(
                self.settings, user.email, f"Booking confirmed: {course.title} on {occ_date}",
                "Your spot is confirmed:\n\n"
                f"{details}\n"
                f"Manage your bookings any time: {my_url}\n"
                f"Cancel this booking directly: {cancel_url}\n"
                f"{account_line}",
            )

    def _send_booking_result_email(self, user, course, occ_date: str, status: str, cancel_token: str) -> None:
        """The guest-facing booked/waitlisted email + admin notification --
        shared by the instant-booking path above and by my_confirm()'s
        promotion of a newly-confirmed account's pending registrations, so
        the two paths can never drift out of sync in wording. Solo-booking
        path only (see _book_with_guests() for the party equivalent, which
        sends the same guest-facing email via
        _send_booking_result_guest_email() but consolidates the admin
        notification)."""
        self._send_booking_result_guest_email(user, course, occ_date, status, cancel_token)
        send_mail(
            self.settings, self.settings.admin_email,
            f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
            f"{user.name} <{user.email}> {'joined the waitlist for' if status == STATUS_WAITLISTED else 'booked'} "
            f"{course.title} on {occ_date}.",
        )

    def _parse_guest_entries(self, form: dict, leader_email: str) -> tuple[list[tuple[str, str]], str | None]:
        """Reads guest_email_0/guest_name_0 .. guest_email_{MAX_GUESTS-1}/
        guest_name_{MAX_GUESTS-1} off a submitted booking form (see
        _book_page()'s "+ Add participant" rows) -- name is optional per
        guest, email is not (see _book_with_guests() for how a blank name
        is resolved). Returns (entries, error): entries is a list of
        (email, name) pairs in the order submitted; error is a guest-facing
        message if validation failed (bad/duplicate email), in which case
        entries should be ignored and the form re-shown with that error."""
        entries: list[tuple[str, str]] = []
        seen = {leader_email.strip().lower()}
        for i in range(MAX_GUESTS):
            g_email = form.get(f"guest_email_{i}", "").strip()
            g_name = form.get(f"guest_name_{i}", "").strip()
            if not g_email:
                continue
            if "@" not in g_email:
                return [], f"Guest #{i + 1}'s email address doesn't look valid."
            key = g_email.lower()
            if key in seen:
                return [], (
                    f"{g_email} is listed more than once (or matches your own email) -- "
                    "please list each participant only once."
                )
            seen.add(key)
            entries.append((g_email, g_name))
        return entries, None

    def _send_party_admin_email(self, users: list, course, occ_date: str, status: str) -> None:
        """ONE admin email covering an entire guest-booking party (leader +
        every guest) -- see _send_booking_result_guest_email()'s docstring
        for why this is split out instead of reusing
        _send_booking_result_email()'s per-person admin email unchanged
        (that would mean one admin email per party member for what is, to
        the admin, a single booking event)."""
        leader, guests = users[0], users[1:]
        verb = "joined the waitlist for" if status == STATUS_WAITLISTED else "booked"
        if guests:
            guest_list = ", ".join(f"{u.name} <{u.email}>" for u in guests)
            who = f"{leader.name} <{leader.email}> (+ guest(s): {guest_list})"
        else:
            who = f"{leader.name} <{leader.email}>"
        send_mail(
            self.settings, self.settings.admin_email,
            f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
            f"{who} {verb} {course.title} on {occ_date} "
            f"(party of {len(users)}, all {'waitlisted' if status == STATUS_WAITLISTED else 'confirmed'} together).",
        )

    def _book_with_guests(
        self, course, shortname: str, occ_date: str, leader, guests: list[tuple[str, str]], banner: str = "",
    ):
        """The atomic party-booking path -- taken whenever the booking form
        included at least one "+ Add participant" guest (see book()).
        Unlike a solo booking, this runs regardless of whether `leader`
        already has a confirmed account: the whole party (leader + every
        guest) is admitted -- confirmed or waitlisted -- together, right
        now, never gated behind a brand-new guest's own email confirmation
        click (see Store.add_party_registrations_checking_capacity's
        docstring for the full reasoning, and SOLUTION-DESIGN.md's
        guest-booking entry for the tradeoff this accepts: the leader is
        trusted to vouch for who they add, same model SimplyMeet.me used).

        A guest's name is optional on the form -- if left blank, an
        already-existing account's real stored name is reused (never
        overwritten with a blank), and a genuinely brand-new guest with no
        name given falls back to the placeholder "Guest" rather than an
        empty string (User.name has no blank default)."""
        entries: list[tuple[str, str]] = []
        tokens: list[str] = []
        users = [leader]
        leader_token = new_token()
        entries.append((leader.user_id, hash_token(leader_token)))
        tokens.append(leader_token)
        for g_email, g_name in guests:
            existing = self.store.find_user_by_email(g_email)
            resolved_name = g_name or (existing.name if existing else "Guest")
            guest_user = self.store.upsert_user_for_booking(g_email, resolved_name)
            token = new_token()
            entries.append((guest_user.user_id, hash_token(token)))
            tokens.append(token)
            users.append(guest_user)

        regs = self.store.add_party_registrations_checking_capacity(
            shortname, occ_date, entries, course.capacity
        )
        self._sync(shortname, date.fromisoformat(occ_date))
        status = regs[0].status  # all-or-nothing -- every row shares the same status
        for member, token in zip(users, tokens):
            self._send_booking_result_guest_email(member, course, occ_date, status, token)
        self._send_party_admin_email(users, course, occ_date, status)

        party_size = len(users)
        if status == STATUS_WAITLISTED:
            msg = (
                f"Your party of {party_size} is on the waitlist together for "
                f"<b>{esc(course.title)}</b> on {esc(occ_date)} -- there wasn't room to confirm "
                f"all {party_size} of you at once. You'll all be confirmed together automatically "
                "if enough spots open up."
            )
        else:
            msg = f"Your party of {party_size} is booked for <b>{esc(course.title)}</b> on {esc(occ_date)}."
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
            "Booked!",
            f"<p>{msg}</p><p>Everyone in the party -- including you -- got their own email with "
            "a personal cancel link and an invite to manage their booking via /my. "
            "Canceling is always individual: if someone in the party cancels later, "
            "it only affects their own spot.</p>",
            banner=banner,
        )

    def _session_banner_html(self, environ) -> str:
        """A small "Logged in as x@example.org - Logout" banner for the
        dynamic pages a logged-in guest might reach while browsing/booking
        (2026-07-06: "/my should have a 'new booking' button... but with a
        banner showing them that they are logged in and with the ability
        to logout. same for any booking done from within /my") -- see
        courses() and book()/its helpers for where this gets passed to
        page(banner=...). Blank (no banner at all) for an anonymous
        visitor, since /book and /courses both work perfectly well without
        ever logging in -- this is purely a courtesy cue plus a quick way
        back to /my or to log out, for someone who arrived here already
        logged in. Also links back to the marketing homepage
        (settings.base_url, 2026-07-09, the operator: "allow in the banner to
        also go back to https://booking.example.org") -- this banner now shows on
        /my too (see my()), so without this, a guest on /my had no
        one-click way back to the homepage other than the separate
        target="_blank" link my() already has for that."""
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return ""
        user = self.store.find_user_by_id(session["user_id"])
        if user is None:
            return ""
        return (
            '<div class="session-banner">'
            f"<span>Logged in as <b>{esc(user.email)}</b></span>"
            '<span><a href="/my">My bookings</a> &middot; '
            f'<a href="{esc(self.settings.base_url)}">{esc(self._site_label())}</a> &middot; '
            '<form method="post" action="/my/logout">'
            '<button type="submit" class="link-button">Log out</button></form></span>'
            "</div>"
        )

    def _site_label(self) -> str:
        """A short, human name for this deployment to put in guest-facing
        emails that otherwise have no other context (e.g. the account
        confirmation email below) -- derived from settings.base_url's
        hostname (e.g. "https://booking.example.org" -> "booking.example.org") rather than a
        separate settings.toml key, since base_url already has to be
        correct and unique per deployment anyway. Falls back to the raw
        base_url on the rare malformed/missing-hostname case rather than
        showing nothing."""
        return urlparse(self.settings.base_url).hostname or self.settings.base_url

    def _confirm_url(self, user) -> str:
        """(Re)generates a confirm-or-reset token for `user` (see
        storage.User.confirm_token_hash) and returns the /my/confirm/<token>
        URL for it -- WITHOUT sending anything. Factored out of
        _send_confirm_email() (2026-07-06) so a guest-booking email can
        embed an "optional: set up your account" link inline (see
        _send_booking_result_guest_email()) instead of firing a second,
        separate email at someone who's already getting one for the
        booking itself. Regenerating unconditionally on every call is
        deliberate: simpler than trying to detect/reuse an already-
        outstanding token, and it invalidates any earlier link, which is
        exactly the right behavior for a "resend" anyway."""
        token = new_token()
        self.store.set_confirm_token(user.user_id, hash_token(token), now_iso())
        return f"{self.settings.base_url}/my/confirm/{token}"

    def _send_confirm_email(self, user) -> None:
        """Emails the "set your password" link on its own -- called from a
        booking under a not-yet-confirmed email, and from /my/reset's
        unified resend/forgot-password flow.

        Names the site in the subject (see _site_label()) -- without it
        this email is just "Confirm your account" with no indication of
        what account, for what, or from whom, which is confusing on its
        own out of context (caught in practice 2026-07-05: "explain in the
        email an account for WHAT ?!"). The BODY text spells out the full
        https://... base_url instead (2026-07-07, the operator: "please write
        rather https://booking.example.org in the text") -- a subject line reads
        oddly with a URL scheme in it, but the body sentence doesn't.
        Phrased as "...booking account on {url}..." rather than "...your
        {url} booking account..." (2026-07-08, the operator's own exact requested
        wording) -- reads more naturally with a full URL sitting mid-sentence.

        Also states the link's expiry (CONFIRM_TOKEN_TTL_HOURS -- see
        my_confirm()) and that any older link is now void: this call
        always supersedes whatever confirm/reset link was outstanding
        before it (see Store.set_confirm_token), so a guest who requested
        twice needs to know the first email's link won't work anymore and
        why (2026-07-07, the operator: "a new email should invalidate the pending
        link ... [and] inform the user that there should be a NEW link
        coming to him")."""
        confirm_url = self._confirm_url(user)
        first_time = not user.password_hash
        site = self._site_label()
        site_url = self.settings.base_url
        subject = f"Confirm your {site} account" if first_time else f"Reset your {site} password"
        verb = (f"confirm your booking account on {site_url} and set a password"
                if first_time else f"set a new password for your booking account on {site_url}")
        send_mail(
            self.settings, user.email, subject,
            f"Click below to {verb}:\n\n{confirm_url}\n\n"
            f"This link expires in {CONFIRM_TOKEN_TTL_HOURS} hours. If you requested this more "
            "than once, only the link in this latest email will work -- any earlier one is "
            "no longer valid.\n\n"
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

    def _book_page(self, course, occurrences, error: str | None = None, banner: str = ""):
        subtitle = _course_subtitle_html(course)
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
            body = subtitle + desc_html + "<p>No dates currently available, please check back next week.</p>"
        else:
            date_buttons = "".join(
                '<label class="date-btn">'
                f'<input type="radio" name="occurrence_date" value="{esc(o.date.isoformat())}" '
                f'data-date="{esc(o.date.isoformat())}" data-full="{"1" if o.is_full else "0"}" '
                # data-spots-left is the TRUE remaining count, deliberately
                # separate from _spots_left_text()'s possibly
                # spots_left_offset-adjusted display text -- the "adding a
                # guest may waitlist your whole party" warning (see the
                # guest-rows script below) has to reason from reality, not
                # the display-only urgency number (see _spots_left_text's
                # own docstring for why that number must never drive an
                # actual admission decision).
                f'data-spots-left="{o.spots_left}"'
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
              <label>Dates available
                <div class="dates" role="radiogroup" aria-label="Dates available">{date_buttons}</div>
              </label>
              <div class="selected-box">Selected date: <strong id="selected-date-text">{esc(occurrences[0].date.isoformat())}</strong></div>
              <label>Your name <span class="req">(required)</span>
                <input class="big-input" name="name" required></label>
              <label>Your email <span class="req">(required)</span>
                <input class="big-input" name="email" type="email" required></label>
              <p class="hint">First time booking with this email? We'll send a link to confirm your
                account and set a password.</p>
              <div class="guests-section">
                <div id="guest-rows"></div>
                <button type="button" id="add-guest-btn" class="link-button">+ Add participant</button>
                <p id="party-warning" class="note" style="display:none"></p>
              </div>
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

              // Guest rows ("+ Add participant", mirroring SimplyMeet.me's
              // own UX) -- each row's fields are named guest_email_<n>/
              // guest_name_<n> (never repeated -- see app/webapp.py::
              // _parse_guest_entries(); form-encoded POST bodies here only
              // ever keep the FIRST value of a repeated name, so distinct
              // per-row names are required, not just a style choice).
              var guestRowsEl = document.getElementById("guest-rows");
              var addGuestBtn = document.getElementById("add-guest-btn");
              var partyWarning = document.getElementById("party-warning");
              var guestIndex = 0;
              var MAX_GUESTS = {MAX_GUESTS};

              function guestRowCount() {{ return guestRowsEl ? guestRowsEl.children.length : 0; }}

              function updatePartyWarning() {{
                var r = currentRadio();
                if (!r || !partyWarning) return;
                var spotsLeft = parseInt(r.dataset.spotsLeft, 10);
                var partySize = 1 + guestRowCount();
                if (r.dataset.full !== "1" && !isNaN(spotsLeft) && partySize > spotsLeft) {{
                  var spotWord = spotsLeft === 1 ? "spot" : "spots";
                  var peopleWord = partySize === 1 ? "person" : "people";
                  partyWarning.textContent = "Only " + spotsLeft + " confirmed " + spotWord +
                    " left for this session, but your party is " + partySize + " " + peopleWord +
                    ". Submitting will place your whole group on the waitlist together -- " +
                    "remove a guest above to get confirmed instantly instead.";
                  partyWarning.style.display = "";
                }} else {{
                  partyWarning.style.display = "none";
                }}
              }}

              function addGuestRow() {{
                if (!guestRowsEl || guestRowCount() >= MAX_GUESTS) return;
                var i = guestIndex++;
                var row = document.createElement("div");
                row.className = "guest-row";
                row.innerHTML =
                  '<label>Guest email <span class="req">(required)</span>' +
                  '<input class="big-input" name="guest_email_' + i + '" type="email" required></label>' +
                  '<label>Guest name <span class="opt">(optional)</span>' +
                  '<input class="big-input" name="guest_name_' + i + '"></label>' +
                  '<button type="button" class="link-button remove-guest-btn">Remove participant</button>';
                guestRowsEl.appendChild(row);
                row.querySelector(".remove-guest-btn").addEventListener("click", function() {{
                  guestRowsEl.removeChild(row);
                  if (addGuestBtn) addGuestBtn.style.display = "";
                  updatePartyWarning();
                  refresh();
                }});
                row.querySelector('[name="guest_email_' + i + '"]').addEventListener("input", updatePartyWarning);
                if (guestRowCount() >= MAX_GUESTS && addGuestBtn) addGuestBtn.style.display = "none";
                updatePartyWarning();
                refresh();
              }}
              if (addGuestBtn) addGuestBtn.addEventListener("click", addGuestRow);

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
                updatePartyWarning();
              }}
              for (var i = 0; i < radios.length; i++) {{ radios[i].addEventListener("change", refresh); }}
              [nameEl, emailEl, agreeEl].forEach(function(el) {{
                el.addEventListener("input", refresh);
                el.addEventListener("change", refresh);
              }});
              refresh();
            }})();
            </script>"""
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(course.title, body, banner=banner)

    # -- /cancel/<token> (guest, from email) ---------------------------------

    def guest_cancel(self, method: str, token: str, environ):
        reg = self.store.find_by_guest_token_hash(hash_token(token))
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page("Not found", "<p>This link is invalid or already used.</p>")
        course = self.settings.course(reg.course_shortname)
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            self.store.cancel(reg.registration_id, canceled_by="guest", host_message=message)
            self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
            if course:
                user = self.store.find_user_by_id(reg.user_id)
                # Both sides notified, same as every other cancellation path
                # (see _send_cancellation_emails) -- this guest already knows
                # they just did this, but the email is still their own copy
                # of "here's what got canceled and when", same as the other
                # two paths get.
                self._send_cancellation_emails(course, reg.occurrence_date, user, canceled_by="guest", message=message)
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Your booking has been canceled.</p>")
        body = f"""<p>Cancel your booking for <b>{esc(course.title)}</b> on {esc(reg.occurrence_date)}?</p>
        <form method="post" class="card">
          <label>Optional reason <textarea name="message" rows="2" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Yes, cancel it</button></div>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Cancel booking", body)

    # -- /my (guest self-service) --------------------------------------------

    def my(self, method: str, environ):
        """Note (2026-07-06): upcoming bookings are always shown in full,
        sorted soonest-first; past bookings are sorted most-recent-first
        and capped at MY_PAST_BOOKINGS_LIMIT -- "should always show the
        past 3 courses they scheduled" -- shown as a separate table so the
        cap is obvious rather than looking like an arbitrary cutoff on one
        mixed list. A "New booking" button links to /courses (see
        courses()) rather than back to any specific course, since this
        guest may want a course they haven't booked before.

        (2026-07-09, the operator: "What about PAST meetings?", asked while
        looking at an account with zero bookings of either kind) Past now
        always shows its own "You have no past bookings." message when
        empty, exactly like Upcoming already did -- omitting the whole
        section when empty (the original behavior) looked indistinguishable
        from the section being missing/broken rather than genuinely empty.

        Also shows the same _session_banner_html() banner /courses and
        /book already show (2026-07-09, the operator: "Rather use the BANNER as
        here to be CONSISTENT!!") instead of a separate, redundant "Log
        out" button in the bottom row -- the banner's own Logout covers
        that; only "Delete my account & data" (a distinct, destructive
        action) remains in that row on its own. The top row's own
        separate "Visit booking.example.org (opens in a new tab)" link is gone too
        now (2026-07-09, the operator: "Now we can get rid of the ugly green
        sentence behind New bookings as we have https://booking.example.org in the
        top-bar") -- the banner's own homepage link (see
        _session_banner_html) covers it, in the same tab, alongside "My
        bookings" and "Log out"."""
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            all_regs = self.store.registrations_for_user(session["user_id"])
            today = datetime.now(timezone.utc).date()
            upcoming = sorted(
                (r for r in all_regs if date.fromisoformat(r.occurrence_date) >= today),
                key=lambda r: r.occurrence_date,
            )
            past = sorted(
                (r for r in all_regs if date.fromisoformat(r.occurrence_date) < today),
                key=lambda r: r.occurrence_date, reverse=True,
            )[:MY_PAST_BOOKINGS_LIMIT]

            def _row(r):
                # course_shortname is the CSV's own key -- an internal
                # identifier, not something a guest should have to read.
                # Translate it to the human-facing title/time/location for
                # display; None only if the course was removed from
                # settings.toml since booking (old row, nothing to look up
                # anymore) -- fall back to the shortname rather than
                # showing a blank so the row is still identifiable.
                course = self.settings.course(r.course_shortname)
                title = course.title if course else r.course_shortname
                time_range = course.time_range_label() if course else ""
                location = course.location if course else ""
                cancel_id = f"cancel-{esc(r.registration_id)}"
                # Confirmed or waitlisted are the only cancelable states --
                # this used to only allow CONFIRMED, which silently made it
                # impossible to leave the waitlist from this page (the
                # emailed cancel link and /admin could always do both;
                # caught 2026-07-05 while touching this code for the
                # cancel-dialog/both-sides-notification consistency pass).
                disabled = r.status not in (STATUS_CONFIRMED, STATUS_WAITLISTED)
                # A <dialog> (real pop-up) asking for an optional reason,
                # opened by intercepting the Cancel button's click in JS
                # below -- progressive enhancement: without JS (or on a
                # browser predating <dialog>/showModal), the button is a
                # plain type="submit" and cancels immediately with no
                # reason, exactly like before this feature existed.
                actions = (
                    f'<form method="post" action="/my/cancel/{esc(r.registration_id)}" id="{cancel_id}-form">'
                    f'<button type="submit" class="confirm-dialog-btn" data-dialog="{cancel_id}-dialog" '
                    f'{"disabled" if disabled else ""}>Cancel</button>'
                    "</form>"
                    f'<dialog id="{cancel_id}-dialog" class="card">'
                    f"<p><b>Are you sure?</b></p>"
                    f"<p>Cancel your booking for <b>{esc(title)}</b> on {esc(r.occurrence_date)}?</p>"
                    f'<label>Optional reason <textarea name="message" rows="2" class="big-input" '
                    f'form="{cancel_id}-form"></textarea></label>'
                    '<div class="submit-row">'
                    f'<button type="submit" form="{cancel_id}-form">Confirm cancellation</button> '
                    f'<button type="button" class="dialog-close-btn" data-dialog="{cancel_id}-dialog">Never mind</button>'
                    "</div></dialog>"
                )
                return (
                    f"<tr><td>{esc(title)}</td><td>{esc(r.occurrence_date)}</td>"
                    f"<td>{esc(time_range)}</td><td>{esc(location)}</td>"
                    f"<td>{esc(r.status)}</td>"
                    f"<td>{actions}</td></tr>"
                )

            def _table(table_id: str, regs_for_table: list) -> str:
                if not regs_for_table:
                    return ""
                rows = "".join(_row(r) for r in regs_for_table)
                return f"""
                <div class="table-tools">
                  <input type="search" id="{table_id}-filter" class="big-input" placeholder="Filter bookings...">
                </div>
                <table id="{table_id}" border="1" cellpadding="6">
                  <thead><tr>
                    <th>Course<span class="sort-indicator"></span></th>
                    <th>Date<span class="sort-indicator"></span></th>
                    <th>Time<span class="sort-indicator"></span></th>
                    <th>Location<span class="sort-indicator"></span></th>
                    <th>Status<span class="sort-indicator"></span></th>
                    <th>Actions<span class="sort-indicator"></span></th>
                  </tr></thead>
                  <tbody>{rows}</tbody>
                </table>""" + _sortable_filterable_table_script(table_id)

            upcoming_id, past_id = "my-upcoming-table", "my-past-table"
            upcoming_html = _table(upcoming_id, upcoming) or "<p>You have no upcoming bookings.</p>"
            past_html = _table(past_id, past) or "<p>You have no past bookings.</p>"
            body = f"""
            <div class="submit-row">
              <a href="/courses"><button type="button">New booking</button></a>
            </div>
            <h3>Upcoming</h3>
            {upcoming_html}
            <h3>Past (most recent {MY_PAST_BOOKINGS_LIMIT})</h3>
            {past_html}
            <div class="submit-row">
              <form method="post" action="/my/delete-account" style="display:inline" id="delete-account-form"
                onsubmit="return confirm('Delete your account and all related data? This will cancel any booking you still have!');">
                <button type="submit" class="confirm-dialog-btn" data-dialog="delete-account-dialog">Delete my account &amp; data</button>
              </form>
            </div>
            <dialog id="delete-account-dialog" class="card">
              <p><b>Are you sure?</b></p>
              <p>Delete your account and all related data? This will cancel any booking you still have!</p>
              <div class="submit-row">
                <button type="submit" form="delete-account-form">Yes, delete everything</button>
                <button type="button" class="dialog-close-btn" data-dialog="delete-account-dialog">Never mind</button>
              </div>
            </dialog>""" + _DIALOG_WIRING_SCRIPT
            return (
                "200 OK", [("Content-Type", "text/html")],
                page("My bookings", body, banner=self._session_banner_html(environ)),
            )

        error = None
        lockout_seconds = 0.0
        if method == "POST":
            form = self._read_form(environ)
            email, password = form.get("email", "").strip(), form.get("password", "").strip()
            now = time.time()
            if email.lower() == "admin":
                # 2026-07-06: "/my should accept email: admin and the admin
                # password in order to login to /admin space" -- reuses the
                # exact same rate-limiter KEY as admin_login() (per client
                # IP, not per-email/per-string) so this can't be used to
                # dodge that lockout by switching entry points, and so
                # hammering "admin" here can't lock the real admin out of
                # /admin/login either -- one shared bucket either way in.
                key = f"admin:{_client_ip(environ)}"
                if not login_limiter.allow(key, now=now):
                    error = "Too many attempts -- try again later."
                    lockout_seconds = login_limiter.retry_after(key, now=now)
                    log.warning("rate limit blocked: admin login from %s (via /my)", _client_ip(environ))
                elif verify_admin_password(password, self.settings.admin_password_hash):
                    sid = _new_session({"kind": "admin"})
                    return "302 Found", [("Location", "/admin"), ("Set-Cookie", _session_cookie_header(sid))], ""
                else:
                    # Same generic wording as a guest mismatch below --
                    # never confirms that "admin" is treated specially.
                    error = "Email and/or password did not match."
            else:
                key = f"guest:{email.lower()}"
                if not login_limiter.allow(key, now=now):
                    error = "Too many attempts -- try again later."
                    lockout_seconds = login_limiter.retry_after(key, now=now)
                    # WARNING (not DEBUG): a real signal the watchdog counts
                    # (see app/watchdog.py) -- masked, never the raw email.
                    log.warning("rate limit blocked: guest login for %s", _masked(email))
                else:
                    user = self.store.find_user_by_email(email)
                    # user.password_hash is empty for a not-yet-confirmed
                    # account -- bail out before verify_secret rather than
                    # feeding it an empty hash/salt.
                    if user and user.password_hash and verify_secret(password, user.password_hash, user.password_salt):
                        sid = _new_session({"kind": "guest", "user_id": user.user_id})
                        self.store.touch_login(user.user_id)
                        return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header(sid))], ""
                    error = "Email and/or password did not match."
        return self._my_login_page(login_error=error, login_lockout_seconds=lockout_seconds)

    def my_signup(self, method: str, environ):
        """POST target for /my's "Sign up" tab (2026-07-06, the operator: "let's
        also have a 'Sign up' possibility"). Creates a brand-new account
        (with the given name) if this email doesn't have one yet, then
        ALWAYS sends a confirm-or-reset link and shows the exact same
        generic success message either way -- entering an email that
        already has an account behaves exactly like /my/reset's "forgot
        password" (same rate-limiter KEYS too: reset:<email>/reset-ip:<ip>,
        deliberately shared with my_reset() since both endpoints end up
        doing the same thing -- create/confirm an account and email a
        token -- so they must share one lockout budget or an attacker
        could dodge one by hitting the other), rather than silently
        overwriting that existing account's real name with whatever was
        just typed into this form."""
        if method != "POST":
            return "302 Found", [("Location", "/my")], ""
        form = self._read_form(environ)
        name, email = form.get("name", "").strip(), form.get("email", "").strip()
        if not email or "@" not in email or not name:
            return self._my_login_page(
                signup_error="Please fill in your name and a valid email.", active_tab="signup"
            )
        now = time.time()
        email_key = f"reset:{email.lower()}"
        ip_key = f"reset-ip:{_client_ip(environ)}"
        email_ok = login_limiter.allow(email_key, now=now)
        ip_ok = reset_ip_limiter.allow(ip_key, now=now)
        if not (email_ok and ip_ok):
            log.warning("rate limit blocked: sign up for %s", _masked(email))
            lockout_seconds = max(
                0.0 if email_ok else login_limiter.retry_after(email_key, now=now),
                0.0 if ip_ok else reset_ip_limiter.retry_after(ip_key, now=now),
            )
            return self._my_login_page(
                signup_error="Too many attempts -- try again later.",
                signup_lockout_seconds=lockout_seconds, active_tab="signup",
            )
        existing = self.store.find_user_by_email(email)
        user = existing if existing else self.store.upsert_user_for_booking(email, name)
        self._send_confirm_email(user)
        return self._my_login_page(
            signup_success="Check your email for a link to finish setting up your account.",
            active_tab="signup",
        )

    def _my_login_page(
        self,
        *,
        login_error: str | None = None,
        login_lockout_seconds: float = 0.0,
        signup_error: str | None = None,
        signup_success: str | None = None,
        signup_lockout_seconds: float = 0.0,
        active_tab: str = "login",
    ):
        """Renders /my's logged-out page: two CSS-only tabs (radio buttons
        + sibling selectors -- no JS needed to switch between them)
        labeled "Login" (default) and "Sign up" (2026-07-06). Both my()
        (GET/POST login) and my_signup() (POST) render through this one
        function so the two tabs' markup can't drift apart, and so a
        failed submission re-opens on the SAME tab the guest was using
        (via active_tab) instead of silently flipping back to Login."""
        login_checked = "checked" if active_tab == "login" else ""
        signup_checked = "checked" if active_tab == "signup" else ""

        login_err_html = f'<p class="err">{esc(login_error)}</p>' if login_error else ""
        login_label = "Login"
        login_body = f"""{login_err_html}<form method="post" action="/my" class="card">
          <label>Email <input class="big-input" name="email" type="text" required></label>
          <label>Password <input class="big-input" name="password" type="password" required></label>
          <div class="submit-row"><button type="submit" id="my-login-btn">{esc(login_label)}</button></div>
        </form>
        <p><a href="/my/reset">Forgot your password, or still need to confirm your account?</a></p>"""
        if login_lockout_seconds:
            login_body += _lockout_countdown_script(login_lockout_seconds, "my-login-btn", login_label)

        if signup_success:
            # Boxed the same as the form it replaces (2026-07-06 fix: a
            # bare, unboxed <p> here looked like a stray sentence floating
            # on an otherwise-empty page -- the operator: "This is a bit ugly").
            signup_body = f'<div class="card"><p>{esc(signup_success)}</p></div>'
        else:
            signup_err_html = f'<p class="err">{esc(signup_error)}</p>' if signup_error else ""
            signup_label = "Sign up"
            # The "we'll email you a link" hint lives INSIDE the card, right
            # after the fields it explains -- same placement convention as
            # book()'s own "First time booking with this email?" hint --
            # rather than dangling below the closed form (2026-07-06 fix,
            # same complaint as the success message above).
            signup_body = f"""{signup_err_html}<form method="post" action="/my/signup" class="card">
              <label>Name <input class="big-input" name="name" type="text" required></label>
              <label>Email <input class="big-input" name="email" type="email" required></label>
              <p class="hint">We'll email you a link to set your password.</p>
              <div class="submit-row"><button type="submit" id="my-signup-btn">{esc(signup_label)}</button></div>
            </form>"""
            if signup_lockout_seconds:
                signup_body += _lockout_countdown_script(signup_lockout_seconds, "my-signup-btn", signup_label)

        body = f"""
        <div class="tabs">
          <input type="radio" id="my-tab-login" name="my-tab" class="tab-radio" {login_checked}>
          <input type="radio" id="my-tab-signup" name="my-tab" class="tab-radio" {signup_checked}>
          <div class="tab-labels">
            <label for="my-tab-login" class="tab-label">Login</label>
            <label for="my-tab-signup" class="tab-label">Sign up</label>
          </div>
          <div class="tab-panel" id="my-panel-login">{login_body}</div>
          <div class="tab-panel" id="my-panel-signup">{signup_body}</div>
        </div>"""
        return "200 OK", [("Content-Type", "text/html")], page("My bookings", body)

    def my_reset(self, method: str, environ):
        """Unified "forgot password" + "resend confirmation" flow -- both
        reduce to the same thing (email a link to /my/confirm/<token>), so
        one form/route covers both instead of two near-duplicates. The
        SUCCESS response is always the exact same regardless of whether
        the email is registered, confirmed, or unknown -- this endpoint
        must never let an attacker probing many different addresses tell
        which ones exist.

        A rate-limited response, on the other hand, is safe to show
        distinctly (2026-07-05, prompted by the operator asking why not): both
        login_limiter (per email) and reset_ip_limiter (per client IP,
        see its own comment) are checked and incremented BEFORE
        find_user_by_email is ever consulted, so whether either one
        blocks a given request never depends on whether the submitted
        email actually has an account -- it only depends on how many
        times that exact string, or that IP, has hit this endpoint
        recently. Showing "please wait" instead of silently pretending to
        send (the old behavior) is also just more honest to a real guest
        who's clicked the button more than once while waiting for the
        email to arrive.
        """
        lockout_seconds = 0.0
        if method == "POST":
            form = self._read_form(environ)
            email = form.get("email", "").strip()
            now = time.time()
            email_key = f"reset:{email.lower()}"
            ip_key = f"reset-ip:{_client_ip(environ)}"
            email_ok = login_limiter.allow(email_key, now=now)
            ip_ok = reset_ip_limiter.allow(ip_key, now=now)
            if email_ok and ip_ok:
                user = self.store.find_user_by_email(email)
                if user:
                    self._send_confirm_email(user)
                body = (
                    "<p>If that email has an account with us, we've just sent a link to set/reset your password.</p>"
                    '<p><a href="/my">Back to login</a></p>'
                )
                return "200 OK", [("Content-Type", "text/html")], page("Check your email", body)
            log.warning("rate limit blocked: password reset for %s", _masked(email))
            lockout_seconds = max(
                0.0 if email_ok else login_limiter.retry_after(email_key, now=now),
                0.0 if ip_ok else reset_ip_limiter.retry_after(ip_key, now=now),
            )
        reset_label = "Send me a link"
        err_html = '<p class="err">Too many attempts -- try again later.</p>' if lockout_seconds else ""
        body = f"""{err_html}<form method="post" class="card" id="reset-form">
          <label>Email <input class="big-input" name="email" type="email" required></label>
          <div class="submit-row"><button type="submit" id="reset-btn">{esc(reset_label)}</button></div>
        </form>""" + _resend_cooldown_script("reset-btn", reset_label)
        if lockout_seconds:
            body += _lockout_countdown_script(lockout_seconds, "reset-btn", reset_label)
        return "200 OK", [("Content-Type", "text/html")], page("Forgot your password?", body)

    def _set_password_form(self, token: str) -> str:
        return f"""<form method="post" class="card">
          <label>New password <span class="req">(required)</span>
            <input class="big-input" name="password" type="password"
              minlength="{MIN_PASSWORD_LENGTH}" required></label>
          <p class="hint">At least {MIN_PASSWORD_LENGTH} characters. Any letters, numbers, or
            symbols are allowed, and there's no upper limit -- just avoid leading/trailing
            spaces, which are trimmed off.</p>
          <div class="submit-row"><button type="submit">Set password</button></div>
        </form>"""

    def _confirm_token_expired(self, user) -> bool:
        """2026-07-07 -- see CONFIRM_TOKEN_TTL_HOURS. `user` must already be
        the result of a successful find_user_by_confirm_token_hash lookup
        (i.e. confirm_token_created_at was set by the matching
        set_confirm_token call); an empty value is treated as NOT expired
        rather than raising, purely defensively."""
        if not user.confirm_token_created_at:
            return False
        created = datetime.fromisoformat(user.confirm_token_created_at)
        return datetime.now(timezone.utc) - created > timedelta(hours=CONFIRM_TOKEN_TTL_HOURS)

    def my_confirm(self, method: str, token: str, environ):
        """Landing page for BOTH the first-time account-confirmation link
        and a later password-reset link -- see storage.User.confirm_token_hash.
        Setting a password here also promotes every STATUS_PENDING_CONFIRMATION
        registration for this user, re-checking capacity fresh at this exact
        moment (it may have filled up while the account sat unconfirmed) --
        see Store.confirm_pending_registration.

        Three distinct "this won't work" outcomes (2026-07-07, the operator asked
        "when will the confirmation links be invalid?" and "clicking the
        invalidated link should inform the user that there should be a NEW
        link coming to him"), checked in this order:
          1. Found + past CONFIRM_TOKEN_TTL_HOURS -> "expired" message.
          2. Not found, but matches prev_confirm_token_hash -> "a newer
             link was already sent to you" message (this is the common
             case when a guest double-submits Sign up/reset).
          3. Not found anywhere -> generic "invalid or already used"
             message (garbage token, or already consumed to set a
             password)."""
        token_hash = hash_token(token)
        user = self.store.find_user_by_confirm_token_hash(token_hash)
        if user is not None and self._confirm_token_expired(user):
            body = (f'<p>This link has expired -- confirmation links are only valid for '
                     f'{CONFIRM_TOKEN_TTL_HOURS} hours. '
                     '<a href="/my/reset">Request a new one</a>.</p>')
            return "200 OK", [("Content-Type", "text/html")], page("Link expired", body)
        if user is None:
            superseded = self.store.find_user_by_prev_confirm_token_hash(token_hash)
            if superseded is not None:
                body = ('<p>This link has been disabled because a newer link was already '
                        'sent to you -- check your inbox for the latest email.</p>')
                return "200 OK", [("Content-Type", "text/html")], page("Link replaced", body)
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
        # Shown so the guest can confirm this link/page is actually theirs
        # before typing a password -- not deliberately hidden before, just
        # missing (the token in the URL gives no visual confirmation of
        # whose account it is).
        email_note = f"<p>Setting a password for <b>{esc(user.email)}</b>.</p>"

        if method == "POST":
            form = self._read_form(environ)
            password = form.get("password", "").strip()
            if len(password) < MIN_PASSWORD_LENGTH:
                err = f'<p class="err">Please choose a password at least {MIN_PASSWORD_LENGTH} characters long.</p>'
                return "200 OK", [("Content-Type", "text/html")], page(
                    "Set your password", err + email_note + pending_note + self._set_password_form(token)
                )
            pw_hash, pw_salt = hash_secret(password)
            self.store.set_password(user.user_id, pw_hash, pw_salt)
            # Keep this in-memory `user` object in sync with the store --
            # it's reused below by _send_booking_result_email() (via
            # _send_booking_result_guest_email()), which now checks
            # user.password_hash to decide whether to append an
            # account-setup link (2026-07-06); without this, that check
            # would still see the pre-password-set empty value and offer
            # a redundant "set up your account" link to someone who just
            # set their password seconds ago.
            user.password_hash, user.password_salt = pw_hash, pw_salt

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
                # Status suffix only for the non-obvious outcome (waitlisted)
                # -- "did succeed" below already says what confirmed means,
                # no need to repeat it on every line too.
                status_suffix = "" if updated.status == STATUS_CONFIRMED else f" -- {updated.status}"
                confirmed_lines.append(
                    f"{course.title} on {reg.occurrence_date} at {course.time_range_label()} "
                    f"({course.location}){status_suffix}"
                )

            sid = _new_session({"kind": "guest", "user_id": user.user_id})
            self.store.touch_login(user.user_id)
            if confirmed_lines:
                plural = "s" if len(confirmed_lines) != 1 else ""
                summary = (
                    f"<p>Your course booking{plural} did succeed for:</p><ul>"
                    + "".join(f"<li>{esc(line)}</li>" for line in confirmed_lines) + "</ul>"
                )
            else:
                summary = ""
            body = (
                "<p>Your password is set and your account is now active.</p>"
                f"{summary}<p><a href=\"/my\">View my bookings</a></p>"
            )
            return (
                "200 OK",
                [("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", _session_cookie_header(sid))],
                page("Account & booking confirmed!", body),
            )

        return "200 OK", [("Content-Type", "text/html")], page(
            "Set your password", email_note + pending_note + self._set_password_form(token)
        )

    def my_cancel(self, method: str, registration_id: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return "403 Forbidden", [("Content-Type", "text/plain")], "log in first"
        reg = self.store.find_by_id(registration_id)
        if reg and reg.user_id == session["user_id"]:
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            self.store.cancel(registration_id, canceled_by="guest", host_message=message)
            self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
            course = self.settings.course(reg.course_shortname)
            if course:
                user = self.store.find_user_by_id(session["user_id"])
                # Both sides notified, always -- see _send_cancellation_emails
                # (standing default now, SOLUTION-DESIGN.md). This is what
                # lets the real account owner notice a cancellation made by
                # someone who got into their /my session but isn't them.
                self._send_cancellation_emails(course, reg.occurrence_date, user, canceled_by="guest", message=message)
        return "302 Found", [("Location", "/my")], ""

    def my_logout(self, method: str, environ):
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            SESSIONS.pop(session["_sid"], None)
        return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header("", clear=True))], ""

    def my_delete_account(self, method: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return "403 Forbidden", [("Content-Type", "text/plain")], "log in first"
        user = self.store.find_user_by_id(session["user_id"])
        if user:
            # erase_user_by_email now runs the same promote+calendar-sync
            # (app.cancel_flow.cancel_and_promote) for each future
            # confirmed/waitlisted booking it force-cancels, given a caldav
            # client -- passing self.caldav here is what used to be this
            # method's own separate loop (pre-computing future_regs, then
            # calling self._cancel_and_promote() per row after erasing).
            # That loop is gone: erasure.py does it internally now, so the
            # web `/my` self-erasure path and `my-bt erase` (scripts/my-bt)
            # can never drift apart on this.
            erase_user_by_email(self.store, self.settings, user.email, caldav=self.caldav)
        SESSIONS.pop(session["_sid"], None)
        return "302 Found", [("Location", "/my"), ("Set-Cookie", _session_cookie_header("", clear=True))], ""

    def my_session_status(self, method: str, environ):
        """GET-only JSON: {"logged_in": bool, "email": str|null} for
        whatever guest session (if any) this request's cookie belongs to.
        Never reports an admin session as logged in here -- this exists
        purely so the STATIC homepage (site/index.html, served directly by
        nginx, not proxied through this app -- see README's "Login banner"
        section) can ask its own small question, "is this visitor's
        browser already logged in as a guest?", via a same-origin fetch()
        and swap its plain Login button for the same My-bookings/Log-out
        affordance the dynamic pages (/courses, /book, /my) already show
        (2026-07-09, the operator: "if you are logged in, https://booking.example.org
        should show the same banner and not 'Login' button").

        A same-origin browser request already carries the HttpOnly session
        cookie automatically even though the static page's own JS can
        never read it directly via document.cookie -- this endpoint is the
        intentional, narrow exception that lets that JS learn ONLY
        "yes/no, and which email," nothing else, and only for the browser
        that already holds a valid session of its own (no cross-user
        leakage). Under `/my`'s existing nginx prefix match, so (like
        /my/signup etc.) no nginx config change was needed.

        Important limitation, carried over from the homepage's own
        existing comment on why it had NO session-awareness before this:
        this page is also embedded via <iframe> on a separate "center
        homepage." A same-origin fetch from WITHIN that iframe is a
        third-party request relative to the embedding page's origin, and
        modern browsers increasingly partition or block third-party
        cookies by default -- so this endpoint (and the session cookie
        itself) may simply not see the guest's login there, regardless of
        whether they're actually logged in in the top-level tab. That's
        not a bug in this endpoint; it's the same constraint the homepage
        comment already documented. The static page's JS treats a failed
        or negative check as "stay on the plain Login button" (the exact
        prior behavior), so this is a pure enhancement for a direct/
        standalone visit, never a regression for the iframe-embedded one."""
        if method != "GET":
            return "405 Method Not Allowed", [("Content-Type", "text/plain")], "GET only"
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            user = self.store.find_user_by_id(session["user_id"])
        else:
            user = None
        payload = {"logged_in": user is not None, "email": user.email if user else None}
        return "200 OK", [("Content-Type", "application/json")], json.dumps(payload)

    # -- /admin ---------------------------------------------------------------

    def admin_login(self, method: str, environ):
        error = None
        lockout_seconds = 0.0
        if method == "POST":
            form = self._read_form(environ)
            password = form.get("password", "")
            now = time.time()
            # Keyed by client IP, not a single global "admin" bucket --
            # otherwise anyone, unauthenticated, could lock the real admin
            # out of /admin/login for up to an hour with 5 wrong guesses
            # from any IP (a self-inflicted DoS the old global key allowed;
            # see the maintainer's local notes).
            key = f"admin:{_client_ip(environ)}"
            if not login_limiter.allow(key, now=now):
                error = "Too many attempts -- try again later."
                lockout_seconds = login_limiter.retry_after(key, now=now)
                # WARNING, and the IP itself (not personal guest data, and
                # already visible in nginx's own access log) -- counted by
                # the watchdog (app/watchdog.py).
                log.warning("rate limit blocked: admin login from %s", _client_ip(environ))
            elif verify_admin_password(password, self.settings.admin_password_hash):
                sid = _new_session({"kind": "admin"})
                return "302 Found", [("Location", "/admin"), ("Set-Cookie", _session_cookie_header(sid))], ""
            else:
                error = "Wrong password."
        err_html = f'<p class="err">{esc(error)}</p>' if error else ""
        login_label = "Log in"
        body = f"""{err_html}<form method="post" class="card">
          <label>Admin password <input class="big-input" name="password" type="password" required></label>
          <div class="submit-row"><button type="submit" id="admin-login-btn">{esc(login_label)}</button></div>
        </form>"""
        if lockout_seconds:
            body += _lockout_countdown_script(lockout_seconds, "admin-login-btn", login_label)
        return "200 OK", [("Content-Type", "text/html")], page("Admin login", body)

    def admin_overview(self, method: str, environ):
        session = _get_session(environ)
        if not session or session.get("kind") != "admin":
            return "302 Found", [("Location", "/admin/login")], ""
        show_past = "past=1" in environ.get("QUERY_STRING", "")
        today = datetime.now(timezone.utc).date()
        # scope="all" (not all_registrations()/find_user_by_id(), which are
        # live-only) so an erased guest's past registrations still show up
        # here instead of silently vanishing -- their user row moved to the
        # archive with a hashed email (Store.erase_user), and so did every
        # one of their registration rows, regardless of status. See
        # security.hash_email_for_erasure/is_erased_email.
        # Read live and archived separately (not just scope="all") so the
        # past/upcoming filter below can treat archived rows as always-past
        # regardless of their occurrence_date -- an erased user's booking
        # was already force-canceled by Store.erase_user before archiving,
        # so it's never something the "today + future only" view needs to
        # surface; it should only appear once "include past" is toggled.
        live_regs = [Registration(**r) for r in self.store.read_registrations(scope="live")]
        archived_regs = [Registration(**r) for r in self.store.read_registrations(scope="archived")]
        all_regs = live_regs + archived_regs
        users_by_id = {u["user_id"]: User(**u) for u in self.store.read_users(scope="all")}
        # "Times booked" has always counted every registration ever made by
        # this user_id, live or since-canceled -- computed here from the
        # same all-scope set rather than Store.times_registered() (which
        # only reads the live CSV), so an erased user's historical count
        # doesn't drop to 0 just because their rows moved to the archive.
        times_by_user = Counter(r.user_id for r in all_regs)
        # Map hashed-email -> archived user_ids sharing that hash, so a live
        # user who re-books under the same email after being erased can have
        # their pre-erasure history folded into "Times booked" below. This
        # never touches the archived row itself (still just its own count,
        # name "[erased]", hashed email) -- see the docstring at the top of
        # this method's PR description / the maintainer's local notes for why nothing is
        # restored or merged on disk, only summed for display here.
        archived_ids_by_hash: dict[str, list[str]] = {}
        for u in users_by_id.values():
            if is_erased_email(u.email):
                archived_ids_by_hash.setdefault(u.email, []).append(u.user_id)
        regs = all_regs if show_past else [r for r in live_regs if date.fromisoformat(r.occurrence_date) >= today]
        regs.sort(key=lambda r: (r.occurrence_date, r.course_shortname))
        # Guest bookings (2026-07): group every registration -- live AND
        # archived, so an erased party member's row still counts toward
        # "+N guest(s)" on the still-live leader's row -- by party_id, so
        # each row can show who it booked together with (see Registration's
        # own docstring for what party_id/invited_by_user_id record).
        # Blank party_id (a solo booking, including everything made before
        # this feature existed) is never grouped -- see Store's own
        # promote_next_waitlisted docstring for the same "blank means not a
        # party" convention.
        party_members: dict[str, list[Registration]] = {}
        for r in all_regs:
            if r.party_id:
                party_members.setdefault(r.party_id, []).append(r)
        rows = []
        for r in regs:
            user = users_by_id.get(r.user_id)
            erased = user is not None and is_erased_email(user.email)
            # The Course column shows the short internal key (r.course_shortname
            # below), not the human title -- compact and matches how admins
            # already refer to courses via /book/<shortname>. The full title
            # is still used in the cancel-confirmation dialog text, where a
            # human-readable name reads better than a shortname.
            course = self.settings.course(r.course_shortname)
            title = course.title if course else r.course_shortname
            if erased:
                # The hash (Store.erase_user's replacement for the real
                # email, e.g. "erased:<64 hex chars>") is long -- ~70
                # characters. A colspan across Name+Email would fit it more
                # comfortably, but it would also give that one row fewer
                # <td> cells than every other row, which breaks the
                # sortable table script's index-based column lookup
                # (_sortable_filterable_table_script) for that row. Putting
                # it in the Email cell alone (wrapped via .hash-cell) keeps
                # every row's cell count identical -- correctness over the
                # extra width.
                name_cell = f"<td>{esc(user.name)}</td>"  # "[erased]", set by Store.erase_user
                email_cell = f'<td class="hash-cell">{esc(user.email)}</td>'
            elif user:
                name_cell = f"<td>{esc(user.name)}</td>"
                email_cell = f"<td>{esc(user.email)}</td>"
            else:
                name_cell = "<td>(unknown)</td>"
                email_cell = "<td>(unknown)</td>"
            times = times_by_user.get(r.user_id, 0)
            prior = 0
            if user and not erased:
                # Same email, re-booked after a prior erasure: the old
                # identity's hash (Store.erase_user) still matches
                # hash_email_for_erasure(this live user's real email), even
                # though it's a brand-new user_id. Fold that pre-erasure
                # history into the displayed count -- nothing on disk
                # changes, this is display-only.
                hashed = hash_email_for_erasure(user.email, self.settings.erasure_pepper)
                for prior_uid in archived_ids_by_hash.get(hashed, []):
                    prior += times_by_user.get(prior_uid, 0)
                times += prior
            times_cell = f"{times} (incl. {prior} pre-erasure)" if prior > 0 else str(times)
            if user and not erased:
                cancel_id = f"admin-cancel-{esc(r.registration_id)}"
                disabled = r.status not in (STATUS_CONFIRMED, STATUS_WAITLISTED)
                actions = (
                    f'<form method="post" action="/admin/cancel/{esc(r.registration_id)}" id="{cancel_id}-form">'
                    f'<button type="submit" class="confirm-dialog-btn" data-dialog="{cancel_id}-dialog" '
                    f'{"disabled" if disabled else ""}>Cancel</button>'
                    "</form>"
                    f'<dialog id="{cancel_id}-dialog" class="card">'
                    f"<p><b>Are you sure?</b></p>"
                    f"<p>Cancel <b>{esc(user.name)}</b>'s booking for <b>{esc(title)}</b> "
                    f"on {esc(r.occurrence_date)}? They'll be notified by email.</p>"
                    f'<label>Optional message to them <textarea name="message" rows="2" class="big-input" '
                    f'form="{cancel_id}-form"></textarea></label>'
                    '<div class="submit-row">'
                    f'<button type="submit" form="{cancel_id}-form">Confirm cancellation</button> '
                    f'<button type="button" class="dialog-close-btn" data-dialog="{cancel_id}-dialog">Never mind</button>'
                    "</div></dialog>"
                )
            else:
                # Archived (erased) or otherwise unresolvable registrations
                # aren't actionable here -- find_by_id() only reads the live
                # CSV, so admin_cancel() couldn't find one of these anyway.
                actions = ""
            # "Guest of <leader>" if this row was added by someone else, or
            # "+N guest(s)" on the leader's own row if they brought people
            # along -- blank for an ordinary solo booking. Looked up by
            # user_id (not registration_id) since the leader/guest relation
            # is between PEOPLE, and the whole point of party_id is to
            # survive each member canceling independently (see
            # Registration's own docstring) -- so this still shows correctly
            # even after some party members have already canceled.
            party_cell = ""
            if r.invited_by_user_id:
                leader_user = users_by_id.get(r.invited_by_user_id)
                leader_label = leader_user.name if leader_user else "(unknown)"
                party_cell = f"guest of {leader_label}"
            elif r.party_id:
                other_members = {
                    m.user_id for m in party_members.get(r.party_id, []) if m.user_id != r.user_id
                }
                if other_members:
                    n = len(other_members)
                    party_cell = f"+{n} guest{'s' if n != 1 else ''}"
            rows.append(
                f"<tr><td>{esc(r.status)}</td><td>{esc(r.course_shortname)}</td>"
                f"<td>{esc(r.occurrence_date)}</td>{name_cell}{email_cell}"
                f"<td>{esc(r.registered_at)}</td><td>{esc(times_cell)}</td>"
                f"<td>{esc(party_cell)}</td>"
                f"<td>{actions}</td></tr>"
            )
        toggle = '<a href="/admin">today + future only</a>' if show_past else '<a href="/admin?past=1">include past</a>'
        table_id = "admin-overview-table"
        body = f"""<p>{toggle}</p>
        <div class="table-tools">
          <input type="search" id="{table_id}-filter" class="big-input" placeholder="Filter...">
        </div>
        <table id="{table_id}" border="1" cellpadding="6">
        <thead><tr>
          <th>Status<span class="sort-indicator"></span></th>
          <th>Course<span class="sort-indicator"></span></th>
          <th>Date<span class="sort-indicator"></span></th>
          <th>Name<span class="sort-indicator"></span></th>
          <th>Email<span class="sort-indicator"></span></th>
          <th>Registered<span class="sort-indicator"></span></th>
          <th>Times booked<span class="sort-indicator"></span></th>
          <th>Party<span class="sort-indicator"></span></th>
          <th>Actions<span class="sort-indicator"></span></th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody></table>""" + _sortable_filterable_table_script(table_id) + _DIALOG_WIRING_SCRIPT
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
            if course:
                # Both sides notified, always -- see _send_cancellation_emails
                # (standing default now, SOLUTION-DESIGN.md). The admin's own
                # copy is what surfaces an unexpected cancellation if someone
                # other than you got into /admin and did this.
                self._send_cancellation_emails(course, reg.occurrence_date, user, canceled_by="host", message=message)
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Registration canceled and guest notified.</p>")
        body = f"""
        <p>About to cancel <b>{esc(user.name if user else '(erased)')}</b>
        ({esc(user.email if user else '(erased)')}) for
        <b>{esc(course.title)}</b> on {esc(reg.occurrence_date)}.</p>
        <form method="post" class="card">
          <label>Optional message to them <textarea name="message" rows="3" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Cancel this booking</button></div>
        </form>"""
        return "200 OK", [("Content-Type", "text/html")], page("Cancel registration", body)
