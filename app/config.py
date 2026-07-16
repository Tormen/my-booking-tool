"""Load settings.toml + secrets referenced from it. Stdlib only on the
target server (Fedora 43 ships Python 3.14, so tomllib -- stdlib since
3.11 -- is always available there). The try/except below only exists so
this same code can also run/be tested on older Python elsewhere (e.g. a
developer machine or CI on 3.10) via the small `tomli` backport; it changes
nothing about the zero-runtime-dependency story on the actual server.
"""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (dev/test only, not the target server)
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CourseDateOverride:
    """One exceptional date for a course (2026-07-16): a per-course
    config option to exceptionally change the time for a course on a
    certain date, with an optional explanatory message, and support for
    a LIST of such dates with different times. Parsed from a `[[course.date_override]]` sub-table
    nested under the relevant `[[course]]` entry (see
    settings.toml.example) -- one course can have any number of these.

    `start_time` follows the exact same "HH:MM" convention as
    Course.start_time. `duration_minutes` is optional -- omit it (None,
    the default) to keep the course's own normal duration and only shift
    the START time; set it to actually run long/short that one day too.
    `message` is optional free-text shown alongside the exceptional time
    everywhere it's displayed (booking page, index.html, every
    confirmation/cancellation email for that occurrence) -- blank omits
    it, showing just the changed time with no explanation."""
    date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM"
    duration_minutes: int | None = None
    message: str = ""

    def start_hm(self) -> tuple[int, int]:
        h, m = self.start_time.split(":")
        return int(h), int(m)


@dataclass(frozen=True)
class Course:
    shortname: str
    title: str
    location: str
    weekday: str  # "mon".."sun"
    start_time: str  # "HH:MM"
    duration_minutes: int
    capacity: int
    audience: str = "private"  # "private" | "public"
    language: str = "en"
    description: str = ""
    # Optional map/directions link (2026-07-09): makes /my's Location
    # column clickable for this course. "" (the default -- key omitted in settings.toml) means
    # no link at all: /my's Location column falls back to plain text, same
    # as it always has. Deliberately its own field rather than reusing
    # `location` as a combined "text (url)" string -- `location` stays
    # exactly what it's always been (plain display text, also used in the
    # auto-derived subtitle line/emails), so nothing that already reads
    # `location` needs to change.
    location_url: str = ""
    # Optional override for the booking page's subtitle line (rendered as
    # plain text, not rich HTML like `description` -- see
    # app/webapp.py::_book_page). None (the default -- key omitted in
    # settings.toml) means auto-derive "<Weekday>s <start>h<mm> - <end>h<mm>
    # -- <location>" (e.g. "Saturdays 10h45 - 12h45 -- Ayur Yoga Center
    # Trier Nord") from weekday/start_time/duration_minutes/location below
    # -- see time_range_label(). Set to "" explicitly to show no subtitle
    # at all, or to any other string to override the auto-derived one.
    subtitle: str | None = None
    # Determines this course's position on /courses (2026-07-09: a
    # sorting key to control the ORDER of the courses; renamed from the
    # original `order` the same day to be self-explanatory about what
    # this "order" is actually used for -- the bare word "order" alone
    # didn't say order of WHAT): sorted ascending, lowest first. Defaults
    # to 0 for every course that doesn't set it, so an existing
    # settings.toml with no `order_in_all_courses` keys anywhere is
    # unaffected -- load_settings()'s sort is stable, so a tie (e.g.
    # everything left at the default 0) keeps each course's original
    # position in the file, exactly as before this existed. Doesn't need
    # to be unique or contiguous -- gaps (10, 20, 30) are a common,
    # deliberately loose convention that leaves room to slot a new course
    # in later without renumbering everything else.
    order_in_all_courses: int = 0
    # Optional, per-course. 2026-07-14: a list of email addresses that,
    # when set on a course in settings.toml, are also invited as
    # optional (cc) so they receive the same invite as well. Passed straight through to
    # app.ics.VEvent's own organizer/attendees fields by
    # app.calendar_sync.sync_occurrence() -- see that call site and
    # VEvent's own docstring for the ROLE=OPT-PARTICIPANT/RSVP=FALSE
    # semantics, and the caveat that whether this actually triggers an
    # invite EMAIL depends on the CalDAV server's own scheduling support.
    # Empty tuple (the default -- key omitted in settings.toml) means no
    # ORGANIZER/ATTENDEE properties at all, byte-identical to before this
    # existed.
    host_calendar_entry_cc_list: tuple[str, ...] = ()
    # 2026-07-16: exceptional per-date time changes -- see
    # CourseDateOverride's own docstring above. Empty tuple (the default
    # -- no `[[course.date_override]]` sub-tables in settings.toml) means
    # no exceptions at all, byte-identical to before this existed.
    date_overrides: tuple[CourseDateOverride, ...] = ()

    WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    WEEKDAY_LABELS = {
        "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
        "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
    }

    def weekday_index(self) -> int:
        return self.WEEKDAYS.index(self.weekday.lower())

    def weekday_label(self) -> str:
        """Full weekday name for display (e.g. "sat" -> "Saturday") -- used
        on the booking page so guests see e.g. "Saturdays 10h45 - 12h45 --
        Trier" instead of just the bare course title, without hardcoding
        any one deployment's actual day/time/location into the generic
        template."""
        return self.WEEKDAY_LABELS.get(self.weekday.lower(), self.weekday.title())

    def start_hm(self) -> tuple[int, int]:
        h, m = self.start_time.split(":")
        return int(h), int(m)

    def end_hm(self) -> tuple[int, int]:
        """start_time + duration_minutes, wrapped to a 24h clock -- a
        session is never assumed to cross midnight in display terms (the
        format is purely for the subtitle line, not scheduling math)."""
        h, m = self.start_hm()
        total = h * 60 + m + self.duration_minutes
        return (total // 60) % 24, total % 60

    @staticmethod
    def _fmt_hm(h: int, m: int) -> str:
        # European "10h45" style, not "10:45" -- matches how times are
        # actually written/spoken in the venues this template targets.
        # Minutes always shown (zero-padded), even on the hour ("10h00"),
        # so from/till always line up visually in the rendered subtitle.
        return f"{h}h{m:02d}"

    def time_range_label(self) -> str:
        """e.g. "10h45 - 12h45" -- used by app/webapp.py to auto-derive the
        booking page's subtitle line when Course.subtitle isn't set."""
        return f"{self._fmt_hm(*self.start_hm())} - {self._fmt_hm(*self.end_hm())}"

    def weekday_time_range_label(self) -> str:
        """e.g. "SAT 10h45-12h45" -- the 3-letter weekday code (already how
        `weekday` is stored in settings.toml, just uppercased) plus a
        TIGHTER time range than time_range_label() (no spaces around the
        dash) -- used by /my's bookings table Time column, where several
        rows of this need to fit in one narrow column at a glance
        (2026-07-10: added the weekday to the TIME column, e.g. "SAT
        10h45-12h45")."""
        return f"{self.weekday.upper()} {self._fmt_hm(*self.start_hm())}-{self._fmt_hm(*self.end_hm())}"

    def override_for(self, occ_date: str) -> CourseDateOverride | None:
        """The CourseDateOverride for this exact "YYYY-MM-DD", if any --
        the single lookup every other *_for() helper below and
        app/slots.py::build_occurrences share, so a date match is only
        ever defined in one place."""
        return next((o for o in self.date_overrides if o.date == occ_date), None)

    def start_hm_for(self, occ_date: str) -> tuple[int, int]:
        override = self.override_for(occ_date)
        return override.start_hm() if override else self.start_hm()

    def duration_minutes_for(self, occ_date: str) -> int:
        override = self.override_for(occ_date)
        if override and override.duration_minutes is not None:
            return override.duration_minutes
        return self.duration_minutes

    def end_hm_for(self, occ_date: str) -> tuple[int, int]:
        h, m = self.start_hm_for(occ_date)
        total = h * 60 + m + self.duration_minutes_for(occ_date)
        return (total // 60) % 24, total % 60

    def time_range_label_for(self, occ_date: str) -> str:
        """Same shape as time_range_label(), but reflecting an exceptional
        date's own overridden start/end if `occ_date` ("YYYY-MM-DD") has
        one -- what every guest-facing "When:" line (booking page,
        confirmation/cancellation emails, index.html) should call instead
        of the plain time_range_label() from now on, so a one-off
        schedule change is never silently shown with the wrong time."""
        if self.override_for(occ_date) is None:
            return self.time_range_label()
        return f"{self._fmt_hm(*self.start_hm_for(occ_date))} - {self._fmt_hm(*self.end_hm_for(occ_date))}"

    def override_message_for(self, occ_date: str) -> str:
        """The optional explanation attached to `occ_date`'s override, or
        "" if there's no override for that date, or the override has no
        message (message is itself optional, see CourseDateOverride)."""
        override = self.override_for(occ_date)
        return override.message if override else ""


@dataclass(frozen=True)
class Settings:
    timezone: str
    admin_email: str
    base_url: str

    caldav_url: str
    caldav_username: str
    caldav_password: str
    booking_calendar: str
    conflict_calendars: tuple[str, ...]

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str

    admin_password_hash: str

    show_next_slots: int
    show_next_days: int
    # No longer hides occurrences (see app/slots.py) -- gates LATE bookings
    # against min_required_participants instead (see app/webapp.py::book).
    min_notice_hours: int

    retention_months: int
    canceled_retention_months: int
    erasure_pepper: bytes
    # Optional (2026-07-09): the scheduler that deletes accounts also
    # sends a dormant-account warning email once, before deletion. 0
    # (the default -- also what "" or a
    # commented-out/omitted key parse to, see load_settings()) disables
    # this entirely: no warning is ever sent. When positive, it's how many
    # days BEFORE an account would reach `retention_months` of inactivity
    # (see app.retention.send_account_deletion_warnings) that the ONE
    # warning email goes out -- reusing retention_months as the actual
    # dormancy threshold (it already defines the retention duration)
    # rather than adding a second duration setting.
    # "Activity" is the latest of User.last_login_at, created_at, and the
    # most recent booking made (2026-07-14, review finding B2: booking
    # counts as activity too -- see app/retention.py::account_activity_date).
    account_deletion_warning_days: int = 0

    # Optional (2026-07-09): a directory of custom email templates,
    # overriding this repo's own built-in ones. "" (the default -- key omitted) means every email uses
    # this repo's own built-in email_templates/ copy unconditionally; when
    # set, a matching file THERE takes priority over the built-in one, per
    # template file, so you only need to copy the specific templates you
    # actually want to customize -- see app/email_templates.py's own
    # docstring for the full macro/variable mechanism this enables.
    email_templates_folder: str = ""

    courses: tuple[Course, ...] = field(default_factory=tuple)

    # Calendar-invite VALARM reminders (minutes before start) -- see
    # ics.py::VEvent.alarms_minutes_before. 2026-07-07: reminders became
    # a configurable setting, defaulting to NO reminders, for the
    # TRAINER's own event (app/calendar_sync.py::sync_occurrence, the one
    # PUT to the operator's CalDAV calendar), while course PARTICIPANTS'
    # emailed invite (guest_invite_ics) should default to exactly one
    # reminder, 1h before. Both configurable, in case that changes later;
    # empty tuple = no VALARMs at all (matches VEvent's own default now).
    trainer_calendar_reminder_minutes: tuple[int, ...] = ()
    guest_calendar_reminder_minutes: tuple[int, ...] = (60,)

    # 2026-07-16: whether an ALL-DAY event (DTSTART;VALUE=DATE, no time of
    # day) in a conflict calendar hides that day's course dates. On by
    # default (the operator's explicit call, same day the setting was
    # born with default false): an all-day vacation entry should block
    # without needing a start/end time. The two settings below are the
    # calendar-side escape hatches for the exceptional all-day event
    # that should NOT block. See app/webapp.py::_conflict_checker.
    conflict_calendar_all_day_events_also_block_the_course: bool = True

    # Escape hatch 1: an all-day event whose TITLE contains this marker
    # (case-insensitive) does not block course dates -- lets the operator
    # whitelist one specific all-day event from any calendar client, no
    # server access needed. "" (the default) disables the marker feature
    # entirely.
    all_day_non_blocking_title_marker: str = ""

    # Escape hatch 2: an all-day event marked "show as Free" in the
    # calendar app (RFC 5545 TRANSP:TRANSPARENT) does not block course
    # dates -- that flag literally means "does not reserve the time".
    # On by default. CAUTION, and why this can't be the only mechanism:
    # many clients (Apple Calendar, Google, Open-Xchange) create all-day
    # events as Free BY DEFAULT, so an all-day event the operator wants
    # to block with may need to be flipped to "Busy" by hand. Timed
    # events are deliberately not affected -- they always block, Free or
    # not, since "no slot shown = no session" must stay predictable.
    all_day_free_events_do_not_block: bool = True

    # Also write logs to this file (in addition to stdout/journal -- see
    # app/logutil.py, size-capped rotation). ON by default (2026-07-16,
    # operator's call): the watchdog's rate-limit-block alerting and the
    # CSP-violation checks in `my-bt admin health`/`setup` read ONLY this
    # file and are silently blind without it. A settings.toml withOUT the
    # key gets DEFAULT_LOG_FILE (applied by load_settings via
    # log_file_from_raw -- the dataclass default here stays None so
    # directly-constructed test Settings never touch the filesystem);
    # log_file = "" in settings.toml explicitly disables file logging.
    log_file: str | None = None

    # Optional, comma-separated (2026-07-09): BCC these addresses on
    # every attendee-facing email, e.g. to monitor what guests actually
    # receive. Applied
    # ONLY to attendee/guest-facing emails (booking confirmed/waitlisted,
    # promoted-from-waitlist, canceled, reinstated, account-confirm/
    # password-reset/email-change) -- never to the separate admin-facing
    # copy of the same event (that one already goes straight to
    # admin_email) or to operator-only mail like the watchdog alert.
    # Empty string (the default -- key omitted in settings.toml) disables
    # this entirely, same as today. See app/emailer.py::send_mail's own
    # `bcc_addrs` param and app.config.Settings.bcc_attendee_email_list.
    bcc_attendee_emails: str = ""

    @property
    def bcc_attendee_email_list(self) -> tuple[str, ...]:
        """Parsed, whitespace-trimmed, blank-entries-dropped form of
        `bcc_attendee_emails` -- every attendee-facing send_mail() call site
        reads this instead of re-splitting the raw string itself."""
        return tuple(a.strip() for a in self.bcc_attendee_emails.split(",") if a.strip())

    # Whether the booking page shows "N spot(s) left" / "FULL, join
    # waitlist" at all. True = current/original behaviour.
    show_spots_left: bool = True
    # Deliberately displayed-only, for A/B-testing whether perceived
    # scarcity changes booking behaviour -- see app/webapp.py::_spots_left_text
    # for exactly what this does and does not affect (never touches the
    # real capacity/waitlist decision, which always uses the true count).
    # Positive = show fewer spots than really available (more urgency);
    # negative = show more. 0 = display the real number, unchanged.
    spots_left_offset: int = 0

    # Minimum CONFIRMED registrations for a course to run. Only matters for
    # a LATE booking (within min_notice_hours of start) -- see
    # app/webapp.py::book. Default 1 = never blocks anyone: a single
    # confirmed booking always satisfies it on its own.
    min_required_participants: int = 1

    # Text on the booking page's submit button for a bookable (not full)
    # occurrence -- see app/webapp.py::_book_page. Always overridden to
    # "Join waitlist" for a full occurrence regardless of this setting,
    # since that's the one label that has to stay literally true to what
    # submitting the form actually does.
    book_button_label: str = "Book"

    # Hard ceiling on how many "+ Add participant" guest rows the booking
    # form offers, and how many guests book() will ever admit per booking
    # (enforced on the count of guest_email_N fields submitted, not their
    # index values -- see app/webapp.py's _parse_guest_entries).
    # 2026-07-09: made configurable, defaulting to 3 (was a fixed
    # constant of 9 before).
    max_guests: int = 3

    # A booking made under a not-yet-confirmed email (see
    # storage.STATUS_PENDING_CONFIRMATION and app/webapp.py::book) doesn't
    # hold a real spot or sync to the calendar until the guest clicks the
    # emailed confirmation link. If they never do, the retention job purges
    # that pending registration (and it stops counting toward anything)
    # once it's older than this many hours -- keeps an abandoned or bogus
    # signup from lingering indefinitely. Independent of retention_months/
    # canceled_retention_months below, which are much longer and apply to
    # real (confirmed/waitlisted/canceled) bookings.
    pending_confirmation_hours: int = 48

    # Optional: absolute path to the LIVE, web-served copy of site/ (the
    # separate checkout/host location -- see README.md "Static-site
    # pages"), e.g. "/var/www/example.org". If set, `my-bt status` compares
    # its privacy.html against this settings.toml's retention numbers and
    # warns on drift (see app/cli_checks.py::check_static_site_drift), and
    # `my-bt setup -i` can regenerate it directly there (app/site_render.py)
    # -- no rebuild/reinstall needed just to pick up a config-only change.
    # None (the default) means this check/action is skipped entirely.
    static_site_dir: str | None = None

    # Optional: whether `my-bt setup -i` derives a no-JavaScript
    # index_embedded.html straight from the LIVE, currently deployed
    # index.html (see app/site_render.py::derive_index_embedded_html) plus
    # this settings.toml, and offers to deploy it alongside index.html at
    # static_site_dir -- see README.md "Static-site pages". False (the
    # default) means this whole mechanism is off: most deployments don't
    # embed their site via <iframe> elsewhere, so nothing changes for them.
    index_embedded_enabled: bool = False
    # Only read when index_embedded_enabled is True above. True (the
    # default): every outbound link the derivation retargets (Login,
    # course booking links, footer legal links) opens in a NEW tab
    # (target="_blank" rel="noopener noreferrer") -- the booking flow and
    # /my genuinely need JavaScript, which should run in an ordinary
    # top-level tab, not inside whatever iframe this page happens to be
    # embedded in. False: same tab, target="_top" (breaks out of the
    # iframe, same convention index.html's own Login link already uses).
    index_embedded_new_tab_links: bool = True

    # Optional: operator-authored HTML shown in the same red ATTENTION box
    # as the automatic per-course schedule-exception banner (index.html,
    # index_embedded.html, and every /book/<shortname> page) -- BELOW any
    # such exceptions, separated by a <hr> when both are present, or on
    # its own (no <hr>) when there are no exceptions right now. Not
    # per-course: it's site-wide, so it always shows everywhere the
    # exceptions banner can appear, regardless of which course's page
    # you're on. Raw HTML, NOT escaped -- same operator-is-already-
    # trusted boundary as Course.description and a date-override's own
    # `message` (see attention_html()'s own docstring) -- so formatting
    # tags work, e.g.:
    #   custom_attention_message = "On vacation from <b>2026-08-01</b> to
    #   <b>2026-08-15</b> -- courses resume afterwards at their usual
    #   schedule."
    # "" (the default) means nothing extra is shown.
    custom_attention_message: str = ""

    # Optional: a hostname (typically your own dynamic-DNS name, e.g.
    # "ssh.example.net") whose CURRENT resolved IP is allowed to keep using
    # /courses and /book/<shortname> as normal even while maintenance mode
    # (app/maintenance.py) is ON for everyone else (2026-07-10: so the
    # operator can still access the site during their own maintenance
    # window). Resolved fresh on every request while
    # maintenance is on (rare/short-lived by nature, so no caching), NOT
    # baked in once at startup -- your dynamic IP can change between when
    # you turn maintenance on and when you next check the site. None (the
    # default) means no bypass at all -- maintenance blocks everyone,
    # including you, same as before this setting existed. See
    # app/webapp.py::_maintenance_bypass_allowed().
    maintenance_bypass_hostname: str | None = None
    # Optional second source for the same bypass check, checked IN ADDITION
    # to (not instead of) maintenance_bypass_hostname above -- the path to a
    # plain text file whose LAST non-empty line is your current IP, kept
    # fresh by infrastructure outside this app (2026-07-10: for setups
    # where a changing IP is tracked in a log file in addition to DNS).
    # Mirrors
    # nginx's own sync-dynamic-ip-acls.sh, which already checks both the
    # same hostname AND this same log file when rebuilding /admin's IP
    # allowlist -- DNS can lag an actual IP change by however long the
    # record's TTL/propagation takes, while this file is updated the moment
    # the IP itself changes. None (the default) skips this source; either
    # source alone is enough to match (see
    # app/webapp.py::_maintenance_bypass_allowed()).
    maintenance_bypass_ip_log: str | None = None

    # --- Watchdog (app/watchdog.py) -- see README.md "Watchdog" and
    # [watchdog] in settings.toml. A periodic (systemd-timer-driven) health
    # check, NOT a replacement for fail2ban or fine-grained per-key rate
    # limiting (both already exist -- see RateLimiter in app/security.py and
    # README.md "Logs & debugging"). It looks back over the last
    # watchdog_window_minutes each run and emails admin_email once if
    # anything crosses a threshold; silent otherwise.
    watchdog_enabled: bool = True
    # How far back each run looks. Should match (or be a bit longer than)
    # the systemd timer's own interval (systemd/my-booking-watchdog.timer),
    # or a burst could fall in the gap between two runs and never get
    # counted by either.
    watchdog_window_minutes: int = 15
    # Path to nginx's access log (combined format). None (the default)
    # disables the nginx-burst check entirely -- e.g. if nginx logs
    # somewhere non-standard, or you'd rather rely on fail2ban alone for
    # that layer.
    watchdog_nginx_access_log: str | None = None
    # Alert if a single IP makes at least this many requests within the
    # window -- a crude scraping/DoS-ish signal, not a hard limit (nginx
    # itself isn't told to block anything; this only emails you).
    watchdog_nginx_request_threshold: int = 200
    # Alert if a single IP's 4xx/5xx share of its own requests within the
    # window is at least this fraction (0.0-1.0) -- only evaluated once an
    # IP has made enough requests to be meaningful (see
    # _MIN_REQUESTS_FOR_ERROR_RATE in app/watchdog.py), so one stray 404
    # doesn't trigger anything.
    watchdog_nginx_error_rate_threshold: float = 0.5
    # Alert if at least this many pending_confirmation registrations (see
    # storage.STATUS_PENDING_CONFIRMATION) were created within the window --
    # a burst of brand-new, unconfirmed signups is the shape a
    # capacity-grab attempt would take (a real confirmed booking can never
    # trigger this, only the pending ones).
    watchdog_pending_signup_threshold: int = 10
    # Alert if the app's own log shows at least this many rate-limiter
    # rejections (guest/admin login, password reset -- see
    # webapp.py::login_limiter) within the window, across all keys
    # combined -- a spike here means someone is hammering logins broadly,
    # not just one account.
    watchdog_rate_limit_block_threshold: int = 5
    # Alert if sshd logged at least this many failed-password attempts
    # (any source, any account) within the window -- deliberately a much
    # cruder, sitewide signal than fail2ban's own per-IP ban threshold --
    # this is an early heads-up, not a substitute for it.
    watchdog_sshd_failure_threshold: int = 5
    # Alert if the app's own log shows at least this many CSP violation
    # reports (app/webapp.py::csp_report -- browser-reported
    # Content-Security-Policy violations, e.g. a stale script-src hash
    # after an inline <script> edit, or an embed attempt from outside the
    # allow-listed frame-ancestors origin) within the window, across every
    # distinct violation combined. `my-bt health`/`admin setup` surface
    # ANY CSP violation unconditionally (see app.cli_checks.
    # check_csp_violations) -- this threshold only gates the WATCHDOG's
    # own emailed alert, so a single stray/one-off report doesn't page you
    # at 3am.
    watchdog_csp_violation_threshold: int = 3

    def course(self, shortname: str) -> Course | None:
        for c in self.courses:
            if c.shortname == shortname:
                return c
        return None


def load_raw_toml(toml_path: str | Path) -> dict | None:
    """Parse settings.toml without requiring the secret files load_settings()
    needs -- returns None if the file doesn't exist yet (a legitimate state
    to check for, e.g. `my-bt status` on a fresh install), lets a genuine
    TOML syntax error raise normally (that's a real problem to surface)."""
    toml_path = Path(toml_path)
    if not toml_path.exists():
        return None
    with toml_path.open("rb") as f:
        return tomllib.load(f)


# The default [logging].log_file -- inside the app's own data dir, so the
# service user can always write it. See log_file_from_raw() for the
# absent-vs-"" semantics and Settings.log_file's comment for why file
# logging is on by default.
DEFAULT_LOG_FILE = "/var/lib/my-booking/my-booking.log"


def log_file_from_raw(raw: dict | None) -> str | None:
    """The effective [logging].log_file for a raw-parsed settings.toml:
    key absent -> DEFAULT_LOG_FILE (file logging is ON by default),
    log_file = "" -> None (explicitly disabled), else the configured
    path. THE one place this rule lives -- load_settings, peek_log_file,
    and every raw-toml reader (`my-bt status`/`admin health`/`admin
    csp-violations`, cli_checks, cli_setup) resolve through it, so "which
    log file is in effect" can't drift between the running service and
    the tooling that reads its log back."""
    logging_cfg = (raw or {}).get("logging", {})
    if "log_file" not in logging_cfg:
        return DEFAULT_LOG_FILE
    return logging_cfg.get("log_file") or None


def peek_log_file(toml_path: str | Path) -> str | None:
    """The effective log_file (see log_file_from_raw) without requiring
    the secret files load_settings() needs -- used by `my-bt -L/--log`,
    since several my-bt subcommands (list/users/show/stats) never call
    load_settings() at all, and shouldn't have to just to find a log
    path. No settings.toml at all -> None (a bare dev checkout shouldn't
    start writing to /var/lib just for running a CLI command)."""
    raw = load_raw_toml(toml_path)
    if raw is None:
        return None
    return log_file_from_raw(raw)


def _read_secret(path_str: str) -> str:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(
            f"secret file {path} does not exist yet -- create it (mode 600) "
            "before starting the service"
        )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file {path} is empty")
    return value


def courses_from_raw(raw: dict) -> tuple[Course, ...]:
    """Parses every `[[course]]` (and nested `[[course.date_override]]`)
    sub-table into real `Course`/`CourseDateOverride` objects, straight off
    the *raw* parsed TOML -- no secrets touched, so a caller that only has
    `raw` (e.g. `my-bt status`/`setup`, or app/site_render.py's
    index_embedded.html rendering) can get real `Course` objects (needed
    for `Course.time_range_label_for()`, used to render an upcoming
    date_override's exceptional time) without needing a fully loaded
    `Settings` -- which would require every secret file to exist first.
    Factored out of load_settings() (2026-07-16), which now just calls this
    -- the parsing logic itself hasn't changed, only where it lives, so
    load_settings()'s own behavior (including its duplicate-shortname
    check) is unchanged."""
    courses = [
        Course(
            shortname=c["shortname"],
            title=c["title"],
            location=c["location"],
            weekday=c["weekday"],
            start_time=c["start_time"],
            duration_minutes=int(c["duration_minutes"]),
            capacity=int(c["capacity"]),
            audience=c.get("audience", "private"),
            language=c.get("language", "en"),
            description=c.get("description", ""),
            location_url=c.get("location_url", ""),
            subtitle=c.get("subtitle"),
            order_in_all_courses=int(c.get("order_in_all_courses", 0)),
            host_calendar_entry_cc_list=tuple(c.get("host_calendar_entry_cc_list", [])),
            date_overrides=tuple(
                CourseDateOverride(
                    date=o["date"],
                    start_time=o["start_time"],
                    duration_minutes=int(o["duration_minutes"]) if o.get("duration_minutes") is not None else None,
                    message=o.get("message", ""),
                )
                for o in c.get("date_override", [])
            ),
        )
        for c in raw.get("course", [])
    ]

    shortnames = [c.shortname for c in courses]
    if len(shortnames) != len(set(shortnames)):
        raise ValueError("duplicate course shortname in settings.toml")

    # Stable sort (see Course.order_in_all_courses's own docstring): every
    # course left at the default order_in_all_courses=0 keeps its original
    # settings.toml position relative to every other 0-order course, so
    # this is a no-op unless order_in_all_courses is actually set somewhere.
    return tuple(sorted(courses, key=lambda c: c.order_in_all_courses))


def today_in_raw_timezone(raw: dict) -> str:
    """"YYYY-MM-DD" for "now" in `[site].timezone` -- falls back to UTC if
    the raw dict doesn't have one yet (e.g. an incomplete settings.toml a
    health check is still allowed to inspect). Shared by
    app/webapp.py::schedule_exceptions and app/site_render.py's
    index_embedded.html rendering so both agree on what "today" means when
    filtering upcoming_date_overrides() below."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz_name = raw.get("site", {}).get("timezone") or "UTC"
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def upcoming_date_overrides(courses, today: str) -> list[dict]:
    """Every CourseDateOverride across `courses` whose date is >= `today`
    ("YYYY-MM-DD"), sorted (date, then shortname) -- the exact computation
    app/webapp.py::schedule_exceptions used to do inline for its public
    JSON endpoint (2026-07-16). Factored out here so that live endpoint AND
    app/site_render.py's static index_embedded.html rendering share one
    definition of "upcoming" and can never drift apart on it -- each item:
    {course_shortname, course_title, date, weekday, time_label, message}.

    2026-07-13: added `weekday` (course.weekday_label(), e.g. "Saturday")
    -- the site-wide banner (index.html/index_embedded.html, which lists
    exceptions across every course at once) leads with the weekday so
    it's clear at a glance which recurring session is affected, without
    needing to parse `date` -- unlike the per-course banner on that
    course's own /book/<shortname> page, where the weekday is already
    obvious from context."""
    items = [
        {
            "course_shortname": course.shortname,
            "course_title": course.title,
            "date": override.date,
            "weekday": course.weekday_label(),
            "time_label": course.time_range_label_for(override.date),
            "message": override.message,
        }
        for course in courses
        for override in course.date_overrides
        if override.date >= today
    ]
    items.sort(key=lambda it: (it["date"], it["course_shortname"]))
    return items


def load_settings(toml_path: str | Path) -> Settings:
    toml_path = Path(toml_path)
    with toml_path.open("rb") as f:
        raw = tomllib.load(f)

    site = raw["site"]
    cal = raw["calendar"]
    smtp = raw["smtp"]
    admin = raw["admin"]
    defaults = raw.get("defaults", {})
    privacy = raw.get("privacy", {})
    watchdog = raw.get("watchdog", {})

    courses = courses_from_raw(raw)

    return Settings(
        timezone=site["timezone"],
        admin_email=site["admin_email"],
        base_url=site["base_url"].rstrip("/"),
        static_site_dir=(site.get("static_site_dir") or None),
        index_embedded_enabled=bool(site.get("index_embedded_enabled", False)),
        index_embedded_new_tab_links=bool(site.get("index_embedded_new_tab_links", True)),
        custom_attention_message=site.get("custom_attention_message", ""),
        email_templates_folder=site.get("email_templates_folder", ""),
        maintenance_bypass_hostname=(site.get("maintenance_bypass_hostname") or None),
        maintenance_bypass_ip_log=(site.get("maintenance_bypass_ip_log") or None),
        caldav_url=cal["caldav_url"],
        caldav_username=cal["caldav_username"],
        caldav_password=_read_secret(cal["caldav_password_file"]),
        booking_calendar=cal["booking_calendar"],
        conflict_calendars=tuple(cal.get("conflict_calendars", [])),
        conflict_calendar_all_day_events_also_block_the_course=bool(
            cal.get("conflict_calendar_all_day_events_also_block_the_course", True)
        ),
        all_day_non_blocking_title_marker=str(cal.get("all_day_non_blocking_title_marker", "")),
        all_day_free_events_do_not_block=bool(cal.get("all_day_free_events_do_not_block", True)),
        trainer_calendar_reminder_minutes=tuple(int(m) for m in cal.get("trainer_reminder_minutes", [])),
        guest_calendar_reminder_minutes=tuple(int(m) for m in cal.get("guest_reminder_minutes", [60])),
        smtp_host=smtp["host"],
        smtp_port=int(smtp["port"]),
        smtp_username=smtp["username"],
        smtp_password=_read_secret(smtp["password_file"]),
        smtp_from=smtp["from_address"],
        bcc_attendee_emails=smtp.get("bcc_attendee_emails", ""),
        admin_password_hash=_read_secret(admin["password_hash_file"]),
        show_next_slots=int(defaults.get("show_next_slots", 4)),
        show_next_days=int(defaults.get("show_next_days", 42)),
        min_notice_hours=int(defaults.get("min_notice_hours", 2)),
        show_spots_left=bool(defaults.get("show_spots_left", True)),
        spots_left_offset=int(defaults.get("spots_left_offset", 0)),
        min_required_participants=int(defaults.get("min_required_participants", 1)),
        book_button_label=defaults.get("book_button_label", "Book"),
        max_guests=int(defaults.get("max_guests", 3)),
        pending_confirmation_hours=int(defaults.get("pending_confirmation_hours", 48)),
        retention_months=int(privacy.get("retention_months", 24)),
        canceled_retention_months=int(privacy.get("canceled_retention_months", 6)),
        erasure_pepper=bytes.fromhex(_read_secret(privacy["erasure_pepper_file"])),
        # `or 0` collapses every "off" spelling (0, "", or
        # the key omitted entirely -- privacy.get's own default) to the
        # same falsy value in one step: 0/""/None are all falsy in Python,
        # so only a genuinely truthy (non-zero, non-blank) value reaches
        # int() at all.
        account_deletion_warning_days=int(
            privacy.get("how_many_days_before_account_deletion_send_warning_mail", 0) or 0
        ),
        courses=courses,
        log_file=log_file_from_raw(raw),
        watchdog_enabled=bool(watchdog.get("enabled", True)),
        watchdog_window_minutes=int(watchdog.get("window_minutes", 15)),
        watchdog_nginx_access_log=(watchdog.get("nginx_access_log") or None),
        watchdog_nginx_request_threshold=int(watchdog.get("nginx_request_threshold", 200)),
        watchdog_nginx_error_rate_threshold=float(watchdog.get("nginx_error_rate_threshold", 0.5)),
        watchdog_pending_signup_threshold=int(watchdog.get("pending_signup_threshold", 10)),
        watchdog_rate_limit_block_threshold=int(watchdog.get("rate_limit_block_threshold", 5)),
        watchdog_sshd_failure_threshold=int(watchdog.get("sshd_failure_threshold", 5)),
        watchdog_csp_violation_threshold=int(watchdog.get("csp_violation_threshold", 3)),
    )
