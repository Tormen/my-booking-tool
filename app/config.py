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
    # Optional map/directions link (2026-07-09, the operator: "add a location_url
    # and then use it on /my in the column location to make those
    # clickable"). "" (the default -- key omitted in settings.toml) means
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
    # Determines this course's position on /courses (2026-07-09, the operator:
    # "add a sorting key ... allowing me to determine the ORDER of the
    # courses"; renamed from the original `order` same day, the operator: "please
    # rename to something like order_in_all_courses to be self-explanatory
    # how this 'order' is actually USED" -- the bare word "order" alone
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
        (2026-07-10, the operator: "add the weekday to the TIME column (e.g. SAT
        10h45-12h45)")."""
        return f"{self.weekday.upper()} {self._fmt_hm(*self.start_hm())}-{self._fmt_hm(*self.end_hm())}"


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
    # Optional (2026-07-09, the operator: "Our scheduler that then deletes accounts
    # should detect imminent accounts that would need to be deleted and
    # then send out such an email" -- a dormant-account warning, one email,
    # before deletion, similar to a Notion account-cleanup notice he
    # forwarded as an example). 0 (the default -- also what "" or a
    # commented-out/omitted key parse to, see load_settings()) disables
    # this entirely: no warning is ever sent. When positive, it's how many
    # days BEFORE an account would reach `retention_months` of inactivity
    # (see app.retention.send_account_deletion_warnings) that the ONE
    # warning email goes out -- reusing retention_months as the actual
    # dormancy threshold rather than adding a second duration setting,
    # per the operator: "there is already a variable that defines the duration".
    # "Inactivity" is User.last_login_at, falling back to created_at for an
    # account that has never logged in again since booking.
    account_deletion_warning_days: int = 0

    courses: tuple[Course, ...] = field(default_factory=tuple)

    # Calendar-invite VALARM reminders (minutes before start) -- see
    # ics.py::VEvent.alarms_minutes_before. 2026-07-07, the operator: "make the
    # reminders (list) a setting. But default to NO reminders" for the
    # TRAINER's own event (app/calendar_sync.py::sync_occurrence, the one
    # PUT to the operator's CalDAV calendar), while course PARTICIPANTS'
    # emailed invite (guest_invite_ics) should default to exactly one
    # reminder, 1h before. Both configurable, in case that changes later;
    # empty tuple = no VALARMs at all (matches VEvent's own default now).
    trainer_calendar_reminder_minutes: tuple[int, ...] = ()
    guest_calendar_reminder_minutes: tuple[int, ...] = (60,)

    # Optional: also write logs to this file (in addition to stdout/journal
    # -- see app/logutil.py). None (the default, and what a settings.toml
    # without a [logging] section gets) means stdout/journal only.
    log_file: str | None = None

    # Optional, comma-separated (2026-07-09, the operator: "add as BCC the given
    # email address to all mails that go out to the attendees ... so that
    # for some time I can watch this to ensure that all is OK"). Applied
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
    # form offers, and how many guest_email_N/guest_name_N fields book()
    # will ever look for on a submitted form -- see app/webapp.py's
    # MAX_GUESTS docstring. 2026-07-09, the operator: "add a setting for the max
    # number of guests ... default to 3" (was a fixed constant of 9 before).
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

    # Optional: a hostname (typically your own dynamic-DNS name, e.g.
    # "ssh.example.net") whose CURRENT resolved IP is allowed to keep using
    # /courses and /book/<shortname> as normal even while maintenance mode
    # (app/maintenance.py) is ON for everyone else (2026-07-10, the operator: "can
    # the maintenance mode still let me access the site from
    # ssh.example.net please?"). Resolved fresh on every request while
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
    # fresh by infrastructure outside this app (2026-07-10, the operator: "if you
    # need an IP this changes and the latest can be found in
    # /home/me/my-ip.log, but else the DNS also auto-updates!"). Mirrors
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


def peek_log_file(toml_path: str | Path) -> str | None:
    """Read just settings.toml's [logging].log_file, without requiring the
    secret files load_settings() needs -- used by `my-bt -L/--log`, since
    several my-bt subcommands (list/users/show/stats) never call
    load_settings() at all, and shouldn't have to just to find a log path."""
    raw = load_raw_toml(toml_path)
    if raw is None:
        return None
    return raw.get("logging", {}).get("log_file") or None


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
    logging_cfg = raw.get("logging", {})
    watchdog = raw.get("watchdog", {})

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
    courses = tuple(sorted(courses, key=lambda c: c.order_in_all_courses))

    return Settings(
        timezone=site["timezone"],
        admin_email=site["admin_email"],
        base_url=site["base_url"].rstrip("/"),
        static_site_dir=(site.get("static_site_dir") or None),
        maintenance_bypass_hostname=(site.get("maintenance_bypass_hostname") or None),
        maintenance_bypass_ip_log=(site.get("maintenance_bypass_ip_log") or None),
        caldav_url=cal["caldav_url"],
        caldav_username=cal["caldav_username"],
        caldav_password=_read_secret(cal["caldav_password_file"]),
        booking_calendar=cal["booking_calendar"],
        conflict_calendars=tuple(cal.get("conflict_calendars", [])),
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
        # `or 0` collapses every "off" spelling the operator asked for (0, "", or
        # the key omitted entirely -- privacy.get's own default) to the
        # same falsy value in one step: 0/""/None are all falsy in Python,
        # so only a genuinely truthy (non-zero, non-blank) value reaches
        # int() at all.
        account_deletion_warning_days=int(
            privacy.get("how_many_days_before_account_deletion_send_warning_mail", 0) or 0
        ),
        courses=courses,
        log_file=(logging_cfg.get("log_file") or None),
        watchdog_enabled=bool(watchdog.get("enabled", True)),
        watchdog_window_minutes=int(watchdog.get("window_minutes", 15)),
        watchdog_nginx_access_log=(watchdog.get("nginx_access_log") or None),
        watchdog_nginx_request_threshold=int(watchdog.get("nginx_request_threshold", 200)),
        watchdog_nginx_error_rate_threshold=float(watchdog.get("nginx_error_rate_threshold", 0.5)),
        watchdog_pending_signup_threshold=int(watchdog.get("pending_signup_threshold", 10)),
        watchdog_rate_limit_block_threshold=int(watchdog.get("rate_limit_block_threshold", 5)),
        watchdog_sshd_failure_threshold=int(watchdog.get("sshd_failure_threshold", 5)),
    )
