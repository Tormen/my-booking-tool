"""wsgiref-based web app -- no framework dependency. Routes:

  GET      /courses                 overview of every configured course, linking to /book/<shortname>
  GET/POST /book/<shortname>        attendee booking form (name+email only)
  GET/POST /cancel/<token>          attendee self-cancel (link from email)
  GET/POST /reinstate/<token>       attendee self-reinstate (link from the
                                     cancellation email), no login needed --
                                     same bearer-token model as /cancel/<token>,
                                     but the token is a FRESH one minted at
                                     cancellation time, not the original
                                     booking's own cancel token (see
                                     Store.cancel()'s reinstate_token_hash
                                     param and guest_reinstate()'s docstring)
  GET/POST /my                      attendee login (email+password, "Login"/"Sign up" tabs) / bookings list
  POST     /my/signup               "Sign up" tab's target -- create account + email a confirm link
  GET/POST /my/confirm/<token>      set password -- first-time account confirmation
                                     AND password reset both land here (same token
                                     mechanism, see storage.User.confirm_token_hash)
  GET/POST /my/reset                request a confirm/reset link by email (always
                                     the same response either way -- doesn't reveal
                                     whether an email is registered)
  POST     /my/cancel/<reg_id>      attendee cancels one of their own bookings
  POST     /my/reinstate/<reg_id>   attendee undoes a cancellation of their own,
                                     for an occurrence still in the future
  POST     /my/logout               attendee logout
  POST     /my/delete-account       attendee erases their own account (Art. 17)
  GET      /my/session               JSON {"logged_in": bool, "email": ...} for the
                                     STATIC homepage's own JS to check (see my_session_status)
  GET      /my/settings             view/change name, view or abort a pending
                                     email change
  POST     /my/settings/name        change display name (takes effect immediately)
  POST     /my/settings/email       request an email change (rate-limited,
                                     sends a confirm link to the NEW address)
  POST     /my/settings/email/cancel  abort a pending email change
  GET/POST /my/confirm-email/<token>  finalize a requested email change --
                                     same GET-preview/POST-consume shape as
                                     /my/confirm/<token>, see my_confirm_email()
  GET/POST /my/cancel-email-change/<token>  no-login "abort this pending
                                     email change" link (linked from the
                                     CURRENT address's own notification
                                     email) -- a SEPARATE token from the one
                                     above, see storage.User
                                     .pending_email_cancel_token_hash
  GET/POST /admin/login             admin login
  GET      /admin                   admin overview (today+future by default)
  GET/POST /admin/cancel/<reg_id>   host cancels a registration, optional message
                                     (requires an admin login session)
  POST     /admin/reinstate/<reg_id> host undoes a cancellation on any
                                     registration, for an occurrence still
                                     in the future (requires admin login)
  GET/POST /host-cancel/<reg_id>    same cancellation, but as a no-login "magic
                                     link" -- this is what the calendar event's
                                     own per-participant "cancel:" line links
                                     to (see app/calendar_sync.py), so the host
                                     can cancel someone straight from their
                                     calendar app without first logging into
                                     /admin. Gated only by registration_id
                                     being an unguessable uuid4 (see
                                     host_cancel()'s own docstring for why
                                     that's an adequate boundary here).
  GET/POST /host-reinstate/<reg_id> no-login "magic link" reinstate, reachable
                                     from the ADMIN's own copy of the
                                     cancellation email -- same gating as
                                     /host-cancel/<reg_id> (unguessable uuid4
                                     only, no separate token).

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

GET /internal/status         same-process JSON dump of SESSIONS (who's
                              logged in, since when, last page+timestamp)
                              plus the current maintenance-mode state, for
                              `my-bt status` to query directly over HTTP on
                              127.0.0.1 -- see internal_status() below for
                              why this doesn't need its own auth system.
2026-07-13, the operator: "I would prefer a web endpoint that queries on
localhost the running server" over persisting session state to disk --
this app already only ever listens on 127.0.0.1 (see app/serve.py), so
`my-bt` (always run on the same host) can hit that same loopback port
directly, no new persistence layer needed. The endpoint rejects any
request carrying X-Forwarded-For, since that header is only ever set by
nginx (see _client_ip's own docstring) -- a request my-bt sends by
connecting straight to 127.0.0.1:8811 never has it, but anything arriving
via the public reverse proxy always would, so as long as nginx's own
config never proxies this path (it doesn't, and shouldn't), this is
unreachable from outside this host at all, with that header check as a
second layer of defense.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
from datetime import date, datetime, timedelta, timezone
from http import cookies
from urllib.parse import parse_qs, urlparse

from . import calendar_sync
from . import cli_list
from . import maintenance
from .caldav_client import CalDAVClient, CalDAVError
from .cancel_flow import (
    CANCELABLE_STATUSES, cancel_and_promote, cancel_occurrence, find_cancelable_registrations_for_occurrence,
)
from .cancellation import (
    booking_details_text, course_recap_html, greeting_html, html_email_body, html_to_text, intro_html,
    send_cancellation_emails, send_reinstatement_emails,
)
from .config import Settings
from .email_templates import load_email_template, render_template
from .emailer import _masked, send_mail
from .erasure import erase_user_by_email
from .security import (
    RateLimiter, hash_secret, hash_token, is_erased_email, new_token,
    sanitize_csv_field, tokens_match, verify_admin_password, verify_secret,
)
from .slots import build_occurrences
from .storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_PENDING_CONFIRMATION,
    STATUS_WAITLISTED, Registration, Store, User, format_display_timestamp, now_iso, status_label,
)
from .templates import esc, page
from .version import PACKAGE_VERSION

log = logging.getLogger("my_booking.webapp")

# status_label() (display-only Status-column labeling) moved to
# app/storage.py 2026-07-13 -- see its own docstring there for why
# (app/cli_list.py needs it too, for `my-bt list`'s clean default view).

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

# /my/settings' "change email" request (2026-07-10) -- keyed by user_id
# (not email/IP) since this action is only reachable from an already
# logged-in session, unlike login_limiter/reset_ip_limiter which must
# stay usable against an anonymous, possibly-fake email string. Same
# 5/hour ceiling as login_limiter, just a different bucket -- there's no
# reason a guest changing their own email needs a looser or stricter
# budget than a login attempt does.
email_change_limiter = RateLimiter(max_attempts=5, window_seconds=3600)

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
# will ever look for (guest_email_0.. guest_email_{settings.max_guests-1}),
# so a hand-crafted POST can't make it scan an unbounded number of form
# fields. The form's own JS also stops offering "+ Add participant" once
# this many rows exist -- see _book_page()'s guest-rows script. Now a
# configurable Settings.max_guests (2026-07-09, the operator: "add a setting for
# the max number of guests ... default to 3" -- see settings.toml
# [defaults].max_guests); this was a fixed constant of 9 before.

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


_SAFE_NEXT_PATH_RE = re.compile(r"^/(courses|book/[A-Za-z0-9_-]+)$")


def _safe_next_path(raw: str) -> str:
    """Validates a `?next=` value before it's ever used in a redirect or
    echoed into a hidden form field (2026-07-11, the operator: "Login link returns
    to originating page" -- clicking the plain "Login" link shown on
    /courses or /book/<shortname> for an anonymous visitor, see
    _anonymous_banner_html(), should land back on that SAME page after a
    successful login instead of always on /my's bookings list).

    Deliberately an ALLOWLIST, not just "starts with a single /" -- an
    open-redirect guard alone (blocking `//evil.com` or `https://...`)
    would still let this become a probe for arbitrary internal paths
    reachable only while logged in. The only two places this app ever
    generates a `next=` value are /courses and /book/<shortname> (see
    App.courses()/App.book() -- both pass their own path to
    _session_banner_html()), so only those two shapes are ever considered
    safe to redirect back to; anything else (missing, malformed, or
    hand-edited in the URL bar) falls back to "" -- which every caller
    below treats exactly like "no next path was given" (i.e. lands on
    /my, the prior/default behavior)."""
    if raw and _SAFE_NEXT_PATH_RE.fullmatch(raw):
        return raw
    return ""


def _latest_logged_ip(path: str | None) -> str | None:
    """Last non-empty line of an IP-tracking log file (e.g. the operator's own
    /home/me/my-ip.log, kept fresh by infrastructure outside this app) --
    a second source of "what's my current IP" alongside DNS, for exactly
    the same reason nginx's own sync-dynamic-ip-acls.sh already checks
    both when rebuilding /admin's IP allowlist: DNS can lag an actual IP
    change by however long the record's TTL/propagation takes, while a
    locally-written log file is updated the moment the IP itself changes.
    None on any error (unset, missing file, unreadable, empty) -- this is
    just one of two independent sources (see _maintenance_bypass_allowed
    below), so a problem with this one alone shouldn't take out the other."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return None
    return lines[-1] if lines else None


def _maintenance_bypass_allowed(client_ip: str, hostname: str | None, ip_log_path: str | None = None) -> bool:
    """True if `client_ip` matches EITHER `hostname`'s current resolved
    address(es) OR the last logged IP in `ip_log_path` -- lets
    [site].maintenance_bypass_hostname/maintenance_bypass_ip_log (e.g.
    the operator's own dynamic-DNS name "ssh.example.net" and his
    /home/me/my-ip.log -- the exact same two sources nginx's own
    sync-dynamic-ip-acls.sh already checks to keep /admin's IP allowlist
    current) still reach /courses and /book/<shortname> normally while
    maintenance mode blocks everyone else (2026-07-10: "can the
    maintenance mode still let me access the site from ssh.example.net
    please?", then "if you need an IP this changes and the latest can be
    found in /home/me/my-ip.log, but else the DNS also auto-updates!").
    Either source alone is enough -- they're independent fallbacks for
    each other, not both required.

    Both sources are read fresh on every call rather than cached --
    maintenance mode is rare and short-lived by nature, so the extra DNS
    lookup/file read only ever happens while it's actually on, and a stale
    cached IP would be exactly wrong if your dynamic IP changes
    mid-window. Fails CLOSED (returns False, i.e. no bypass -- maintenance
    stays in effect) if NEITHER setting is configured, or every configured
    source errors out (unresolvable hostname, unreadable/missing/empty log
    file): an outage in either source should never accidentally leave
    maintenance mode not actually blocking the general public, only
    worst-case block you too until DNS/the log file recovers.

    Same trust model as `_client_ip()` itself (see its own docstring) --
    this app is only ever reachable via its own nginx reverse proxy on
    127.0.0.1, which is what actually sets X-Forwarded-For, so an outside
    client can't spoof its way past this."""
    allowed: set[str] = set()
    if hostname:
        try:
            allowed |= {info[4][0] for info in socket.getaddrinfo(hostname, None)}
        except (socket.gaierror, OSError):
            pass
    logged_ip = _latest_logged_ip(ip_log_path)
    if logged_ip:
        allowed.add(logged_ip)
    return client_ip in allowed


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


def _login_required_redirect() -> tuple[str, list, str]:
    """302 to /my (the login page) for a guest-only endpoint hit with no
    valid session -- most commonly because the session simply timed out
    while the guest still had the tab open and then clicked something.
    2026-07-14, the operator: "Can the page please redirect to login when the
    session times out?" Replaces the old bare "403 Forbidden"/"log in
    first" plain-text response every my_*() guest-action handler used to
    return here, which left the guest stuck looking at unstyled error
    text with no way forward.

    Not `next=`-aware: _safe_next_path()'s own allowlist only covers
    /courses and /book/<shortname> (the two shapes _session_banner_html()
    ever generates for an anonymous visitor) -- none of these guest-
    action endpoints (cancel/reinstate/delete-account/settings/...) are
    in it, and expanding that allowlist is a separate, deliberate
    decision this fix doesn't make on its own. Always lands on plain
    /my; logging back in from there and clicking through again is one
    extra step, not a dead end."""
    return "302 Found", [("Location", "/my")], ""


def _record_page_view(environ, path: str) -> None:
    """2026-07-13, the operator: "Can I see with my-bt who is currently logged
    in please? (should be user, since when connected and their current /
    last loaded page with timestamp)" -- called once per request from
    App.__call__, BEFORE routing, so it captures the path even if the
    route handler itself then 404s/errors. Only touches SESSIONS for a
    request that already carries a valid session cookie (i.e. someone
    logged in as a guest on /my or an admin on /admin) -- anonymous
    browsing of /courses, /book/<slug>, etc. was never "logged in" and
    isn't tracked here at all, keeping this dict from growing on every
    anonymous hit.

    "Since when connected" is deliberately NOT a new field: SESSIONS
    already stores `expires` (set once, at _new_session() time, to
    now + SESSION_TTL_SECONDS) -- `expires - SESSION_TTL_SECONDS` IS the
    creation time, exactly, with no extra bookkeeping. See
    internal_status() (App, below) for the one place that math happens."""
    session = _get_session(environ)
    if session is None:
        return
    sid = session["_sid"]
    entry = SESSIONS.get(sid)
    if entry is not None:
        entry["last_page"] = path
        entry["last_seen"] = time.time()


def _invalidate_all_sessions_for_user(user_id: str) -> None:
    """Logs a guest out of EVERY active session (any browser/device), not
    just the one making the current request -- used after a credential-
    like change where an already-open old session should stop working
    (2026-07-11, the operator, re: email-change confirmation: "Logout user
    before email is changed (so with its old email)"). Sessions are
    keyed by session_id, not user_id, so this is a linear scan of the
    (small, in-memory -- see SESSIONS' own module comment) session store;
    fine at this app's scale, same tradeoff SESSIONS itself already
    accepts. A session dict for `user_id` is invalidated regardless of
    whether it's a "guest" or (impossible in practice, but harmless if it
    ever happened) some other kind sharing that same id value."""
    stale = [sid for sid, data in SESSIONS.items() if data.get("user_id") == user_id]
    for sid in stale:
        SESSIONS.pop(sid, None)


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


# 2026-07-07, the operator (repeatable console CSP violation on /my's lockout
# screen, "maybe related?"): these three scripts used to be built as
# f-strings interpolating a button id/label/seconds straight into the
# <script> text, per call site. Every render therefore produced different
# bytes -> a different sha256 hash -> never matched the fixed CSP
# script-src allow-list, so the countdown/disable behavior has likely
# never actually run in production. Fixed the same way the sortable-table
# script was fixed earlier: these are now plain, non-interpolated
# constants that read whatever they need from data-* attributes and from
# the button's own already-rendered text, so ONE stable hash per script
# covers every page/call site, forever. Callers now just add the right
# data-* attribute(s) to their HTML and append the matching constant.

_RESEND_COOLDOWN_SCRIPT = """<script>
(function() {
  var btn = document.querySelector("[data-resend-cooldown-btn]");
  if (!btn) return;
  var label = btn.textContent;
  function tick() {
    var until = parseInt(localStorage.getItem("mb_resend_until") || "0", 10);
    var left = Math.ceil((until - Date.now()) / 1000);
    if (left > 0) {
      btn.disabled = true;
      btn.textContent = label + " (" + left + "s)";
      setTimeout(tick, 250);
    } else {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
  tick();
})();
</script>"""

_RESEND_INLINE_COOLDOWN_SCRIPT = """<script>
(function() {
  var form = document.querySelector("[data-resend-inline-form]");
  var btn = document.querySelector("[data-resend-inline-btn]");
  var status = document.querySelector("[data-resend-inline-status]");
  if (!form || !btn) return;
  var label = btn.textContent;
  function tick() {
    var until = parseInt(localStorage.getItem("mb_resend_until") || "0", 10);
    var left = Math.ceil((until - Date.now()) / 1000);
    if (left > 0) {
      btn.disabled = true;
      btn.textContent = label + " (" + left + "s)";
      setTimeout(tick, 250);
    } else {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
  form.addEventListener("submit", function(ev) {
    if (!window.fetch) return;  // no fetch: let the real submit go through
    ev.preventDefault();
    localStorage.setItem("mb_resend_until", String(Date.now() + 60 * 1000));
    tick();
    if (status) status.textContent = " Sending...";
    fetch(form.action, {method: "POST", body: new URLSearchParams(new FormData(form))})
      .then(function() { if (status) status.textContent = " Sent -- check your email."; })
      .catch(function() { if (status) status.textContent = " Couldn't send -- try again."; });
  });
  tick();
})();
</script>"""

_LOCKOUT_COUNTDOWN_SCRIPT = """<script>
(function() {
  var btn = document.querySelector("[data-lockout-btn]");
  if (!btn) return;
  var remaining = parseInt(btn.dataset.lockoutSeconds, 10) + 1;
  var original = btn.textContent;
  btn.disabled = true;
  function tick() {
    if (remaining <= 0) {
      btn.disabled = false;
      btn.textContent = original;
      return;
    }
    btn.textContent = original + " (" + remaining + "s)";
    remaining -= 1;
    setTimeout(tick, 1000);
  }
  tick();
})();
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


# /admin overview's "cancel entire session" checkbox (2026-07-13, the operator:
# "auto check ALL participants as well and GREY OUT and disable the cancel
# button on any other line ... undone, when you uncheck ANY of them (this
# then unchecks all)") -- see admin_overview()'s own row-rendering comment
# for the checkbox/button markup this wires up (`.cancel-entire-checkbox`/
# `.cancel-btn`, both tagged with a shared `data-occurrence` key). Module-
# level constant, not per-row-interpolated, for the same CSP script-src-hash
# reason _SORTABLE_FILTERABLE_TABLE_SCRIPT is (see that constant's own
# comment) -- one script, one hash, no matter how many rows/occurrences
# exist on the page.
_CANCEL_ENTIRE_SESSION_SCRIPT = """<script>
(function() {
  document.querySelectorAll(".cancel-entire-checkbox").forEach(function(cb) {
    cb.addEventListener("change", function() {
      var key = cb.dataset.occurrence;
      var ownButton = cb.form ? cb.form.querySelector("button.cancel-btn") : null;
      document.querySelectorAll(".cancel-entire-checkbox").forEach(function(sib) {
        if (sib.dataset.occurrence === key) sib.checked = cb.checked;
      });
      document.querySelectorAll("button.cancel-btn").forEach(function(btn) {
        if (btn.dataset.occurrence !== key) return;
        // Checking: grey out every OTHER row's Cancel button (this row's
        // own button is how you actually submit) -- unchecking: re-enable
        // every one of them, including this row's own (it may have been
        // disabled by a SIBLING's checkbox in the meantime).
        if (cb.checked && btn === ownButton) return;
        btn.disabled = cb.checked;
      });
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


def _course_recap_html(course, occ_date: str) -> str:
    """Thin wrapper around app.cancellation.course_recap_html (2026-07-09) --
    the WHAT/WHEN/WHERE(+description) recap shown on the booking-confirmation
    page and every cancel-confirmation page (guest_cancel/admin_cancel/
    host_cancel), reusing the EXACT SAME generator the HTML emails use
    (the operator: "Can be always the same code that generates this for the page
    or email"), so page and email can never drift apart on layout, emoji,
    or ordering. See that function's own docstring for the full rationale."""
    return course_recap_html(course, occ_date)


# Client-side filter (substring, across every cell) + click-a-header-to-sort
# for a <table> immediately preceded by a `.table-tools` <div> containing an
# `<input type="search">` (see _table()/admin_overview()'s own markup for
# that exact structure) -- standing default for every table in the app
# (2026-07-05, see SOLUTION-DESIGN.md); both /my's bookings table(s) and
# /admin's overview table use this, so a future third table gets the same
# behavior for free rather than a one-off. Deliberately vanilla JS/no
# library: these tables are small (one operator's own bookings, or one
# small deployment's admin view), so a full data-grid dependency would be
# more weight than the problem needs. Sorting is index-based (numeric-aware,
# falls back to a locale-aware string compare) -- every row in a given table
# must have the same number of cells as the header for column indexes to
# line up (see admin_overview()'s comment on why an erased row's hash goes
# in the Email cell rather than a colspan, specifically because of this).
#
# A MODULE-LEVEL CONSTANT (2026-07-10 fix), not a function taking table_id
# anymore -- it used to interpolate table_id directly into the script text
# via document.getElementById(...), which meant EVERY distinct table_id
# produced a byte-different script, and therefore a DIFFERENT CSP
# script-src hash per table. That's unmaintainable behind a hash-based CSP
# (real incident: booking.example.org's hardened nginx config only allow-lists two
# script hashes total, so /my's table silently got a different hash than
# /admin's, and neither was actually allow-listed -- BOTH were silently
# non-functional, including the confirm-dialog wiring script that's
# concatenated right after this one). Fixed by locating the table/filter
# input via DOM relationships (`document.currentScript`'s own preceding
# siblings) instead of by id -- this script's text is now IDENTICAL no
# matter how many tables exist or what they're named, so ONE hash covers
# every one of them, forever.
_SORTABLE_FILTERABLE_TABLE_SCRIPT = """<script>
(function() {
  var table = document.currentScript.previousElementSibling;
  if (!table || table.tagName !== "TABLE" || !table.tHead || !table.tBodies.length) return;
  var tbody = table.tBodies[0];
  var headerCells = Array.prototype.slice.call(table.tHead.rows[0].cells);
  var rows = Array.prototype.slice.call(tbody.rows);
  function applySort(th, idx, dir) {
    headerCells.forEach(function(h) {
      h.dataset.dir = "";
      var i = h.querySelector(".sort-indicator");
      if (i) i.textContent = "";
    });
    th.dataset.dir = dir;
    var indicator = th.querySelector(".sort-indicator");
    if (indicator) indicator.textContent = dir === "asc" ? " ▲" : " ▼";
    var sorted = rows.slice().sort(function(a, b) {
      var av = a.cells[idx] ? a.cells[idx].textContent.trim() : "";
      var bv = b.cells[idx] ? b.cells[idx].textContent.trim() : "";
      var an = parseFloat(av), bn = parseFloat(bv);
      var bothNumeric = av !== "" && bv !== "" && !isNaN(an) && !isNaN(bn)
        && /^-?[0-9.]+$/.test(av) && /^-?[0-9.]+$/.test(bv);
      var cmp = bothNumeric ? (an - bn) : av.localeCompare(bv, undefined, {sensitivity: "base"});
      return dir === "asc" ? cmp : -cmp;
    });
    sorted.forEach(function(r) { tbody.appendChild(r); });
  }
  headerCells.forEach(function(th, idx) {
    th.style.cursor = "pointer";
    th.addEventListener("click", function() {
      applySort(th, idx, th.dataset.dir === "asc" ? "desc" : "asc");
    });
  });
  var toolsDiv = table.previousElementSibling;
  var filterInput = toolsDiv ? toolsDiv.querySelector('input[type="search"]') : null;
  if (filterInput) {
    filterInput.addEventListener("input", function() {
      var q = filterInput.value.trim().toLowerCase();
      rows.forEach(function(r) {
        r.style.display = (!q || r.textContent.toLowerCase().indexOf(q) !== -1) ? "" : "none";
      });
    });
  }
  // 2026-07-08, the operator (screenshot of /admin?past=1): "Please by default
  // sort the view ... by Date ... Like this people see also the sort
  // arrow and can understand that this page is sortable" -- every table
  // using this script was already RENDERED in some sensible server-side
  // order (see admin_overview()/my()'s own sort calls), but the arrow
  // that shows WHICH column that is, and that clicking a header does
  // anything at all, only ever appeared after an actual click. A
  // `data-default-sort="asc"|"desc"` attribute on the relevant <th> (set
  // server-side per table -- see admin_overview()/_table()) now runs the
  // exact same applySort() on load, so the indicator (and, harmlessly,
  // the sort itself -- a no-op re-sort matching what the server already
  // produced) appears immediately without a click. Still a plain data
  // attribute read at runtime, not a script-text change per table, so
  // this stays the one script-src hash every table shares.
  headerCells.forEach(function(th, idx) {
    var dir = th.getAttribute("data-default-sort");
    if (dir === "asc" || dir === "desc") applySort(th, idx, dir);
  });
})();
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

    def _cancel_occurrence(self, course_shortname: str, occurrence_date_str: str, message: str = ""):
        """Thin wrapper around app.cancel_flow.cancel_occurrence (2026-07-13,
        "cancel the entire session" -- see host_cancel_occurrence() below),
        same reasoning as _cancel_and_promote() above: passes self._sync as
        the sync_fn override so this reuses App's own cached calendar-href
        lookup instead of a fresh one-off PROPFIND, and so tests can keep
        stubbing self.app._sync to a no-op."""
        return cancel_occurrence(
            self.store, self.settings, self.caldav, course_shortname, occurrence_date_str, message=message,
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
        _record_page_view(environ, path)
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
        if m := re.fullmatch(r"/reinstate/([A-Za-z0-9_-]+)", path):
            return self.guest_reinstate(method, m.group(1), environ)
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
        if m := re.fullmatch(r"/my/reinstate/([0-9a-fA-F-]+)", path):
            return self.my_reinstate(method, m.group(1), environ)
        if path == "/my/logout":
            return self.my_logout(method, environ)
        if path == "/my/delete-account":
            return self.my_delete_account(method, environ)
        if path == "/my/session":
            return self.my_session_status(method, environ)
        if path == "/my/settings":
            return self.my_settings(method, environ)
        if path == "/my/settings/name":
            return self.my_settings_name(method, environ)
        if path == "/my/settings/email":
            return self.my_settings_email(method, environ)
        if path == "/my/settings/email/cancel":
            return self.my_settings_email_cancel(method, environ)
        if m := re.fullmatch(r"/my/confirm-email/([A-Za-z0-9_-]+)", path):
            return self.my_confirm_email(method, m.group(1), environ)
        if m := re.fullmatch(r"/my/cancel-email-change/([A-Za-z0-9_-]+)", path):
            return self.my_cancel_email_change(method, m.group(1), environ)
        if path == "/admin/login":
            return self.admin_login(method, environ)
        if path == "/admin":
            return self.admin_overview(method, environ)
        if m := re.fullmatch(r"/admin/cancel/([0-9a-fA-F-]+)", path):
            return self.admin_cancel(method, m.group(1), environ)
        if m := re.fullmatch(r"/admin/reinstate/([0-9a-fA-F-]+)", path):
            return self.admin_reinstate(method, m.group(1), environ)
        if m := re.fullmatch(r"/host-cancel/([0-9a-fA-F-]+)", path):
            return self.host_cancel(method, m.group(1), environ)
        if m := re.fullmatch(r"/host-reinstate/([0-9a-fA-F-]+)", path):
            return self.host_reinstate(method, m.group(1), environ)
        if m := re.fullmatch(r"/host-cancel-occurrence/([a-z0-9-]+)/(\d{4}-\d{2}-\d{2})", path):
            return self.host_cancel_occurrence(method, m.group(1), m.group(2), environ)
        if path == "/internal/status":
            return self.internal_status(method, environ)
        return "404 Not Found", [("Content-Type", "text/plain")], "not found"

    @staticmethod
    def _read_form(environ) -> dict:
        try:
            size = int(environ.get("CONTENT_LENGTH", 0) or 0)
        except ValueError:
            size = 0
        raw = environ["wsgi.input"].read(size).decode("utf-8") if size else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # -- maintenance mode ------------------------------------------------------

    def _maintenance_response(self, state: maintenance.MaintenanceState):
        """503, not 200 -- correctly signals "temporarily unavailable" to
        anything automated (monitoring, a bot) hitting a booking link while
        `my-bt admin site-maintenance on` is active, without touching any other route's
        status code.

        2026-07-10, the operator: "the maintenance page should have a back link or
        button" -- now that this shows on every guest-facing route (see
        _maintenance_guard), landing here for someone who followed an old
        bookmark/email link left them with nowhere to go but the browser's
        own Back button. Links to the marketing homepage (settings.base_url),
        same "Back to {site}" wording _my_login_page() uses."""
        body = (
            f'<div class="card">{maintenance.message_html(self.settings.admin_email, state.message)}'
            f'<p><a href="{esc(self.settings.base_url)}">Back to {esc(self._site_label())}</a></p></div>'
        )
        return "503 Service Unavailable", [("Content-Type", "text/html; charset=utf-8")], page("Maintenance", body)

    def _maintenance_guard(self, environ) -> tuple[str, list, str] | None:
        """2026-07-10, the operator: "The maintenance banner should be displayed on
        all pages. EXCEPT if the local excluded IP is recognized... else
        everything works normally from this IP. But I did a test and I was
        able to click on login and see the normal login page from an
        external IP in maintenance mode. This should not be!" -- courses()
        and book() originally had this check inlined, but ONLY those two
        (see app/maintenance.py's now-outdated docstring, written the same
        day: "existing-booking management (/my, /admin, /cancel/,
        /reinstate/, /host-cancel/, /host-reinstate/) is deliberately left
        untouched"). That narrower scope is exactly the bug the operator caught
        via a real external-IP test -- /my's login form worked completely
        normally for a non-bypass visitor.

        Factored out here and now called from every GUEST-facing route
        (courses, book, /cancel, /reinstate, and every /my/* endpoint) so
        there's one single place deciding "does this visitor see the
        maintenance page", instead of N separate inlined copies that can
        silently drift apart exactly like this one did.

        Deliberately NOT called from /admin/*, /host-cancel/<id>, or
        /host-reinstate/<id> -- those are the HOST's own tools (the latter
        two are unguessable-uuid4 "magic links" only ever emailed to
        admin_email), and blocking the host's own ability to manage
        bookings during a maintenance window they themselves declared would
        be counterproductive, not safer. Also NOT called from
        my_session_status() -- that's a read-only JSON status check (not a
        page, and not a booking/management action) that the STATIC
        homepage's own JS calls to swap its Login/Logout button; returning
        an HTML maintenance page there would just break that JS's JSON
        parsing for no real benefit, since nothing it does lets anyone
        book or manage anything.

        Returns the 503 response tuple to return immediately if this
        visitor should be blocked, or None if the caller should proceed
        normally (either maintenance is off, or this IP is the recognized
        bypass -- see _maintenance_bypass_allowed for that check, which
        this reuses unchanged)."""
        state = maintenance.read_state(self.store.data_dir)
        if state.enabled and not _maintenance_bypass_allowed(
            _client_ip(environ), self.settings.maintenance_bypass_hostname,
            self.settings.maintenance_bypass_ip_log,
        ):
            return self._maintenance_response(state)
        return None

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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        banner = self._session_banner_html(environ, next_path="/courses")
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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        course = self.settings.course(shortname)
        if course is None:
            return "404 Not Found", [("Content-Type", "text/plain")], "unknown course"

        # Computed once per request and threaded through every response
        # this method (and its helpers _book_page/_book_with_guests) can
        # return -- see _session_banner_html's own docstring.
        banner = self._session_banner_html(environ, next_path=f"/book/{shortname}")

        # 2026-07-09, the operator (screenshots of /book + /my): "when you are
        # logged in Name + email on booking page should be filled and
        # greyed out or hidden". `logged_in_user`, when set, is threaded
        # through every _book_page() render below so the name/email
        # fields are always prefilled+readonly for an active guest
        # session -- never re-blanked on an error-retry render either.
        session = _get_session(environ)
        logged_in_user = (
            self.store.find_user_by_id(session["user_id"])
            if session and session.get("kind") == "guest" else None
        )

        def capacity_lookup(sn, d):
            return self.store.count_confirmed(sn, d.isoformat())

        now = datetime.now(timezone.utc)
        occurrences = build_occurrences(
            course, self.settings, now, capacity_lookup, self._conflict_checker(exclude_own=True)
        )

        # 2026-07-11, the operator (my-bt list showing he was already `confirmed`
        # for 2026-07-11 while /book/trier-sat-yoga still offered that
        # exact date as a pickable option): "If I am already booked +
        # confirmed for a date this date should simply be hidden here for
        # me" -- a logged-in guest who already holds an active (confirmed
        # or waitlisted -- same definition Store.has_active_registration
        # already uses for the double-booking guard below/entry #85) spot
        # for a given occurrence never needs to see that date offered
        # again; they'd only get bounced by that exact guard if they tried.
        # Filtered here, once, right after occurrences is built, so both
        # the GET render and every POST error-retry render below (which all
        # reuse this same `occurrences` list) agree -- no separate
        # threading needed. This is a display-only convenience on top of
        # an already-enforced rule, not a new safety boundary: the
        # has_active_registration() check further down still runs
        # regardless, for the rare case of two tabs/a stale cached page.
        if logged_in_user is not None:
            occurrences = [
                o for o in occurrences
                if not self.store.has_active_registration(shortname, o.date.isoformat(), logged_in_user.user_id)
            ]

        if method == "POST":
            form = self._read_form(environ)
            if form.get("agree") != "on":
                return self._book_page(
                    course, occurrences, error="Please acknowledge the participation terms.",
                    banner=banner, logged_in_user=logged_in_user,
                )
            occ_date = form.get("occurrence_date", "")
            occ = {o.date.isoformat(): o for o in occurrences}.get(occ_date)
            if occ is None:
                return self._book_page(
                    course, occurrences, error="That slot is no longer available.",
                    banner=banner, logged_in_user=logged_in_user,
                )
            # An active guest session always books under ITS OWN identity --
            # the name/email fields are readonly client-side (see
            # _book_page()) purely as a courtesy/clarity cue, but readonly
            # is not a security boundary, so the server enforces this too:
            # whatever the form submitted for name/email is ignored/
            # overridden whenever logged_in_user is set, rather than trusted
            # as-is. Also closes a possible confusion where a logged-in
            # session could otherwise create a booking attributed to a
            # DIFFERENT email while the banner still says "Logged in as X".
            if logged_in_user is not None:
                email, name = logged_in_user.email, logged_in_user.name
            else:
                email, name = form.get("email", "").strip(), form.get("name", "").strip()
            if not email or "@" not in email or not name:
                return self._book_page(
                    course, occurrences, error="Please fill in your name and a valid email.",
                    banner=banner, logged_in_user=logged_in_user,
                )

            rejection = self._late_booking_rejection(occ, now)
            if rejection:
                return self._book_page(course, occurrences, error=rejection, banner=banner, logged_in_user=logged_in_user)

            guests, guest_error = self._parse_guest_entries(form, email)
            if guest_error:
                return self._book_page(course, occurrences, error=guest_error, banner=banner, logged_in_user=logged_in_user)

            # No password is ever collected here -- upsert_user_for_booking
            # only ever touches `name`, leaving any existing account's
            # password_hash (confirmed or still empty) completely alone.
            # This is what closes the old hole where re-submitting someone
            # else's email with a chosen PIN silently took over their
            # account: nothing reachable from this form can change another
            # email's credential anymore.
            user = self.store.upsert_user_for_booking(email, name)

            # 2026-07-10, the operator (screenshot of /my): "double booking
            # possible?" -- yes, neither add_registration_checking_capacity
            # nor add_party_registrations_checking_capacity ever checked
            # whether the requesting user (or a guest being added) already
            # held an active spot for this exact course+date, only
            # aggregate capacity. See Store.has_active_registration's own
            # docstring for why STATUS_PENDING_CONFIRMATION is deliberately
            # excluded. Checked here, before branching into the solo vs.
            # party path below, so both are covered by one check for the
            # leader; guests get their own check further down since they
            # aren't upserted (and so have no user_id yet) until
            # _book_with_guests runs.
            if self.store.has_active_registration(shortname, occ_date, user.user_id):
                return self._book_page(
                    course, occurrences,
                    error="You're already booked for this session -- see /my to manage it.",
                    banner=banner, logged_in_user=logged_in_user,
                )

            # 2026-07-10, the operator: "no we take their booking. if the main
            # person already booked, then cannot book again." -- so unlike
            # the leader (rejected above), a guest who already holds an
            # active spot is NOT an error: their existing booking is simply
            # kept as-is (not duplicated), and they're dropped from this
            # party before it's admitted. See _book_with_guests's
            # `already_booked` param for how the leader is told which
            # guest(s) this happened to.
            already_booked_guests = []
            filtered_guests = []
            for g_email, g_name in guests:
                existing_guest = self.store.find_user_by_email(g_email)
                if existing_guest and self.store.has_active_registration(shortname, occ_date, existing_guest.user_id):
                    already_booked_guests.append(g_email)
                else:
                    filtered_guests.append((g_email, g_name))
            guests = filtered_guests

            if guests:
                return self._book_with_guests(
                    course, shortname, occ_date, user, guests, banner=banner,
                    already_booked=already_booked_guests,
                )

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
                ) + self._already_booked_guests_note(already_booked_guests)
                return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
                    "Booked!",
                    f"<p>{msg}<br>{self._check_confirmation_text(environ)}</p>"
                    + _course_recap_html(course, occ_date),
                    banner=banner,
                )

            # Brand-new or still-unconfirmed email: deliberately does NOT
            # hold a real spot or touch the calendar yet -- see
            # storage.STATUS_PENDING_CONFIRMATION's docstring. Re-sending
            # the confirmation email on every such attempt is deliberate --
            # exactly what a "resend" should do -- but the pending ROW
            # itself is now deduped per course+date+user (2026-07-11,
            # the operator: "silent re-registration for unconfirmed accounts" --
            # see Store.has_pending_registration's own docstring for the
            # multi-row-promoted-at-once bug this closes): only the FIRST
            # attempt for a given course+date inserts a row; every retry
            # just resends the same email against the row already there.
            if not self.store.has_pending_registration(shortname, occ_date, user.user_id):
                self.store.add_registration(
                    shortname, occ_date, user.user_id, "", status=STATUS_PENDING_CONFIRMATION
                )
            self._send_confirm_email(user)
            resend_label = "Resend the confirmation email"
            # 2026-07-07, the operator (comparing this page's plain-prose "Your spot
            # for X on Y" line against the What/When/Where box shown on
            # every other page/email referring to one course instance --
            # host-cancel, Booked!, cancel/reinstate confirmations): "The way
            # you present 'one course instance' ... should be CONSISTENT
            # EVERYWHERE" -- so this now reuses the exact same
            # _course_recap_html() block as those, instead of its own
            # one-off sentence.
            body = (
                f"<p>Almost there -- we've emailed <b>{esc(email)}</b> a link to confirm your account.</p>"
                "<p>Once you click the link in the email and set a password.</p>"
                f"<p>Only then will your place in the course be reserved:"
                f"{self._already_booked_guests_note(already_booked_guests)}</p>"
                + _course_recap_html(course, occ_date)
                + '<div class="hint">Didn\'t get it? '
                '<form method="post" action="/my/reset" id="resend-form" data-resend-inline-form style="display:inline">'
                f'<input type="hidden" name="email" value="{esc(email)}">'
                f'<button type="submit" id="resend-btn" class="link-button" data-resend-inline-btn>{esc(resend_label)}</button>'
                '</form><span id="resend-status" data-resend-inline-status></span>.</div>'
                + _RESEND_INLINE_COOLDOWN_SCRIPT
            )
            return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page("Almost there", body, banner=banner)

        return self._book_page(course, occurrences, banner=banner, logged_in_user=logged_in_user)

    def _booking_details_text(self, course, occ_date: str) -> str:
        """Thin wrapper around app.cancellation.booking_details_text (moved
        there 2026-07-06 so `my-bt cancel`, which has no App instance, can
        call the exact same logic) -- kept as an App method since every
        existing call site here already has `self`. See that function's
        docstring for what/why."""
        return booking_details_text(course, occ_date)

    def _send_cancellation_emails(
        self, course, occ_date: str, user, canceled_by: str, message: str,
        registration_id: str, reinstate_token: str | None = None,
    ) -> None:
        """Thin wrapper around app.cancellation.send_cancellation_emails
        (moved there 2026-07-06 so `my-bt cancel` triggers IDENTICAL emails
        to the web admin's /admin/cancel, instead of reimplementing this)
        -- kept as an App method since every existing call site here
        already has `self.settings`. See that function's docstring for the
        full rationale (notify-both-sides, reinstate-link params, etc.).

        Also builds the CANCEL .ics attachment (2026-07-09) here, once, so
        guest_cancel()/admin_cancel()/host_cancel() -- every web caller of
        this method -- get it for free without each building it themselves."""
        ics_filename, ics_text = calendar_sync.guest_cancel_ics(self.settings, course, date.fromisoformat(occ_date))
        send_cancellation_emails(
            self.settings, course, occ_date, user, canceled_by, message,
            registration_id, reinstate_token,
            ics_attachment=(ics_filename, ics_text, "CANCEL"),
        )

    def _send_reinstatement_emails(
        self, course, occ_date: str, user, confirmed: bool, reinstated_by: str, message: str = "",
    ) -> None:
        """Thin wrapper around app.cancellation.send_reinstatement_emails,
        same pattern/rationale as _send_cancellation_emails above. Builds a
        fresh PUBLISH .ics only when `confirmed` is True -- a still-
        waitlisted reinstatement has no real slot to hand a calendar entry
        for yet, matching the original booking flow's own rule (see
        _send_booking_result_guest_email)."""
        ics_attachment = None
        if confirmed:
            ics_filename, ics_text = calendar_sync.guest_invite_ics(
                self.settings, course, date.fromisoformat(occ_date)
            )
            ics_attachment = (ics_filename, ics_text, "PUBLISH")
        send_reinstatement_emails(
            self.settings, course, occ_date, user, confirmed, reinstated_by, message,
            ics_attachment=ics_attachment,
        )

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
        recap_html = _course_recap_html(course, occ_date)
        account_line = ""
        account_line_html = ""
        if not user.password_hash:
            confirm_url = self._confirm_url(user)
            account_line = (
                f"Optional: set up a password to view/manage this from {my_url}: "
                f"{confirm_url}\n"
            )
            account_line_html = (
                f'<p>Optional: set up a password to view/manage this from '
                f'<a href="{my_url}">{my_url}</a>: <a href="{confirm_url}">{confirm_url}</a></p>'
            )
        # 2026-07-08, the operator: "they should now all start with 'Dear <NAME>',
        # correct?" -- they didn't (only _send_confirm_email() had this);
        # added here too, same terse-but-warm register. See
        # cancellation.greeting_html()'s own docstring for why it's a
        # plain, non-bold line ahead of intro_html()'s bold status
        # sentence, and why this is guest-facing only, never the admin
        # copies in this method's own siblings.
        greeting_html_val = greeting_html(user.name)
        if status == STATUS_WAITLISTED:
            intro = (
                "You're on the waitlist -- full for now, but you'll be confirmed automatically "
                "by email if a spot opens up:"
            )
            manage_link_html = f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
            leave_link_html = f'<p>Leave the waitlist directly: <a href="{cancel_url}">{cancel_url}</a></p>'
            send_mail(
                self.settings, user.email, f"Waitlisted: {course.title} on {occ_date}",
                render_template(
                    load_email_template(self.settings, "waitlisted_email.txt"),
                    greeting=f"Dear {user.name},\n\n", intro=intro, details=details,
                    manage_url=my_url, cancel_url=cancel_url, account_line=account_line,
                ),
                html_body=html_email_body(render_template(
                    load_email_template(self.settings, "waitlisted_email.html"),
                    greeting=greeting_html_val, intro=intro_html(intro), recap=recap_html,
                    manage_link=manage_link_html, leave_link=leave_link_html, account_line=account_line_html,
                )),
                bcc_addrs=self.settings.bcc_attendee_email_list,
            )
        else:
            ics_filename, ics_text = calendar_sync.guest_invite_ics(self.settings, course, date.fromisoformat(occ_date))
            intro = "Your spot is confirmed:"
            manage_link_html = f'<p>Manage your bookings: <a href="{my_url}">{my_url}</a></p>'
            cancel_link_html = f'<p>Cancel this booking directly: <a href="{cancel_url}">{cancel_url}</a></p>'
            send_mail(
                self.settings, user.email, f"Booking confirmed: {course.title} on {occ_date}",
                render_template(
                    load_email_template(self.settings, "booking_confirmed_email.txt"),
                    greeting=f"Dear {user.name},\n\n", intro=intro, details=details,
                    manage_url=my_url, cancel_url=cancel_url, account_line=account_line,
                ),
                html_body=html_email_body(render_template(
                    load_email_template(self.settings, "booking_confirmed_email.html"),
                    greeting=greeting_html_val, intro=intro_html(intro), recap=recap_html,
                    manage_link=manage_link_html, cancel_link=cancel_link_html, account_line=account_line_html,
                )),
                ics_attachment=(ics_filename, ics_text, "PUBLISH"),
                bcc_addrs=self.settings.bcc_attendee_email_list,
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
        who = f"{user.name} <{user.email}>"
        verb = "joined the waitlist for" if status == STATUS_WAITLISTED else "booked"
        send_mail(
            self.settings, self.settings.admin_email,
            f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
            render_template(
                load_email_template(self.settings, "new_booking_admin_email.txt"),
                who=who, verb=verb, course_title=course.title, occ_date=occ_date,
            ),
        )

    def _already_booked_guests_note(self, already_booked: list[str]) -> str:
        """2026-07-10, the operator: "no we take their booking" -- a guest already
        holding an active spot for this session gets dropped from the party
        rather than duplicated (see book()'s filtering just before it calls
        _book_with_guests). This renders the one-line addendum telling
        whoever submitted the form which guest(s) that happened to, so the
        booking doesn't look like it silently ignored someone they listed.
        Empty string (no addendum) when nobody was skipped."""
        if not already_booked:
            return ""
        who = ", ".join(esc(e) for e in already_booked)
        verb = "was" if len(already_booked) == 1 else "were"
        return f" ({who} already {verb} booked for this session, so we kept their existing booking as-is.)"

    def _parse_guest_entries(self, form: dict, leader_email: str) -> tuple[list[tuple[str, str]], str | None]:
        """Reads guest_email_0/guest_name_0 .. guest_email_{max_guests-1}/
        guest_name_{max_guests-1} off a submitted booking form (see
        _book_page()'s "+ Add participant" rows) -- name is optional per
        guest, email is not (see _book_with_guests() for how a blank name
        is resolved). Returns (entries, error): entries is a list of
        (email, name) pairs in the order submitted; error is a guest-facing
        message if validation failed (bad/duplicate email), in which case
        entries should be ignored and the form re-shown with that error."""
        entries: list[tuple[str, str]] = []
        seen = {leader_email.strip().lower()}
        for i in range(self.settings.max_guests):
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
        status_word = "waitlisted" if status == STATUS_WAITLISTED else "confirmed"
        send_mail(
            self.settings, self.settings.admin_email,
            f"New {'waitlist entry' if status == STATUS_WAITLISTED else 'booking'}: {course.title} on {occ_date}",
            render_template(
                load_email_template(self.settings, "new_booking_party_admin_email.txt"),
                who=who, verb=verb, course_title=course.title, occ_date=occ_date,
                party_size=str(len(users)), status_word=status_word,
            ),
        )

    def _book_with_guests(
        self, course, shortname: str, occ_date: str, leader, guests: list[tuple[str, str]], banner: str = "",
        already_booked: list[str] = (),
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
        empty string (User.name has no blank default).

        `already_booked` (2026-07-10, the operator: "no we take their booking")
        lists any guest emails book() already filtered OUT of `guests`
        because they held an active registration for this course+date --
        purely informational, for the success message's addendum via
        _already_booked_guests_note(); `guests` itself never contains
        them, so no duplicate row is ever created here."""
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
        msg += self._already_booked_guests_note(list(already_booked))
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(
            "Booked!",
            f"<p>{msg}</p><p>Everyone in the party -- including you -- got their own email with "
            "a personal cancel link and an invite to manage their booking via /my. "
            "Canceling is always individual: if someone in the party cancels later, "
            "it only affects their own spot.</p>" + _course_recap_html(course, occ_date),
            banner=banner,
        )

    def _session_banner_html(self, environ, *, on_my_page: bool = False, next_path: str = "") -> str:
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
        target="_blank" link my() already has for that.

        `on_my_page=True` (2026-07-09, the operator, screenshot of /my's own
        banner: "My bookings link on the my bookings page (in top-bar) :(")
        drops the "My bookings" link -- a link back to the exact page
        you're already looking at is dead weight, not a shortcut. Only
        my() passes this; /courses and /book (where "My bookings" is a
        genuine link elsewhere) leave it at the default.

        2026-07-09, the operator (screenshots of /book + /my): "Make it so that
        the top-bar is ALWAYS visible (except for index.html) either with
        LOGIN or with the BAR." -- an anonymous visitor to /courses or
        /book now gets a small "Login" banner instead of nothing at all,
        same box/position the logged-in banner uses, so the bar is never
        just... absent. /my's own anonymous view is deliberately NOT given
        one here -- that page IS the Login/Sign up form already (see
        _my_login_page()), so a "Login" banner sitting above a login form
        would be redundant, not helpful."""
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return self._anonymous_banner_html(next_path)
        user = self.store.find_user_by_id(session["user_id"])
        if user is None:
            return self._anonymous_banner_html(next_path)
        my_bookings_link = "" if on_my_page else '<a href="/my">My bookings</a> &middot; '
        return (
            '<div class="session-banner">'
            f"<span>Logged in as <b>{esc(user.email)}</b></span>"
            f'<span>{my_bookings_link}'
            f'<a href="{esc(self.settings.base_url)}">{esc(self._site_label())}</a> &middot; '
            '<form method="post" action="/my/logout">'
            '<button type="submit" class="link-button">Log out</button></form></span>'
            "</div>"
        )

    def _anonymous_banner_html(self, next_path: str = "") -> str:
        """The "not logged in" half of the always-visible top-bar (see
        _session_banner_html()'s own docstring) -- same box, a plain Login
        link instead of "Logged in as...". Also links to the homepage, same
        as the logged-in banner does, so an anonymous visitor gets that
        one-click way back too.

        `next_path` (2026-07-11, the operator: "Login link returns to originating
        page"), when given, is appended as `?next=<path>` on the Login
        link -- my()'s login form/POST carries it through (see
        _my_login_page()/App.my()) so a guest who clicks Login from
        /courses or /book/<shortname> lands back on that same page after
        a successful login, instead of always on /my's bookings list.
        Already validated by the caller (_session_banner_html, via
        App.courses()/App.book()) against _safe_next_path()'s allowlist --
        this method just echoes it, trusting that check rather than
        re-validating here too."""
        next_qs = f"?next={esc(next_path)}" if next_path else ""
        return (
            '<div class="session-banner">'
            "<span>Not logged in</span>"
            f'<span><a href="/my{next_qs}">Login</a> &middot; '
            f'<a href="{esc(self.settings.base_url)}">{esc(self._site_label())}</a></span>'
            "</div>"
        )

    def _check_confirmation_text(self, environ) -> str:
        """The "Check ... for confirmation and a link in case you need to
        cancel your booking" line on the single-booking confirmation page
        (2026-07-09, the operator: "If they are logged in you should rather say:
        Check My bookings (as link) and if they are NOT logged in keep
        your current version about the email") -- someone already logged
        in can just go straight to /my instead of digging through email."""
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            return 'Check <a href="/my">My bookings</a> for confirmation and a link in case you need to cancel your booking.'
        return "Check your email for confirmation and a link in case you need to cancel your booking."

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
        # 2026-07-07, the operator (screenshot of this email): "please formulate the
        # email a bit more nicer" -- added a "Dear <name>," greeting and a
        # brief sign-off, same terse-but-warm register as every other
        # guest-facing email, not a wall of bare instructions.
        send_mail(
            self.settings, user.email, subject,
            render_template(
                load_email_template(self.settings, "confirm_email.txt"),
                name=user.name, verb=verb, confirm_url=confirm_url,
                ttl_hours=str(CONFIRM_TOKEN_TTL_HOURS), site=site,
            ),
            bcc_addrs=self.settings.bcc_attendee_email_list,
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

    def _book_page(self, course, occurrences, error: str | None = None, banner: str = "", logged_in_user=None):
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
        # 2026-07-09, the operator (screenshot of /my showing 2 future confirmed
        # bookings the date-picker above never mentioned): "It could be
        # nice here, to show the user that he/she already booked the
        # classes ... Like here: I have 2 future bookings already booked,
        # they should be listed." Follow-up answers, in order: (1) "Only
        # FUTURE bookings!" -- no past history, so filtered to
        # occurrence_date >= today; (2) "Ignore canceled and also show
        # waitlisted but rather say: 'On waitinglist'"; (3) a diagonal
        # ribbon across the date-box corner (his own suggestion, in place
        # of a plain greyed-out box) "with contrasted fontcolor" -- see
        # .date-badge/.ribbon in templates.py's <style> block; (4) "not
        # clickable" -- a plain <span>, no <input>/<label> at all, unlike
        # the real bookable date-boxes below.
        already_booked = []
        if logged_in_user is not None:
            today_iso = datetime.now(timezone.utc).date().isoformat()
            already_booked = sorted(
                (
                    r for r in self.store.registrations_for_user(logged_in_user.user_id)
                    if r.course_shortname == course.shortname
                    and r.status in (STATUS_CONFIRMED, STATUS_WAITLISTED)
                    and r.occurrence_date >= today_iso
                ),
                key=lambda r: r.occurrence_date,
            )
        if not occurrences and not already_booked:
            body = subtitle + desc_html + "<p>No dates currently available, please check back next week.</p>"
        elif not occurrences:
            # Nothing left TO book, but the guest does have other future
            # bookings for this course -- show those (still useful
            # context), skip the rest of the form (name/email/agree/submit
            # would have nothing to submit against).
            already_booked_html = "".join(
                '<span class="date-btn date-badge"><span><span class="d-date">'
                + esc(r.occurrence_date) + '</span><span class="ribbon">'
                + ("On waitinglist" if r.status == STATUS_WAITLISTED else "Booked")
                + "</span></span></span>"
                for r in already_booked
            )
            body = (
                subtitle + desc_html
                + f'<label>Dates available<div class="dates" role="radiogroup" '
                f'aria-label="Dates available">{already_booked_html}</div></label>'
                + "<p>No further dates currently available, please check back next week.</p>"
            )
        else:
            bookable_items = [
                (
                    o.date.isoformat(),
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
                    + (" checked" if o.date == occurrences[0].date else "")
                    + '><span><span class="d-date">' + esc(o.date.isoformat()) + "</span>"
                    + (f'<span class="d-spots">{esc(text)}</span>' if (text := self._spots_left_text(o)) else "")
                    + "</span></label>",
                )
                for o in occurrences
            ]
            # Already-booked dates are merged chronologically into the SAME
            # row as the real, pickable ones (not a separate section) --
            # sorting the combined (date, html) pairs by date keeps that
            # true regardless of which list either date came from.
            already_booked_items = [
                (
                    r.occurrence_date,
                    '<span class="date-btn date-badge"><span><span class="d-date">'
                    + esc(r.occurrence_date) + '</span><span class="ribbon">'
                    + ("On waitinglist" if r.status == STATUS_WAITLISTED else "Booked")
                    + "</span></span></span>",
                )
                for r in already_booked
            ]
            date_buttons = "".join(
                html for _, html in sorted(bookable_items + already_booked_items, key=lambda pair: pair[0])
            )
            first_label = "Join waitlist" if occurrences[0].is_full else self.settings.book_button_label
            note_html = f'<p class="note">{esc(note)}</p>' if (note := self._policy_note()) else ""
            err_html = f'<p class="err">{esc(error)}</p>' if error else ""
            # 2026-07-09, the operator: "when you are logged in Name + email on
            # booking page should be filled and greyed out or hidden" --
            # prefilled+readonly (NOT disabled: a disabled input's value is
            # never submitted at all, which would break the POST; readonly
            # still submits it, just blocks editing) for an active guest
            # session, reusing their own account name/email. The "first
            # time booking?" hint is dropped in that case too -- it's about
            # brand-new/unconfirmed emails, which can't apply to someone
            # already logged in with a password. See book()'s own comment
            # on why the server ALSO enforces this (readonly is client-side
            # only, not a security boundary).
            if logged_in_user is not None:
                # 2026-07-09, the operator, on the earlier prefilled+readonly
                # fields: "This is confusing: If you are logged in ad you
                # book, please hide Your name + Your email fields (instead
                # of showing them prefilled)." -- the session banner right
                # above ("Logged in as ...") already tells them who they're
                # booking as, so showing greyed-out name/email fields too
                # was redundant AND read as "why can't I edit this?"
                # confusion. Still submitted via hidden inputs (never
                # `disabled` -- see the comment this replaces, same reason:
                # a disabled input's value is never POSTed at all), and the
                # server ALSO enforces this identity server-side (see
                # book()'s own comment) -- hiding the fields client-side is
                # a UX choice only, never the actual security boundary.
                identity_fields_html = (
                    f'<input type="hidden" name="name" value="{esc(logged_in_user.name)}">'
                    f'<input type="hidden" name="email" value="{esc(logged_in_user.email)}">'
                )
                first_time_hint = ""
            else:
                identity_fields_html = (
                    '<label>Your name <span class="req">(required)</span>'
                    '<input class="big-input id-input" name="name" required></label>'
                    '<label>Your email <span class="req">(required)</span>'
                    '<input class="big-input id-input" name="email" type="email" required></label>'
                )
                first_time_hint = (
                    "<p class=\"hint\">First time booking with this email? We'll send a link to confirm your\n"
                    "                account and set a password.</p>"
                )
            body = f"""
            {subtitle}
            {desc_html}
            {note_html}
            {err_html}
            <form method="post" class="card" id="book-form" autocomplete="off"
              data-book-label="{esc(self.settings.book_button_label)}">
              <label>Dates available
                <div class="dates" role="radiogroup" aria-label="Dates available">{date_buttons}</div>
              </label>
              <div class="selected-box">Selected date: <strong id="selected-date-text">{esc(occurrences[0].date.isoformat())}</strong></div>
              {identity_fields_html}
              {first_time_hint}
              <div class="guests-section">
                <div id="guest-rows"></div>
                <button type="button" id="add-guest-btn" class="link-button">+ Add participant</button>
                <p id="party-warning" class="note" style="display:none"></p>
              </div>
              <label><input type="checkbox" name="agree" required> I acknowledge the
                <a href="/terms.html" target="_blank">participation terms</a> (voluntary, at my own risk)
                for myself and any guests I'm registering above
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
              var MAX_GUESTS = {self.settings.max_guests};

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
                  '<input class="big-input id-input" name="guest_email_' + i + '" type="email" required></label>' +
                  '<label>Guest name <span class="opt">(optional)</span>' +
                  '<input class="big-input id-input" name="guest_name_' + i + '"></label>' +
                  '<button type="button" class="link-button remove-guest-btn">Remove participant</button>';
                guestRowsEl.appendChild(row);
                row.querySelector(".remove-guest-btn").addEventListener("click", function() {{
                  guestRowsEl.removeChild(row);
                  if (addGuestBtn) addGuestBtn.style.display = "";
                  updatePartyWarning();
                  refresh();
                }});
                var guestEmailEl = row.querySelector('[name="guest_email_' + i + '"]');
                guestEmailEl.addEventListener("input", function() {{ updatePartyWarning(); refresh(); }});
                guestEmailEl.addEventListener("change", refresh);
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
              function guestRowsValid() {{
                // the operator, 2026-07-09: "if a required field is empty the button
                // should not be clickable. Here someone should either remove
                // the empty participant first or provide an email at
                // least." -- every currently-present guest row's required
                // email must look valid, or the Book button stays disabled
                // until it's filled in or the row is removed.
                if (!guestRowsEl) return true;
                var inputs = guestRowsEl.querySelectorAll('input[name^="guest_email_"]');
                for (var i = 0; i < inputs.length; i++) {{
                  if (inputs[i].value.indexOf("@") <= 0) return false;
                }}
                return true;
              }}
              function refresh() {{
                var r = currentRadio();
                if (r && selText) selText.textContent = r.dataset.date;
                if (r && submitBtn) submitBtn.textContent = r.dataset.full === "1" ? "Join waitlist" : bookLabel;
                var ok = !!r && nameEl.value.trim() !== "" && emailEl.value.indexOf("@") > 0 && agreeEl.checked
                  && guestRowsValid();
                if (submitBtn) submitBtn.disabled = !ok;
                updatePartyWarning();
              }}
              for (var i = 0; i < radios.length; i++) {{ radios[i].addEventListener("change", refresh); }}
              [nameEl, emailEl, agreeEl].forEach(function(el) {{
                el.addEventListener("input", refresh);
                el.addEventListener("change", refresh);
              }});
              refresh();
              // 2026-07-11, the operator ("BUG: selected date!", screenshot showing
              // the 2026-07-18 box highlighted/checked while "Selected
              // date:" still read 2026-07-11): some browsers restore a
              // PREVIOUSLY-checked radio button on reload/back-forward
              // navigation on their own, independent of this script and
              // AFTER it already ran once -- silently, with no "change"
              // event, so refresh()'s one call above (which matched the
              // server's own default, occurrences[0]) never gets to react
              // to the browser's later override. autocomplete="off" on the
              // form (above) stops most browsers from doing this restore at
              // all; "pageshow" (fires on every render including a
              // back/forward-cache restore, unlike "load") is a second,
              // defensive line of the same fix, re-running refresh() at
              // that point to guarantee the visible highlight and the
              // "Selected date" text can never disagree, on any browser.
              window.addEventListener("pageshow", refresh);
            }})();
            </script>"""
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page(course.title, body, banner=banner)

    # -- /cancel/<token> (guest, from email) ---------------------------------

    def guest_cancel(self, method: str, token: str, environ):
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        reg = self.store.find_by_guest_token_hash(hash_token(token))
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page("Not found", "<p>This link is invalid or already used.</p>")
        course = self.settings.course(reg.course_shortname)
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            # Naturally guarded against a stale/replayed submission already
            # (find_by_guest_token_hash above only ever matches a still-
            # CONFIRMED/WAITLISTED row, so a second POST with the same
            # token would already have hit "reg is None" above) -- this
            # explicit changed-guard (2026-07-10, same fix as
            # admin_cancel()/host_cancel()/my_cancel()) additionally closes
            # a genuine concurrent double-submit (two requests racing on
            # the same token), where both could pass the lookup above
            # before either actually cancels.
            # Freshly minted here (2026-07-10), not reused from `token`
            # above -- see Store.cancel()'s own `reinstate_token_hash`
            # docstring for why the ORIGINAL cancel token can't double as
            # the reinstate one. This becomes the row's new
            # guest_cancel_token_hash too, so it doubles as this booking's
            # ongoing cancel-link token if it's later reinstated.
            reinstate_token = new_token()
            changed = self.store.cancel(
                reg.registration_id, canceled_by="guest", host_message=message,
                reinstate_token_hash=hash_token(reinstate_token),
            )
            if changed:
                self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
                if course:
                    user = self.store.find_user_by_id(reg.user_id)
                    # Both sides notified, same as every other cancellation path
                    # (see _send_cancellation_emails) -- this guest already knows
                    # they just did this, but the email is still their own copy
                    # of "here's what got canceled and when", same as the other
                    # two paths get.
                    self._send_cancellation_emails(
                        course, reg.occurrence_date, user, canceled_by="guest", message=message,
                        registration_id=reg.registration_id, reinstate_token=reinstate_token,
                    )
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Your booking has been canceled.</p>")
        # 2026-07-09, the operator: "This page should look like as described for
        # the admin and like the email ... WHAT WHEN WHERE with emojis and
        # bold font for the keyword followed by the description ... And
        # THEN a bit of space and the optional reason and the button as it
        # is." Same _course_recap_html() every other cancel-confirmation
        # page (admin_cancel/host_cancel) and the booking-confirmation page
        # use -- see that function's own docstring on why it's shared.
        body = (
            "<p>Cancel your booking?</p>"
            + _course_recap_html(course, reg.occurrence_date)
            + """<form method="post" class="card">
          <label>Reason <span class="opt">(optional)</span>
            <textarea name="message" rows="2" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Yes, cancel it</button>
            <a href="/" class="link-button">Never mind</a></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Cancel booking", body)

    def guest_reinstate(self, method: str, token: str, environ):
        """No-login "magic link" twin of my_reinstate(), reachable straight
        from the cancellation email (2026-07-10: "for /my and /admin ...
        this POPUP should be used ... Only from the email there will be a
        single page for this ... WHAT, WHEN, WHERE like in the confirmation
        email") -- same page shape as guest_cancel() above (recap, then an
        optional message field, then a confirm button), same trust model
        (a hashed, single-purpose bearer token in the URL, just like
        `/cancel/<token>`), but looked up against a currently-CANCELED row
        instead of a CONFIRMED/WAITLISTED one (Store.find_canceled_by_guest_token_hash).

        `token` is NOT the guest's original cancel-link token -- it's a
        fresh one minted at the moment of THIS booking's most recent
        cancellation (see Store.cancel()'s own `reinstate_token_hash`
        param for why) -- so this link only ever works for the specific
        cancellation email it was sent in, and stops working the moment
        the booking is reinstated by ANY path (dialog or this link) or
        canceled again (a new cancellation mints yet another fresh one)."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        reg = self.store.find_canceled_by_guest_token_hash(hash_token(token))
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page(
                "Not found", "<p>This link is invalid or already used.</p>"
            )
        course = self.settings.course(reg.course_shortname)
        if course is None or date.fromisoformat(reg.occurrence_date) < datetime.now(timezone.utc).date():
            return "200 OK", [("Content-Type", "text/html")], page(
                "Not found", "<p>This booking can no longer be rebooked.</p>"
            )
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            updated = self.store.reinstate(reg.registration_id, course.capacity)
            if updated is not None:
                self._sync(reg.course_shortname, date.fromisoformat(reg.occurrence_date))
                user = self.store.find_user_by_id(reg.user_id)
                # Both sides notified, always -- same standing default as
                # every other registration-status email.
                self._send_reinstatement_emails(
                    course, reg.occurrence_date, user,
                    confirmed=(updated.status == STATUS_CONFIRMED), reinstated_by="guest", message=message,
                )
            return "200 OK", [("Content-Type", "text/html")], page("Rebooked", "<p>Your booking has been rebooked.</p>")
        # 2026-07-14, the operator: "Please try find a simpler more intuitive word
        # than reinstate" -- "Rebook" picked; see cancellation.py's own
        # note on this same rename for the full scoping (visible text
        # only, routes/functions/params unchanged).
        body = (
            "<p>Rebook your booking?</p>"
            + _course_recap_html(course, reg.occurrence_date)
            + """<form method="post" class="card">
          <label>Optional message <span class="opt">(optional)</span>
            <textarea name="message" rows="2" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Yes, rebook it</button>
            <a href="/" class="link-button">Never mind</a></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Rebook booking", body)

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
        bookings" and "Log out".

        2026-07-10, the operator: caught (via a real external-IP test) that this
        page worked completely normally during maintenance mode -- see
        _maintenance_guard()'s own docstring for the fix; this is the guard
        that blocks it now, checked before even looking at the session, so
        an already-logged-in guest sees the maintenance page too, not their
        real bookings, unless they're the recognized bypass IP."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
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
                time_range = course.weekday_time_range_label() if course else ""
                location = course.location if course else ""
                # 2026-07-07, the operator: "Please make the 'Course' string a link
                # to the course booking page." Only linkable if the course
                # still exists in settings.toml (course is None for an old
                # booking whose course was since removed -- nothing to link
                # to in that case, same fallback as title/time_range/location
                # above).
                course_cell = f'<a href="/book/{esc(r.course_shortname)}">{esc(title)}</a>' if course else esc(title)
                # 2026-07-09, the operator: "add a location_url and then use it on
                # /my in the column location to make those clickable" --
                # same "only if we actually have something to link to"
                # fallback as course_cell above: no course.location_url set
                # (the field's own default) or no course at all just falls
                # back to the plain text, exactly like today. target="_blank"
                # so following a map link never navigates away from /my
                # itself (same as the participation-terms link on /book).
                location_cell = (
                    f'<a href="{esc(course.location_url)}" target="_blank" rel="noopener">{esc(location)}</a>'
                    if course and course.location_url else esc(location)
                )
                cancel_id = f"cancel-{esc(r.registration_id)}"
                # Confirmed or waitlisted are the only cancelable states --
                # this used to only allow CONFIRMED, which silently made it
                # impossible to leave the waitlist from this page (the
                # emailed cancel link and /admin could always do both;
                # caught 2026-07-05 while touching this code for the
                # cancel-dialog/both-sides-notification consistency pass).
                # 2026-07-08, the operator (screenshot of /admin?past=1 showing an
                # enabled Cancel button on a 2025-10-18 row): "PAST bookings
                # should NOT have a CANCEL button as well" -- a session that
                # already happened can't be un-happened; also require
                # occurrence_date >= today, same future-only gate Reinstate
                # already uses just below. Applies here too, not just
                # /admin ("what I tell you about /admin should of course
                # also apply to /my").
                disabled = r.status not in (STATUS_CONFIRMED, STATUS_WAITLISTED) or (
                    date.fromisoformat(r.occurrence_date) < today
                )
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
                # Reinstate ("undo the cancel"): 2026-07-10, the operator: "there
                # should be then a reschedule button for canceled meetings
                # which time (WHEN) is in the future" -- offered here for
                # any of the guest's OWN canceled rows whose occurrence
                # hasn't happened yet (a past occurrence has nothing left
                # to reinstate INTO). Same confirm-dialog-with-optional-
                # message pattern as Cancel above (2026-07-10: "Reinstate
                # should, LIKE CANCEL, also ask for a COMMENT to be sent
                # with the email to the other") -- reuses the same
                # _DIALOG_WIRING_SCRIPT already loaded on this page.
                #
                # 2026-07-14, the operator (screenshot of a "Canceled by host" row
                # still showing this button): "a meeting that was canceled
                # by HOST should NOT have a reinstate button." Only
                # STATUS_CANCELED_BY_GUEST now -- a HOST cancellation means
                # the session itself isn't happening (illness, venue
                # unavailable, ...), which a guest un-canceling themselves
                # can't undo; same reasoning already applied to the
                # cancellation EMAIL's own reinstate link 2026-07-13 (see
                # app.cancellation.send_cancellation_emails's own "any
                # cancellation the HOST initiates ... doesn't need the
                # link" note) -- this closes the same gap on the /my page
                # itself, which that email-only fix missed. See
                # my_reinstate()'s own matching server-side guard below.
                if r.status == STATUS_CANCELED_BY_GUEST and (
                    date.fromisoformat(r.occurrence_date) >= today
                ):
                    reinstate_id = f"reinstate-{esc(r.registration_id)}"
                    actions += (
                        f'<form method="post" action="/my/reinstate/{esc(r.registration_id)}" id="{reinstate_id}-form">'
                        f'<button type="submit" class="confirm-dialog-btn" data-dialog="{reinstate_id}-dialog">'
                        "Rebook</button>"
                        "</form>"
                        f'<dialog id="{reinstate_id}-dialog" class="card">'
                        f"<p><b>Are you sure?</b></p>"
                        f"<p>Rebook your booking for <b>{esc(title)}</b> on {esc(r.occurrence_date)}?</p>"
                        f'<label>Optional message <textarea name="message" rows="2" class="big-input" '
                        f'form="{reinstate_id}-form"></textarea></label>'
                        '<div class="submit-row">'
                        f'<button type="submit" form="{reinstate_id}-form">Confirm rebooking</button> '
                        f'<button type="button" class="dialog-close-btn" data-dialog="{reinstate_id}-dialog">Never mind</button>'
                        "</div></dialog>"
                    )
                return (
                    f'<tr><td>{course_cell}</td><td class="nowrap">{esc(r.occurrence_date)}</td>'
                    f"<td>{esc(time_range)}</td><td>{location_cell}</td>"
                    f"<td>{esc(status_label(r.status))}</td>"
                    f"<td>{actions}</td></tr>"
                )

            def _table(table_id: str, regs_for_table: list, default_sort_dir: str = "asc") -> str:
                if not regs_for_table:
                    return ""
                rows = "".join(_row(r) for r in regs_for_table)
                return f"""
                <div class="table-tools">
                  <input type="search" id="{table_id}-filter" class="big-input id-input" placeholder="Filter bookings...">
                </div>
                <table id="{table_id}" border="1" cellpadding="6">
                  <thead><tr>
                    <th>Course<span class="sort-indicator"></span></th>
                    <th data-default-sort="{default_sort_dir}">Date<span class="sort-indicator"></span></th>
                    <th>Time<span class="sort-indicator"></span></th>
                    <th>Location<span class="sort-indicator"></span></th>
                    <th>Status<span class="sort-indicator"></span></th>
                    <th>Actions<span class="sort-indicator"></span></th>
                  </tr></thead>
                  <tbody>{rows}</tbody>
                </table>""" + _SORTABLE_FILTERABLE_TABLE_SCRIPT

            upcoming_id, past_id = "my-upcoming-table", "my-past-table"
            upcoming_html = _table(upcoming_id, upcoming) or "<p>You have no upcoming bookings.</p>"
            past_html = _table(past_id, past, default_sort_dir="desc") or "<p>You have no past bookings.</p>"
            body = f"""
            <div class="submit-row">
              <a href="/courses"><button type="button">New booking</button></a>
              <a href="/my/settings"><button type="button">Account settings</button></a>
            </div>
            <h3>Upcoming</h3>
            {upcoming_html}
            <h3>Past (most recent {MY_PAST_BOOKINGS_LIMIT})</h3>
            {past_html}""" + _DIALOG_WIRING_SCRIPT
            # 2026-07-14, the operator: "please move the delete button under
            # 'Account settings': and rename to 'DELETE this account'" --
            # the delete-account form/dialog used to live at the bottom of
            # THIS page; it's now rendered by _my_settings_page() instead
            # (see that method). _DIALOG_WIRING_SCRIPT stays appended here
            # regardless -- the Cancel/Reinstate confirm dialogs above
            # (per-row, built in _row()) still need it on this same page.
            return (
                "200 OK", [("Content-Type", "text/html")],
                page("My bookings", body, banner=self._session_banner_html(environ, on_my_page=True)),
            )

        error = None
        lockout_seconds = 0.0
        if method == "POST":
            form = self._read_form(environ)
            email, password = form.get("email", "").strip(), form.get("password", "").strip()
            # Carried along as a hidden field by _my_login_page()'s login
            # form -- see _safe_next_path()'s own docstring for why this is
            # re-validated here rather than trusted as-is (a POST body is
            # just as hand-editable as a URL).
            next_path = _safe_next_path(form.get("next", ""))
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
                    # 2026-07-11, the operator ("Why do I get this error? I did
                    # NOT several login attempts!"): a SUCCESSFUL login used
                    # to still count against this same 5/hour budget (allow()
                    # is called unconditionally above, before the password
                    # is even checked) with nothing ever resetting it --
                    # several perfectly legitimate logins within an hour
                    # (exactly what testing/normal use looks like) could
                    # exhaust it on their own, with no wrong password ever
                    # entered. Only a WRONG password should cost anything
                    # against this limiter; a right one clears the slate.
                    login_limiter.reset(key)
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
                        # See the admin branch's own 2026-07-11 comment above
                        # -- a successful login shouldn't cost anything
                        # against this budget, only a wrong password should.
                        login_limiter.reset(key)
                        sid = _new_session({"kind": "guest", "user_id": user.user_id})
                        self.store.touch_login(user.user_id)
                        # 2026-07-11, the operator: "Login link returns to
                        # originating page" -- lands back on /courses or
                        # /book/<shortname> if that's where the guest
                        # clicked Login from, /my otherwise (unchanged
                        # default).
                        return (
                            "302 Found",
                            [("Location", next_path or "/my"), ("Set-Cookie", _session_cookie_header(sid))],
                            "",
                        )
                    error = "Email and/or password did not match."
        else:
            next_path = _safe_next_path(parse_qs(environ.get("QUERY_STRING", "")).get("next", [""])[0])
        return self._my_login_page(login_error=error, login_lockout_seconds=lockout_seconds, next_path=next_path)

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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
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
        next_path: str = "",
    ):
        """Renders /my's logged-out page: two CSS-only tabs (radio buttons
        + sibling selectors -- no JS needed to switch between them)
        labeled "Login" (default) and "Sign up" (2026-07-06). Both my()
        (GET/POST login) and my_signup() (POST) render through this one
        function so the two tabs' markup can't drift apart, and so a
        failed submission re-opens on the SAME tab the guest was using
        (via active_tab) instead of silently flipping back to Login.

        2026-07-10, the operator (screenshot of /my's anonymous login page): "we
        miss a back to https://booking.example.org here" -- this page deliberately
        has no _session_banner_html() banner at all (see that method's own
        docstring: a "Login" banner sitting above a login FORM would be
        redundant), which meant it was the one page in the app with no way
        back to the marketing homepage short of editing the URL by hand.
        Fixed with a plain "Back to {site}" link, same wording convention
        as the other "Back to ..." links already used elsewhere on /my
        (e.g. my_settings()'s "Back to my bookings")."""
        login_checked = "checked" if active_tab == "login" else ""
        signup_checked = "checked" if active_tab == "signup" else ""

        login_err_html = f'<p class="err">{esc(login_error)}</p>' if login_error else ""
        login_label = "Login"
        # 2026-07-11, the operator: "Login link returns to originating page" --
        # carried through as a hidden field so App.my()'s POST handler can
        # redirect back to it on success; see _safe_next_path()'s own
        # docstring for why this is re-validated server-side rather than
        # trusted just because it round-tripped through this form.
        next_field = f'<input type="hidden" name="next" value="{esc(next_path)}">' if next_path else ""
        login_body = f"""{login_err_html}<form method="post" action="/my" class="card">
          {next_field}
          <label>Email <input class="big-input id-input" name="email" type="text" required></label>
          <label>Password <input class="big-input id-input" name="password" type="password" required></label>
          <div class="submit-row"><button type="submit" id="my-login-btn"{
              f' data-lockout-btn data-lockout-seconds="{int(login_lockout_seconds)}"' if login_lockout_seconds else ""
            }>{esc(login_label)}</button></div>
        </form>
        <p><a href="/my/reset">Forgot your password, or still need to confirm your account?</a></p>"""
        if login_lockout_seconds:
            login_body += _LOCKOUT_COUNTDOWN_SCRIPT

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
              <label>Name <input class="big-input id-input" name="name" type="text" required></label>
              <label>Email <input class="big-input id-input" name="email" type="email" required></label>
              <p class="hint">We'll email you a link to set your password.</p>
              <div class="submit-row"><button type="submit" id="my-signup-btn"{
                  f' data-lockout-btn data-lockout-seconds="{int(signup_lockout_seconds)}"' if signup_lockout_seconds else ""
                }>{esc(signup_label)}</button></div>
            </form>"""
            if signup_lockout_seconds:
                signup_body += _LOCKOUT_COUNTDOWN_SCRIPT

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
        </div>
        <p><a href="{esc(self.settings.base_url)}">Back to {esc(self._site_label())}</a></p>"""
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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
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
          <label>Email <input class="big-input id-input" name="email" type="email" required></label>
          <div class="submit-row"><button type="submit" id="reset-btn" data-resend-cooldown-btn{
              f' data-lockout-btn data-lockout-seconds="{int(lockout_seconds)}"' if lockout_seconds else ""
            }>{esc(reset_label)}</button></div>
        </form>""" + _RESEND_COOLDOWN_SCRIPT
        if lockout_seconds:
            body += _LOCKOUT_COUNTDOWN_SCRIPT
        return "200 OK", [("Content-Type", "text/html")], page("Forgot your password?", body)

    def _set_password_form(self, token: str) -> str:
        return f"""<form method="post" class="card">
          <label>New password <span class="req">(required)</span>
            <input class="big-input id-input" name="password" type="password"
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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
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
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        reg = self.store.find_by_id(registration_id)
        if reg and reg.user_id == session["user_id"]:
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            # Guarded on cancel()'s return value (2026-07-10 fix, same as
            # admin_cancel()/host_cancel()) -- this already redirects to
            # /my afterward, but a stale cached copy of /my (browser
            # back-button, or a double-click before the redirect lands)
            # could still resubmit this exact POST for an already-canceled
            # registration_id; without this guard that would silently
            # re-run the waitlist-promotion attempt and send a second round
            # of "canceled" emails to both sides.
            reinstate_token = new_token()
            changed = self.store.cancel(
                registration_id, canceled_by="guest", host_message=message,
                reinstate_token_hash=hash_token(reinstate_token),
            )
            if changed:
                self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
                course = self.settings.course(reg.course_shortname)
                if course:
                    user = self.store.find_user_by_id(session["user_id"])
                    # Both sides notified, always -- see _send_cancellation_emails
                    # (standing default now, SOLUTION-DESIGN.md). This is what
                    # lets the real account owner notice a cancellation made by
                    # someone who got into their /my session but isn't them.
                    self._send_cancellation_emails(
                        course, reg.occurrence_date, user, canceled_by="guest", message=message,
                        registration_id=registration_id, reinstate_token=reinstate_token,
                    )
        return "302 Found", [("Location", "/my")], ""

    def my_reinstate(self, method: str, registration_id: str, environ):
        """Undo a cancellation for one of the guest's own bookings, as long
        as the occurrence is still in the future -- see my()'s own
        `_row()` for the confirm-dialog button that's gated the same way,
        and Store.reinstate()'s docstring for what "undo" means here (same
        occurrence, re-decided confirmed-vs-waitlisted from CURRENT
        capacity; never a move to a different date). The future-date check
        is re-done here, not just trusted from the page: my()'s button is
        already hidden for a past occurrence, but a crafted/replayed POST
        could still hit this route directly. Same reasoning for the
        guest-canceled-only check below (2026-07-14, the operator: "a meeting
        that was canceled by HOST should NOT have a reinstate button") --
        my()'s button is already hidden for a host-canceled row too, but
        this re-checks it server-side for the same crafted/replayed-POST
        reason.

        Optional `message` (2026-07-10, the operator: "Reinstate should, LIKE
        CANCEL, also ask for a COMMENT to be sent with the email to the
        other") is collected by the same dialog+textarea pattern Cancel
        uses, and passed straight through to
        _send_reinstatement_emails() -- see that function's docstring."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        reg = self.store.find_by_id(registration_id)
        if reg and reg.user_id == session["user_id"] and reg.status == STATUS_CANCELED_BY_GUEST:
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            course = self.settings.course(reg.course_shortname)
            if course and date.fromisoformat(reg.occurrence_date) >= datetime.now(timezone.utc).date():
                updated = self.store.reinstate(registration_id, course.capacity)
                if updated is not None:
                    self._sync(reg.course_shortname, date.fromisoformat(reg.occurrence_date))
                    user = self.store.find_user_by_id(session["user_id"])
                    # Both sides notified, always -- same standing default
                    # as every other registration-status email (see
                    # _send_cancellation_emails above).
                    self._send_reinstatement_emails(
                        course, reg.occurrence_date, user,
                        confirmed=(updated.status == STATUS_CONFIRMED), reinstated_by="guest", message=message,
                    )
        return "302 Found", [("Location", "/my")], ""

    def my_logout(self, method: str, environ):
        # Deliberately NOT behind _maintenance_guard -- same reasoning as
        # my_session_status(): logging out isn't a booking or management
        # action, it's the opposite (a teardown), so blocking it during
        # maintenance would only leave a guest stuck "logged in" against
        # their wishes for no real benefit.
        #
        # Redirects to the homepage (settings.base_url), not "/my"
        # (2026-07-11, the operator: "pressing logout should bring you back to
        # https://booking.example.org"). This is the ONE logout form
        # (_session_banner_html()'s own, action="/my/logout") shared by
        # every page that shows it -- the static homepage's own JS-rendered
        # copy (site/index.html), /courses, /book/<shortname>, and /my
        # itself -- so the old "/my" target was most jarring from the
        # homepage: logging out there used to jump straight into the app's
        # /my login page instead of staying on the site you were just on.
        session = _get_session(environ)
        if session and session.get("kind") == "guest":
            SESSIONS.pop(session["_sid"], None)
        return (
            "302 Found",
            [("Location", self.settings.base_url), ("Set-Cookie", _session_cookie_header("", clear=True))],
            "",
        )

    def my_delete_account(self, method: str, environ):
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
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

    # -- /internal/status --------------------------------------------------------

    def internal_status(self, method: str, environ):
        """GET-only JSON diagnostic dump for `my-bt status` -- NOT for
        browsers/guests, and deliberately not linked from anywhere. See
        the module docstring's "GET /internal/status" section for the
        trust model (rejects anything carrying X-Forwarded-For, i.e.
        anything that arrived via nginx rather than a direct localhost
        connection).

        "Currently logged in" (2026-07-13, the operator: "logged in means:
        unexpired sessions") is exactly SESSIONS filtered to
        expires > now -- no extra recency window. Each entry's
        "connected_since" is expires - SESSION_TTL_SECONDS (see
        _record_page_view's docstring for why that's exact, not
        approximate); "last_page"/"last_seen" reflect the most recent
        request _record_page_view saw for that session, and are None
        for a session that's never made a second request yet (i.e. the
        one that just logged in).

        A "guest" session's `who` is resolved to the account's current
        email via self.store (SESSIONS only ever stores user_id, which
        is stable across an email change -- see my_settings_email());
        falls back to a raw user_id string in the (should be impossible)
        case the account was erased out from under a still-live session.
        An "admin" session has no associated user record at all -- there
        is exactly one admin login, gated by settings.toml's
        admin_password_hash, not a per-person account."""
        if method != "GET":
            return "405 Method Not Allowed", [("Content-Type", "text/plain")], "GET only"
        if environ.get("HTTP_X_FORWARDED_FOR"):
            # Arrived via nginx (which always sets this -- see _client_ip's
            # docstring) rather than a direct localhost connection from
            # my-bt itself. Refuse rather than risk this ever becoming
            # reachable from outside this host, even if a future nginx
            # config change accidentally proxied this path.
            return "403 Forbidden", [("Content-Type", "text/plain")], "internal endpoint -- direct localhost access only"
        now = time.time()
        sessions_out = []
        for sid, data in list(SESSIONS.items()):
            if data.get("expires", 0) <= now:
                continue
            kind = data.get("kind", "?")
            if kind == "guest":
                user = self.store.find_user_by_id(data.get("user_id", ""))
                who = user.email if user else f"(erased) user_id={data.get('user_id')}"
            else:
                who = "admin"
            last_seen = data.get("last_seen")
            sessions_out.append({
                "kind": kind,
                "who": who,
                "connected_since": datetime.fromtimestamp(
                    data["expires"] - SESSION_TTL_SECONDS, tz=timezone.utc
                ).isoformat(),
                "expires_at": datetime.fromtimestamp(data["expires"], tz=timezone.utc).isoformat(),
                "last_page": data.get("last_page"),
                "last_seen": datetime.fromtimestamp(last_seen, tz=timezone.utc).isoformat() if last_seen else None,
            })
        state = maintenance.read_state(self.store.data_dir)
        payload = {
            "server_time": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "version": PACKAGE_VERSION,
            "maintenance": {"enabled": state.enabled, "message": state.message, "set_at": state.set_at},
            "sessions": sessions_out,
        }
        return "200 OK", [("Content-Type", "application/json")], json.dumps(payload)

    # -- /my/settings -----------------------------------------------------------
    #
    # 2026-07-10: a guest's own name is fixed at signup/first-booking time,
    # and there was previously no way to change it or the login email
    # without deleting the whole account. Name changes take effect
    # immediately (nothing sensitive). Email changes are a two-step,
    # dual-address-notified flow -- see my_settings_email()/
    # _send_email_change_emails() below -- because the login email IS the
    # account identifier: changing it silently, or without the account's
    # ORIGINAL owner ever finding out, would be a much bigger deal than a
    # display-name edit.

    def _confirm_email_expired(self, user) -> bool:
        """See _confirm_token_expired's own docstring -- identical shape,
        just checking pending_email_token_created_at/CONFIRM_TOKEN_TTL_HOURS
        instead. Reusing the same TTL constant rather than inventing a
        second one: there's no reason an email-change link should live
        longer or shorter than an account-confirmation link."""
        if not user.pending_email_token_created_at:
            return False
        created = datetime.fromisoformat(user.pending_email_token_created_at)
        return datetime.now(timezone.utc) - created > timedelta(hours=CONFIRM_TOKEN_TTL_HOURS)

    def _send_email_change_emails(self, user, new_email: str, token: str, cancel_token: str) -> None:
        """Sent the moment a change is REQUESTED (not yet confirmed) -- one
        to the NEW address (the only one that can actually confirm, via
        the link below) and one to the CURRENT address (a no-login
        "cancel this" link, no way to confirm from there -- see below), so
        the real account owner notices if they didn't want this. Mirrors
        _send_confirm_email's tone/expiry wording. See
        _send_email_change_confirmed_emails for the matching final-
        confirmation pair, sent once the link below is actually clicked
        (in my_confirm_email).

        2026-07-11, the operator (screenshot of the current-address copy): "This
        sentance is not nice to read. Yes the change was done from this
        email... but the important info here is that the login should
        change from test *TO* fred." -- the old wording ("from this
        address to fred@example.org") made the reader work out that "this
        address" meant their own inbox; both emails below now spell out
        the FROM and TO addresses explicitly instead of leaning on a
        self-reference, for the same reason on both copies.

        Also (same round): "it is not relevant if the person did the
        change... he/she could have asked someone... important is if they
        are OK with this!" -- both emails used to ask "did YOU request
        this" (implying only the account owner's own click counts), which
        doesn't hold up if someone else made the change on their behalf,
        with their knowledge, at their own ask. Reworded to ask whether the
        change itself is welcome/expected, not who physically clicked
        anything.

        `cancel_token` (2026-07-11, same round: "Please provide a link
        without login") is the plaintext of a token FRESHLY MINTED by the
        caller (my_settings_email), separate from `token` above -- see
        User.pending_email_cancel_token_hash's own docstring for why the
        confirm token can't double as this one. Builds the current
        address's own no-login `/my/cancel-email-change/<token>` link,
        replacing the old `/my/settings` link that needed a session."""
        site = self._site_label()
        confirm_url = f"{self.settings.base_url}/my/confirm-email/{token}"
        cancel_url = f"{self.settings.base_url}/my/cancel-email-change/{cancel_token}"
        send_mail(
            self.settings, new_email, f"Confirm your new email for your {site} account",
            render_template(
                load_email_template(self.settings, "email_change_new.txt"),
                site=site, old_email=user.email, new_email=new_email, confirm_url=confirm_url,
                ttl_hours=str(CONFIRM_TOKEN_TTL_HOURS),
            ),
            bcc_addrs=self.settings.bcc_attendee_email_list,
        )
        send_mail(
            self.settings, user.email, f"Email change requested for your {site} account",
            render_template(
                load_email_template(self.settings, "email_change_current.txt"),
                site=site, old_email=user.email, new_email=new_email, cancel_url=cancel_url,
            ),
            bcc_addrs=self.settings.bcc_attendee_email_list,
        )

    def _send_email_change_confirmed_emails(self, old_email: str, new_email: str) -> None:
        """Sent once (in my_confirm_email, right after Store.apply_pending_email
        actually swaps the email) -- to BOTH the new (now active) and old
        (now removed) addresses, so neither side is left guessing which
        one won. Deliberately plain-text only (no ics/html) -- this is a
        one-line account notice, not a booking."""
        site = self._site_label()
        send_mail(
            self.settings, new_email, f"Your {site} login email is now confirmed",
            render_template(
                load_email_template(self.settings, "email_change_confirmed_new.txt"),
                site=site, new_email=new_email,
            ),
            bcc_addrs=self.settings.bcc_attendee_email_list,
        )
        send_mail(
            self.settings, old_email, f"Your {site} login email has changed",
            render_template(
                load_email_template(self.settings, "email_change_confirmed_old.txt"),
                site=site, new_email=new_email,
            ),
            bcc_addrs=self.settings.bcc_attendee_email_list,
        )

    def _my_settings_page(
        self, environ, user, *, name_error: str | None = None,
        email_error: str | None = None,
    ):
        banner = self._session_banner_html(environ, on_my_page=True)
        name_err_html = f'<p class="err">{esc(name_error)}</p>' if name_error else ""
        name_body = f"""{name_err_html}<form method="post" action="/my/settings/name" class="card">
          <label>Name <input class="big-input id-input" name="name" type="text" value="{esc(user.name)}" required></label>
          <div class="submit-row"><button type="submit">Save name</button></div>
        </form>"""

        # email_err_html is rendered above EITHER branch below (not just
        # the request-form one) -- a rate-limit hit can occur on an
        # account that already has a pending change (this handler's own
        # `user` reflects state as of just before that blocked attempt),
        # and the error would otherwise be silently dropped by the
        # pending-change branch, which has no error slot of its own.
        email_err_html = f'<p class="err">{esc(email_error)}</p>' if email_error else ""
        if user.pending_email:
            # 2026-07-11, the operator (screenshot of this exact card): "Please use
            # same font-size for both text lines above the button" -- the
            # second line used to be class="hint" (smaller, grey -- see
            # templates.py's .hint rule), which read as visually secondary
            # to the first even though both are equally important status
            # text here (not a hint/aside the way e.g. the "We'll email a
            # confirmation link..." line in the OTHER branch below genuinely
            # is). Plain <p>, same as the first line, for both now.
            email_body = f"""{email_err_html}<div class="card">
              <p>Email change pending: <b>{esc(user.email)}</b> &rarr; <b>{esc(user.pending_email)}</b></p>
              <p>Check <b>{esc(user.pending_email)}</b> for a confirmation link
                (expires {CONFIRM_TOKEN_TTL_HOURS}h after it was sent).</p>
              <form method="post" action="/my/settings/email/cancel">
                <div class="submit-row"><button type="submit">Cancel this change</button></div>
              </form>
            </div>"""
        else:
            email_body = f"""{email_err_html}<form method="post" action="/my/settings/email" class="card">
              <label>Current email <input class="big-input id-input" value="{esc(user.email)}" disabled></label>
              <label>New email <input class="big-input id-input" name="email" type="email" required></label>
              <p class="hint">We'll email a confirmation link to the new address -- your login
                email only changes once that link is clicked.</p>
              <div class="submit-row"><button type="submit">Change email</button></div>
            </form>"""

        body = f"""
        <h3>Name</h3>
        {name_body}
        <h3>Email</h3>
        {email_body}
        <p><a href="/my">Back to my bookings</a></p>
        <div class="submit-row">
          <form method="post" action="/my/delete-account" style="display:inline" id="delete-account-form"
            onsubmit="return confirm('Delete your account and all related data? This will cancel any booking you still have!');">
            <button type="submit" class="confirm-dialog-btn" data-dialog="delete-account-dialog">DELETE this account</button>
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
        # 2026-07-14, the operator: "please move the delete button under 'Account
        # settings': and rename to 'DELETE this account'" -- moved here
        # from the bottom of /my (see my()'s own comment at the same
        # spot). Same form/dialog/confirm() markup as before, just
        # relocated + relabeled.
        return "200 OK", [("Content-Type", "text/html")], page("Account settings", body, banner=banner)

    def my_settings(self, method: str, environ):
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        if method != "GET":
            return "405 Method Not Allowed", [("Content-Type", "text/plain")], "GET only"
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        user = self.store.find_user_by_id(session["user_id"])
        if user is None:
            # Stale session pointing at a since-deleted/erased account --
            # same recovery as _session_banner_html's own stale-session
            # handling: drop the dead cookie rather than 403ing forever.
            SESSIONS.pop(session["_sid"], None)
            return (
                "302 Found",
                [("Location", "/my"), ("Set-Cookie", _session_cookie_header("", clear=True))],
                "",
            )
        return self._my_settings_page(environ, user)

    def my_settings_name(self, method: str, environ):
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        if method != "POST":
            return "302 Found", [("Location", "/my/settings")], ""
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        user = self.store.find_user_by_id(session["user_id"])
        if user is None:
            return "302 Found", [("Location", "/my")], ""
        form = self._read_form(environ)
        name = form.get("name", "").strip()
        if not name:
            return self._my_settings_page(environ, user, name_error="Name can't be empty.")
        self.store.set_name(user.user_id, name)
        return "302 Found", [("Location", "/my/settings")], ""

    def my_settings_email(self, method: str, environ):
        """Requests an email change -- rate-limited per user_id (see
        email_change_limiter's own comment), since unlike /my/reset or
        /my/signup this action is only reachable from an authenticated
        session, so there's no anonymous-probing concern to key on
        email/IP instead. Rejects a target email already in use by a
        DIFFERENT account -- login emails are the account's own unique
        key everywhere else in this app (find_user_by_email), so silently
        allowing two accounts to fight over the same address would break
        that assumption elsewhere (e.g. which account a future booking
        under that email attaches to)."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        if method != "POST":
            return "302 Found", [("Location", "/my/settings")], ""
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        user = self.store.find_user_by_id(session["user_id"])
        if user is None:
            return "302 Found", [("Location", "/my")], ""
        form = self._read_form(environ)
        new_email = form.get("email", "").strip().lower()
        if not new_email or "@" not in new_email:
            return self._my_settings_page(environ, user, email_error="Please enter a valid email address.")
        if new_email == user.email.strip().lower():
            return self._my_settings_page(environ, user, email_error="That's already your current email.")
        other = self.store.find_user_by_email(new_email)
        if other is not None and other.user_id != user.user_id:
            return self._my_settings_page(
                environ, user, email_error="That email is already in use by another account."
            )
        now = time.time()
        key = f"email-change:{user.user_id}"
        if not email_change_limiter.allow(key, now=now):
            log.warning("rate limit blocked: email change for user %s", user.user_id)
            return self._my_settings_page(environ, user, email_error="Too many attempts -- try again later.")
        token = new_token()
        cancel_token = new_token()
        self.store.set_pending_email(
            user.user_id, new_email, hash_token(token), now_iso(),
            cancel_token_hash=hash_token(cancel_token),
        )
        self._send_email_change_emails(user, new_email, token, cancel_token)
        return "302 Found", [("Location", "/my/settings")], ""

    def my_settings_email_cancel(self, method: str, environ):
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        if method != "POST":
            return "302 Found", [("Location", "/my/settings")], ""
        session = _get_session(environ)
        if not session or session.get("kind") != "guest":
            return _login_required_redirect()
        self.store.clear_pending_email(session["user_id"])
        return "302 Found", [("Location", "/my/settings")], ""

    def my_confirm_email(self, method: str, token: str, environ):
        """GET-preview/POST-consume landing page for a requested email
        change (see my_settings_email() above) -- same shape and same
        three-outcome ordering as my_confirm(): expired / superseded-by-
        a-newer-request / generic invalid (see that method's own
        docstring for why this order and wording). Never touches the
        password -- purely swaps which email an account uses; can be
        confirmed from a completely different browser/session than the
        one that requested it (e.g. opening the new inbox on a different
        device).

        2026-07-07, the operator: "Logout user before email is changed (so with
        its old email). Then redirect the user back to login page /my
        with the link please." -- every session for this account (on
        every device/browser, including whichever one is reading this
        very "Email confirmed" page) is invalidated on a successful POST,
        so the next thing anyone does with this account is log in fresh
        under the NEW email -- no stale session left holding the old
        identity. See _invalidate_all_sessions_for_user()."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        token_hash = hash_token(token)
        user = self.store.find_user_by_pending_email_token_hash(token_hash)
        if user is not None and self._confirm_email_expired(user):
            body = (f'<p>This link has expired -- email confirmation links are only valid for '
                     f'{CONFIRM_TOKEN_TTL_HOURS} hours. Request the change again from '
                     '<a href="/my/settings">Account settings</a>.</p>')
            return "200 OK", [("Content-Type", "text/html")], page("Link expired", body)
        if user is None:
            superseded = self.store.find_user_by_prev_pending_email_token_hash(token_hash)
            if superseded is not None:
                body = ('<p>This link has been disabled because a newer email change request '
                        'was already sent -- check your inbox for the latest email.</p>')
                return "200 OK", [("Content-Type", "text/html")], page("Link replaced", body)
            body = '<p>This link is invalid or has already been used.</p>'
            return "200 OK", [("Content-Type", "text/html")], page("Link invalid", body)

        new_email = user.pending_email
        if method == "POST":
            old_email = user.email
            updated = self.store.apply_pending_email(user.user_id)
            if updated is None:
                body = '<p>This link is invalid or has already been used.</p>'
                return "200 OK", [("Content-Type", "text/html")], page("Link invalid", body)
            # the operator: "Logout user before email is changed ... redirect the
            # user back to login page /my" -- kills every session for this
            # account (this browser included), so the link below has to be
            # a fresh login, not a still-logged-in settings page.
            _invalidate_all_sessions_for_user(user.user_id)
            self._send_email_change_confirmed_emails(old_email, new_email)
            body = (f'<p>Your login email is now <b>{esc(new_email)}</b>.</p>'
                    '<p>Please <a href="/my">log in</a> again with your new email.</p>')
            return "200 OK", [("Content-Type", "text/html")], page("Email confirmed", body)

        # 2026-07-11, the operator (screenshot of this exact page): "3x Confirm is
        # 1x too much. Please place the sentance within the box that
        # surrounds the 'Confirm change' button and use the same font
        # size as in the button. When you do, please remove the Confirm
        # and simply ask: 'Change ... ?' instead" -- the page title
        # ("Confirm email change") and the button ("Confirm change") each
        # already say it once; the sentence no longer needs to say it a
        # third time. Moved inside the same <form class="card"> the button
        # lives in (a plain, unclassed <p> -- same 1em as the button, not
        # a smaller/hint-styled aside), and reworded to state the change
        # as a plain question instead of re-confirming it.
        body = ('<form method="post" class="card">'
                f'<p>Change your login email from <b>{esc(user.email)}</b> to '
                f'<b>{esc(new_email)}</b>?</p>'
                '<div class="submit-row"><button type="submit">Confirm change</button>'
                '<a href="/" class="link-button">Never mind</a></div>'
                '</form>')
        return "200 OK", [("Content-Type", "text/html")], page("Confirm email change", body)

    def my_cancel_email_change(self, method: str, token: str, environ):
        """No-login "cancel this pending email change" landing page,
        linked from the CURRENT address's own notification email (see
        _send_email_change_emails -- 2026-07-11, the operator: "Please provide a
        link without login" -- that email's cancel action used to point
        at /my/settings, which needs a session).

        Gated by pending_email_cancel_token_hash, a token completely
        separate from the one the NEW address gets to CONFIRM the change
        (see User.pending_email_cancel_token_hash's own docstring for
        why) -- so possessing this link can only ever abort the change,
        never complete it. Same GET-preview/POST-consume shape as
        guest_cancel()/guest_reinstate(): simply presenting an
        unguessable link is the same trust model every other magic link
        in this app already uses -- deliberately not the my_confirm_email()
        three-way expired/superseded/invalid check above, since there's
        no separate "superseded" state to distinguish here (a second
        email-change request mints a whole new cancel token too, and the
        old one -- like the old confirm token -- simply stops matching
        anything, same as any other invalid link)."""
        guard = self._maintenance_guard(environ)
        if guard:
            return guard
        user = self.store.find_user_by_pending_email_cancel_token_hash(hash_token(token))
        if user is None or not user.pending_email:
            body = '<p>This link is invalid or has already been used.</p>'
            return "200 OK", [("Content-Type", "text/html")], page("Link invalid", body)
        pending_email = user.pending_email
        if method == "POST":
            self.store.clear_pending_email(user.user_id)
            body = (f'<p>The pending change to <b>{esc(pending_email)}</b> has been canceled. '
                    f'Your login email is still <b>{esc(user.email)}</b>.</p>')
            return "200 OK", [("Content-Type", "text/html")], page("Change canceled", body)
        # Same 2026-07-11 fix as my_confirm_email()'s own page -- see that
        # method's comment. Sentence moved inside the button's own box, and
        # states the pending change as a plain fact instead of restating
        # "Cancel" a third time (title + this sentence + button).
        body = ('<form method="post" class="card">'
                f'<p>Pending login email change: <b>{esc(user.email)}</b> &rarr; '
                f'<b>{esc(pending_email)}</b></p>'
                '<div class="submit-row"><button type="submit">Cancel this change</button>'
                '<a href="/" class="link-button">Never mind</a></div>'
                '</form>')
        return "200 OK", [("Content-Type", "text/html")], page("Cancel email change", body)

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
                # 2026-07-11: same fix as my()'s own admin/guest branches --
                # a successful login shouldn't cost anything against this
                # budget, only a wrong password should (see that method's
                # own comment for the full incident this closes).
                login_limiter.reset(key)
                sid = _new_session({"kind": "admin"})
                return "302 Found", [("Location", "/admin"), ("Set-Cookie", _session_cookie_header(sid))], ""
            else:
                error = "Wrong password."
        err_html = f'<p class="err">{esc(error)}</p>' if error else ""
        login_label = "Log in"
        body = f"""{err_html}<form method="post" class="card">
          <label>Admin password <input class="big-input id-input" name="password" type="password" required></label>
          <div class="submit-row"><button type="submit" id="admin-login-btn"{
              f' data-lockout-btn data-lockout-seconds="{int(lockout_seconds)}"' if lockout_seconds else ""
            }>{esc(login_label)}</button></div>
        </form>"""
        if lockout_seconds:
            body += _LOCKOUT_COUNTDOWN_SCRIPT
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
        # 2026-07-10, the operator: "the merge should be automatically done if you
        # also display the history in the /admin page" -- any LIVE guest
        # whose current email hashes to an already-archived (erased)
        # identity shows that pre-erasure registration history alongside
        # their live rows here.
        #
        # 2026-07-13, the operator: "/admin should [be] non-mutating" -- this
        # used to actually rewrite the CSVs on every page load (moving the
        # archived rows for real, same as `my-bt admin dearchive`); now
        # it's a pure display-time merge instead, via the SAME shared
        # helper `my-bt list --all`/`--past` uses (see
        # cli_list.merge_archived_for_display's own docstring) -- nothing
        # on disk changes just from viewing this page anymore. `dearchive`
        # remains the one explicit action that actually persists a merge.
        live_regs = [Registration(**r) for r in self.store.read_registrations(scope="live")]
        archived_regs = [
            Registration(**r) for r in cli_list.merge_archived_for_display(
                self.store, self.settings, self.store.read_users(scope="live"),
                self.store.read_registrations(scope="archived"),
            )
        ]
        all_regs = live_regs + archived_regs
        users_by_id = {u["user_id"]: User(**u) for u in self.store.read_users(scope="all")}
        # "Times booked" counts every registration ever made by this
        # user_id, live or since-canceled, including whatever the auto-merge
        # above just folded in for real -- computed from the same all-scope
        # set rather than Store.times_registered() (which only reads the
        # live CSV), so an erased identity that never got a live rebook
        # (nothing to merge into) still shows its own true historical count
        # rather than dropping to 0 just because its rows live in the
        # archive. Status is deliberately NOT filtered for either count
        # below -- a canceled booking still counts as a real time they were
        # once booked in.
        #
        # 2026-07-08, the operator (screenshot of a guest already showing "9" with
        # sessions still weeks out): "please have the times booked UP TO
        # THIS MOMENT / date (always including of course the current
        # course)", then "actually even better: make it 2/9 ... so that I
        # see the total and also see the current time they joined" -- shows
        # BOTH counts as "up-to-now/total" (e.g. "2/9": 2 sessions actually
        # happened so far, 9 ever booked including future ones). occurrence_
        # date <= today (today's own session counts, not just strictly-past
        # ones) is the same "past" cutoff used everywhere else in this
        # codebase (see app/migrate_simplymeet.py's own docstring on this
        # exact boundary).
        # 2026-07-13: the actual Counter math moved to
        # cli_list.compute_times_booked_counts (dict-based, so `my-bt
        # list`'s own clean default view can share it too -- see that
        # function's own docstring) -- called here on a minimal raw-dict
        # projection of all_regs rather than the Registration objects
        # themselves, since that shared function (also used straight off
        # Store.read_registrations rows in scripts/my-bt) works on dicts.
        raw_all_regs = [
            {"registration_id": r.registration_id, "party_id": r.party_id,
             "invited_by_user_id": r.invited_by_user_id, "user_id": r.user_id,
             "occurrence_date": r.occurrence_date}
            for r in all_regs
        ]
        times_total_by_user, times_upto_now_by_user = cli_list.compute_times_booked_counts(raw_all_regs, today)
        regs = all_regs if show_past else [r for r in live_regs if date.fromisoformat(r.occurrence_date) >= today]
        # 2026-07-08, the operator: "include past should by default show the
        # newest first" -- today-or-future (the default view) stays
        # ascending (soonest upcoming session first, unchanged); toggling
        # "include past" on flips to descending (most recent booking
        # first) instead, same asc-for-upcoming/desc-for-past split as
        # /my's own Upcoming/Past tables (see my()'s _table() calls).
        # data-default-sort below (which drives the on-load indicator --
        # see _SORTABLE_FILTERABLE_TABLE_SCRIPT) must match this, or the
        # arrow would silently lie about which way the rows are ordered.
        regs.sort(key=lambda r: (r.occurrence_date, r.course_shortname), reverse=show_past)
        # Guest bookings (2026-07): "Host (+N guest(s))"/"Guest of <name>"
        # per row -- live AND archived, so an erased party member's row
        # still counts toward the still-live leader's own count. Computed
        # via cli_list.annotate_admin_party_label (2026-07-13, moved out of
        # this method so `my-bt list`'s own clean default view can show the
        # identical column -- see that function's own docstring for the
        # full "guest of Guest" placeholder-fallback history), over the
        # same raw_all_regs projection used for times-booked just above,
        # keyed back to each row by registration_id since that function
        # returns dicts, not Registration objects.
        raw_users_by_id = {uid: {"name": u.name, "email": u.email} for uid, u in users_by_id.items()}
        party_label_by_reg_id = {
            row["registration_id"]: row["party_label"]
            for row in cli_list.annotate_admin_party_label(raw_all_regs, raw_users_by_id)
        }
        # "Cancel entire session" (2026-07-13, the operator): every LIVE row that
        # cancel_flow.cancel_occurrence() would act on, grouped by
        # (course_shortname, occurrence_date) -- used below to (a) show,
        # right in each row's own cancel dialog, exactly who'd be notified
        # if the operator checks "cancel entire session" instead of just
        # this one row, and (b) give the JS wiring (see
        # _CANCEL_ENTIRE_SESSION_SCRIPT) a key to find every sibling
        # checkbox/button sharing the same occurrence. Same
        # CANCELABLE_STATUSES cancel_occurrence() itself filters on, so this
        # can never show a participant here that canceling wouldn't
        # actually reach.
        occurrence_participants: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for r in live_regs:
            if r.status not in CANCELABLE_STATUSES:
                continue
            u = users_by_id.get(r.user_id)
            if u is None or is_erased_email(u.email):
                continue
            occurrence_participants.setdefault((r.course_shortname, r.occurrence_date), []).append((u.name, u.email))
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
            # No more separate "(incl. M pre-erasure)" fold-in needed here
            # (2026-07-10) -- the auto-merge pass at the top of this method
            # already moved any pre-erasure history for a live, re-booked
            # guest into their real registrations before these counts were
            # even computed, so both are already the true totals.
            times_cell = f"{times_upto_now_by_user.get(r.user_id, 0)}/{times_total_by_user.get(r.user_id, 0)}"
            if user and not erased:
                cancel_id = f"admin-cancel-{esc(r.registration_id)}"
                # 2026-07-08, the operator (screenshot of /admin?past=1): "PAST
                # bookings should NOT have a CANCEL button as well :D" --
                # same fix, same reasoning, as my()'s own Cancel button
                # just below.
                #
                # 2026-07-13, the operator: a guest who hasn't yet clicked their
                # account-confirmation email (STATUS_PENDING_CONFIRMATION)
                # needs to be cancelable from here too -- previously they
                # had NO way to be canceled at all, host or guest (see
                # Store.cancel()'s own docstring). Not offered on `my()`'s
                # own guest-facing Cancel button: a pending-confirmation
                # guest has no password yet, so can't reach /my in the
                # first place.
                disabled = r.status not in (
                    STATUS_CONFIRMED, STATUS_WAITLISTED, STATUS_PENDING_CONFIRMATION,
                ) or (
                    date.fromisoformat(r.occurrence_date) < today
                )
                past_field = '<input type="hidden" name="past" value="1">' if show_past else ""
                # "Cancel entire session" (2026-07-13, the operator: "the checkbox
                # ... Cancel ALL reservations for this date ... SHOW who
                # would all then receive this cancel email") -- an
                # occurrence-key-tagged checkbox INSIDE this row's own
                # dialog (same `form="{cancel_id}-form"` trick the message
                # textarea below already uses to submit alongside a form it
                # isn't a DOM child of), plus the full participant list so
                # the operator sees who's affected before ever checking it.
                # Checking it flips admin_cancel()'s own POST handling from
                # "cancel just this row" to "cancel_flow.cancel_occurrence
                # for this row's whole (course, date)" -- see admin_cancel()
                # below. Only offered when this row's OWN Cancel button is
                # enabled: canceling the entire session from a disabled
                # (already-canceled/past) row wouldn't make sense either.
                occurrence_key = f"{r.course_shortname}|{r.occurrence_date}"
                siblings = occurrence_participants.get((r.course_shortname, r.occurrence_date), [])
                entire_session_html = ""
                if not disabled:
                    participant_list = ", ".join(f"{esc(n)} ({esc(e)})" for n, e in siblings)
                    entire_session_html = (
                        f'<label class="cancel-entire-label">'
                        f'<input type="checkbox" name="cancel_entire_session" value="1" '
                        f'form="{cancel_id}-form" class="cancel-entire-checkbox" '
                        f'data-occurrence="{esc(occurrence_key)}"> '
                        f"Cancel the <b>entire session</b> instead -- "
                        f"{len(siblings)} participant(s) will be notified: {participant_list}"
                        "</label>"
                    )
                actions = (
                    f'<form method="post" action="/admin/cancel/{esc(r.registration_id)}" id="{cancel_id}-form">'
                    f'{past_field}'
                    f'<button type="submit" class="confirm-dialog-btn cancel-btn" data-dialog="{cancel_id}-dialog" '
                    f'data-occurrence="{esc(occurrence_key)}" {"disabled" if disabled else ""}>Cancel</button>'
                    "</form>"
                    f'<dialog id="{cancel_id}-dialog" class="card">'
                    f"<p><b>Are you sure?</b></p>"
                    f"<p>Cancel <b>{esc(user.name)}</b> ({esc(user.email)})'s booking for <b>{esc(title)}</b> "
                    f"on {esc(r.occurrence_date)}? They'll be notified by email.</p>"
                    f'<label>Optional message to them <textarea name="message" rows="2" class="big-input" '
                    f'form="{cancel_id}-form"></textarea></label>'
                    f"{entire_session_html}"
                    '<div class="submit-row">'
                    f'<button type="submit" form="{cancel_id}-form">Confirm cancellation</button> '
                    f'<button type="button" class="dialog-close-btn" data-dialog="{cancel_id}-dialog">Never mind</button>'
                    "</div></dialog>"
                )
                # Reinstate ("undo the cancel"), host-side twin of my()'s
                # own button -- 2026-07-10: "ah yes true! (accidental error
                # for the admin could be use case!)". Same future-only
                # gating, and (2026-07-10: "Reinstate should, LIKE CANCEL,
                # also ask for a COMMENT to be sent with the email to the
                # other") the same confirm-dialog-with-optional-message
                # pattern as Cancel above.
                if r.status in (STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST) and (
                    date.fromisoformat(r.occurrence_date) >= today
                ):
                    reinstate_id = f"admin-reinstate-{esc(r.registration_id)}"
                    actions += (
                        f'<form method="post" action="/admin/reinstate/{esc(r.registration_id)}" id="{reinstate_id}-form">'
                        f'{past_field}'
                        f'<button type="submit" class="confirm-dialog-btn" data-dialog="{reinstate_id}-dialog">'
                        "Rebook</button>"
                        "</form>"
                        f'<dialog id="{reinstate_id}-dialog" class="card">'
                        f"<p><b>Are you sure?</b></p>"
                        f"<p>Rebook <b>{esc(user.name)}</b> ({esc(user.email)})'s booking for <b>{esc(title)}</b> "
                        f"on {esc(r.occurrence_date)}? They'll be notified by email.</p>"
                        f'<label>Optional message to them <textarea name="message" rows="2" class="big-input" '
                        f'form="{reinstate_id}-form"></textarea></label>'
                        '<div class="submit-row">'
                        f'<button type="submit" form="{reinstate_id}-form">Confirm rebooking</button> '
                        f'<button type="button" class="dialog-close-btn" data-dialog="{reinstate_id}-dialog">Never mind</button>'
                        "</div></dialog>"
                    )
                # No separate "Merge history" button (2026-07-10) -- see the
                # auto-merge pass at the top of this method's own comment:
                # merging now happens automatically just by loading this
                # page, so there's nothing left for a manual button to do
                # by the time these rows are built.
            else:
                # Archived (erased) or otherwise unresolvable registrations
                # aren't actionable here -- find_by_id() only reads the live
                # CSV, so admin_cancel() couldn't find one of these anyway.
                actions = ""
            # "Guest of <leader>"/"Host (+N guest(s))" -- see
            # cli_list.annotate_admin_party_label's own docstring (this
            # column's full history, including the "guest of Guest"
            # placeholder fallback, now lives there instead of here).
            party_cell = party_label_by_reg_id.get(r.registration_id, "")
            rows.append(
                f"<tr><td>{esc(status_label(r.status))}</td><td>{esc(r.course_shortname)}</td>"
                f'<td class="nowrap">{esc(r.occurrence_date)}</td>{name_cell}{email_cell}'
                # 2026-07-14, the operator: "both date and time should be
                # non-linebreakable" -- format_display_timestamp() now
                # returns a real space between date/time ("2026-07-08
                # 11h49.54"), which is a line-break opportunity in an
                # HTML table cell; nowrap keeps it on one line, same class
                # occurrence_date's own cell already uses above.
                f'<td class="nowrap">{esc(format_display_timestamp(r.registered_at))}</td><td>{esc(times_cell)}</td>'
                f"<td>{esc(party_cell)}</td>"
                f"<td>{actions}</td></tr>"
            )
        toggle = '<a href="/admin">today + future only</a>' if show_past else '<a href="/admin?past=1">include past</a>'
        table_id = "admin-overview-table"
        body = f"""<p>{toggle}</p>
        <div class="table-tools">
          <input type="search" id="{table_id}-filter" class="big-input id-input" placeholder="Filter...">
        </div>
        <table id="{table_id}" border="1" cellpadding="6">
        <thead><tr>
          <th>Status<span class="sort-indicator"></span></th>
          <th>Course<span class="sort-indicator"></span></th>
          <th data-default-sort="{'desc' if show_past else 'asc'}">Date<span class="sort-indicator"></span></th>
          <th>Name<span class="sort-indicator"></span></th>
          <th>Email<span class="sort-indicator"></span></th>
          <th>Registered<span class="sort-indicator"></span></th>
          <th>Times booked<span class="sort-indicator"></span><span class="th-note">for now / total</span></th>
          <th>Guests<span class="sort-indicator"></span></th>
          <th>Actions<span class="sort-indicator"></span></th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody></table>""" + (
            _SORTABLE_FILTERABLE_TABLE_SCRIPT + _DIALOG_WIRING_SCRIPT + _CANCEL_ENTIRE_SESSION_SCRIPT
        )
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
            # changed is False if this exact registration was already
            # canceled by the time this POST landed (2026-07-10 fix, real
            # incident: the admin table's own Cancel button submits this
            # POST directly and used to stay on this exact URL afterward --
            # no redirect -- so a browser back-button + resubmit, or a
            # double-click, replayed the promote/email side effects below a
            # SECOND time for a booking that was already canceled, sending
            # duplicate "canceled" emails to both sides even though
            # Store.cancel() itself is idempotent about the row. Guarding on
            # its return value, not just calling it, is what actually stops
            # the duplicate side effects; the redirect below (instead of
            # rendering "Canceled" on this same POST URL) additionally
            # closes the back-button-resubmit path itself at the source.
            # "Cancel entire session" (2026-07-13, the operator: the /admin
            # checkbox on each row -- see admin_overview()'s own row-
            # rendering comment) -- the checkbox submits alongside THIS
            # row's own form (via its `form="{cancel_id}-form"` attribute),
            # so when checked, cancel the WHOLE (course, date) this row
            # belongs to via cancel_flow.cancel_occurrence instead of just
            # this one registration_id. Reuses the exact same route/dialog/
            # message field -- no separate URL needed, same as the
            # single-cancel path just below.
            if form.get("cancel_entire_session") == "1":
                self._cancel_occurrence(reg.course_shortname, reg.occurrence_date, message=message)
                location = "/admin?past=1" if form.get("past") == "1" else "/admin"
                return "302 Found", [("Location", location)], ""
            reinstate_token = new_token()
            changed = self.store.cancel(
                registration_id, canceled_by="host", host_message=message,
                reinstate_token_hash=hash_token(reinstate_token),
            )
            if changed:
                self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
                if course:
                    # Both sides notified, always -- see _send_cancellation_emails
                    # (standing default now, SOLUTION-DESIGN.md). The admin's own
                    # copy is what surfaces an unexpected cancellation if someone
                    # other than you got into /admin and did this.
                    self._send_cancellation_emails(
                        course, reg.occurrence_date, user, canceled_by="host", message=message,
                        registration_id=registration_id, reinstate_token=reinstate_token,
                    )
            location = "/admin?past=1" if form.get("past") == "1" else "/admin"
            return "302 Found", [("Location", location)], ""
        # Same recap + "space, then reason, then button" layout as
        # guest_cancel()/host_cancel() -- see host_cancel()'s docstring for
        # the full "Can be always the same code" rationale.
        recap = _course_recap_html(course, reg.occurrence_date) if course else ""
        body = (
            f"<p>About to cancel <b>{esc(user.name if user else '(erased)')}</b> "
            f"({esc(user.email if user else '(erased)')})'s booking.</p>"
            + recap
            + """<form method="post" class="card">
          <label>Message to them <span class="opt">(optional)</span>
            <textarea name="message" rows="3" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Cancel this booking</button></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Cancel registration", body)

    def admin_reinstate(self, method: str, registration_id: str, environ):
        """Host-side twin of my_reinstate() -- undoes a cancellation on ANY
        guest's booking, for the same "guest called/emailed after canceling
        by mistake" use case my_reinstate() covers for the guest's own
        self-service view (2026-07-10: "ah yes true! (accidental error for
        the admin could be use case!)"). Same confirm-dialog-with-optional-
        message pattern as admin_cancel() (2026-07-10: "Reinstate should,
        LIKE CANCEL, also ask for a COMMENT to be sent with the email to
        the other") -- see admin_overview()'s row rendering for the
        dialog itself. Preserves `past` the same way admin_cancel() does,
        so reinstating from the "past" view's table doesn't bounce back to
        the default upcoming-only view."""
        session = _get_session(environ)
        if not session or session.get("kind") != "admin":
            return "302 Found", [("Location", "/admin/login")], ""
        reg = self.store.find_by_id(registration_id)
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/plain")], "not found"
        form = self._read_form(environ)
        message = sanitize_csv_field(form.get("message", "").strip())
        course = self.settings.course(reg.course_shortname)
        if course and date.fromisoformat(reg.occurrence_date) >= datetime.now(timezone.utc).date():
            updated = self.store.reinstate(registration_id, course.capacity)
            if updated is not None:
                self._sync(reg.course_shortname, date.fromisoformat(reg.occurrence_date))
                user = self.store.find_user_by_id(reg.user_id)
                self._send_reinstatement_emails(
                    course, reg.occurrence_date, user,
                    confirmed=(updated.status == STATUS_CONFIRMED), reinstated_by="host", message=message,
                )
        location = "/admin?past=1" if form.get("past") == "1" else "/admin"
        return "302 Found", [("Location", location)], ""

    def host_cancel(self, method: str, registration_id: str, environ):
        """A no-login "magic link" twin of admin_cancel() above, same
        cancellation logic (canceled_by="host", both sides notified via
        _send_cancellation_emails) but reachable without an admin session
        (2026-07-09, the operator, screenshot of being bounced to /admin/login:
        "instead it should be a magic link that does not need a password,
        but directly shows the page where it tells you: Cancel Booking /
        WHAT / WHERE / WHEN / Reason: <optional> / CONFIRM button"). This is
        what app/calendar_sync.py's per-participant "cancel:" line in the
        calendar EVENT DESCRIPTION now links to -- so from his own phone's
        calendar app, tapping that link goes straight to a confirm page
        instead of an admin login wall first.

        Security note: unlike guest_cancel()'s /cancel/<token> (a hashed,
        single-purpose token), this is gated purely by registration_id
        being an unguessable uuid4 (see storage.py's add_registration_*
        methods) -- there's no separate secret. That's an intentional,
        narrower trust boundary than the guest-facing link: this ID only
        ever appears somewhere already inside the operator's own trust boundary
        (his own CalDAV calendar, the password-gated /admin overview, or a
        guest's OWN /my page for their OWN booking) -- never broadcast the
        way a guest's emailed cancel link is. If that calendar is ever
        shared/exported somewhere less private, treat this the same as any
        other bearer link in it and reconsider."""
        reg = self.store.find_by_id(registration_id)
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page(
                "Not found", "<p>This link is invalid.</p>"
            )
        user = self.store.find_user_by_id(reg.user_id)
        course = self.settings.course(reg.course_shortname)
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            # See admin_cancel()'s own comment (2026-07-10 fix) on why this
            # is guarded on cancel()'s return value -- a magic link like
            # this one can plausibly be tapped twice from a calendar app
            # (e.g. a slow first tap, then a retry) even without a browser
            # back-button involved, so the same duplicate-email risk
            # applies here too. No redirect to add here, unlike
            # admin_cancel() -- this is a standalone, no-login page with no
            # "list" to send anyone back to.
            reinstate_token = new_token()
            changed = self.store.cancel(
                registration_id, canceled_by="host", host_message=message,
                reinstate_token_hash=hash_token(reinstate_token),
            )
            if changed:
                self._cancel_and_promote(reg.course_shortname, reg.occurrence_date)
                if course:
                    # Both sides notified, always -- see admin_cancel()'s own
                    # comment on why: the admin's own copy is what surfaces an
                    # unexpected cancellation if this link ever leaks.
                    self._send_cancellation_emails(
                        course, reg.occurrence_date, user, canceled_by="host", message=message,
                        registration_id=registration_id, reinstate_token=reinstate_token,
                    )
            return "200 OK", [("Content-Type", "text/html")], page("Canceled", "<p>Registration canceled and attendee notified.</p>")
        recap = _course_recap_html(course, reg.occurrence_date) if course else ""
        # 2026-07-11, the operator (screenshot of this exact page): "please add a
        # 'Never mind' button also here that brings you back to the
        # homepage! Check all other pages that you can reach with a direct
        # link to have not just one submit button as well!" -- this is a
        # standalone, no-login page (unlike the /my and /admin popup
        # dialogs, which already have their own JS "Never mind" close
        # button), so its escape hatch is a plain link back to "/" rather
        # than a dialog-close. Same audit applied to guest_cancel(),
        # guest_reinstate(), host_reinstate(), my_confirm_email(), and
        # my_cancel_email_change() -- every other single-submit-button
        # direct-link page in the app.
        body = (
            f"<p>Cancel <b>{esc(user.name if user else '(erased)')}</b> "
            f"({esc(user.email if user else '(erased)')})'s booking?</p>"
            + recap
            + """<form method="post" class="card">
          <label>Reason <span class="opt">(optional)</span>
            <textarea name="message" rows="3" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Confirm cancellation</button>
            <a href="/" class="link-button">Never mind</a></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Cancel booking", body)

    def host_reinstate(self, method: str, registration_id: str, environ):
        """No-login "magic link" twin of admin_reinstate(), reachable
        straight from the ADMIN's copy of the cancellation email
        (2026-07-10: "Both" [participant and admin copies get a reinstate
        link] / "for /my and /admin ... POPUP ... Only from the email
        there will be a single page ... WHAT WHEN WHERE like in the
        confirmation email"). Same security model as host_cancel() above:
        gated purely by `registration_id` being an unguessable uuid4 (see
        that method's own docstring on why that's an adequate boundary
        here) -- no separate token needed, unlike guest_reinstate()'s
        /reinstate/<token>."""
        reg = self.store.find_by_id(registration_id)
        if reg is None:
            return "404 Not Found", [("Content-Type", "text/html")], page(
                "Not found", "<p>This link is invalid.</p>"
            )
        user = self.store.find_user_by_id(reg.user_id)
        course = self.settings.course(reg.course_shortname)
        if (
            reg.status not in (STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST)
            or course is None
            or date.fromisoformat(reg.occurrence_date) < datetime.now(timezone.utc).date()
        ):
            return "200 OK", [("Content-Type", "text/html")], page(
                "Not found", "<p>This booking can no longer be rebooked.</p>"
            )
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            updated = self.store.reinstate(registration_id, course.capacity)
            if updated is not None:
                self._sync(reg.course_shortname, date.fromisoformat(reg.occurrence_date))
                self._send_reinstatement_emails(
                    course, reg.occurrence_date, user,
                    confirmed=(updated.status == STATUS_CONFIRMED), reinstated_by="host", message=message,
                )
            return "200 OK", [("Content-Type", "text/html")], page(
                "Rebooked", "<p>Registration rebooked and attendee notified.</p>"
            )
        recap = _course_recap_html(course, reg.occurrence_date) if course else ""
        body = (
            f"<p>Rebook <b>{esc(user.name if user else '(erased)')}</b> "
            f"({esc(user.email if user else '(erased)')})'s booking?</p>"
            + recap
            + """<form method="post" class="card">
          <label>Optional message to them <span class="opt">(optional)</span>
            <textarea name="message" rows="3" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Confirm rebooking</button>
            <a href="/" class="link-button">Never mind</a></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Rebook booking", body)

    def host_cancel_occurrence(self, method: str, course_shortname: str, occurrence_date_str: str, environ):
        """"Cancel the entire session" -- no-login "magic link" twin of
        host_cancel() above, but for EVERY registration on one occurrence
        at once rather than a single guest's booking (2026-07-13, the operator:
        "cancel the entire course link ... only for the HOST / admin").
        Reachable from the operator's OWN CalDAV event (see
        calendar_sync.sync_occurrence's own "cancel entire session" line) --
        NEVER from a guest-facing email or .ics attachment; a guest's own
        invite (calendar_sync.guest_invite_ics) has no participant list and
        no cancel links of any kind, by design (see that function's own
        docstring on why).

        Same security model as host_cancel(): gated purely by
        (course_shortname, occurrence_date) both being unremarkable, already
        -public-inside-the-app values (a course shortname and a date aren't
        secrets the way a token is) -- this link only ever appears inside
        the operator's own trust boundary (his own calendar), same narrower
        boundary host_cancel()'s own docstring explains for a single
        registration's magic link.

        GET shows a confirmation page listing every participant who'd be
        canceled (via cancel_flow.find_cancelable_registrations_for_
        occurrence -- confirmed, waitlisted, AND pending-confirmation, see
        that function's own docstring), so the host can see exactly who's
        affected before committing -- an empty list still renders (a plain
        "nobody to cancel" message), it isn't treated as 404: unlike a
        single registration_id, a (course, date) pair can legitimately have
        nothing left to cancel (e.g. the link tapped twice) without being
        an invalid link."""
        course = self.settings.course(course_shortname)
        if course is None:
            return "404 Not Found", [("Content-Type", "text/html")], page(
                "Not found", "<p>This link is invalid (course no longer configured).</p>"
            )
        participants = find_cancelable_registrations_for_occurrence(self.store, course_shortname, occurrence_date_str)
        if method == "POST":
            form = self._read_form(environ)
            message = sanitize_csv_field(form.get("message", "").strip())
            result = self._cancel_occurrence(course_shortname, occurrence_date_str, message=message)
            return "200 OK", [("Content-Type", "text/html")], page(
                "Canceled",
                f"<p>{len(result.canceled)} registration(s) for <b>{esc(course.title)}</b> "
                f"on {esc(occurrence_date_str)} canceled, every participant notified.</p>",
            )
        recap = _course_recap_html(course, occurrence_date_str)
        if not participants:
            return "200 OK", [("Content-Type", "text/html")], page(
                "Cancel entire session",
                f"<p>Nobody is currently booked for <b>{esc(course.title)}</b> on "
                f"{esc(occurrence_date_str)} -- nothing to cancel.</p>" + recap
                + '<p><a href="/" class="link-button">Back to home</a></p>',
            )
        users_by_id = {u["user_id"]: User(**u) for u in self.store.read_users(scope="all")}
        rows = []
        for r in participants:
            u = users_by_id.get(r.user_id)
            who = f"{esc(u.name)} ({esc(u.email)})" if u else "(unknown)"
            rows.append(f"<li>{who} -- {esc(status_label(r.status))}</li>")
        body = (
            f"<p>Cancel <b>EVERY</b> registration for <b>{esc(course.title)}</b> "
            f"on {esc(occurrence_date_str)}? {len(participants)} participant(s) will be "
            "notified by email:</p>"
            f"<ul>{''.join(rows)}</ul>"
            + recap
            + """<form method="post" class="card">
          <label>Reason <span class="opt">(optional)</span>
            <textarea name="message" rows="3" class="big-input"></textarea></label>
          <div class="submit-row"><button type="submit">Confirm -- cancel entire session</button>
            <a href="/" class="link-button">Never mind</a></div>
        </form>"""
        )
        return "200 OK", [("Content-Type", "text/html")], page("Cancel entire session", body)
