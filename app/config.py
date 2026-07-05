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

    WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def weekday_index(self) -> int:
        return self.WEEKDAYS.index(self.weekday.lower())

    def start_hm(self) -> tuple[int, int]:
        h, m = self.start_time.split(":")
        return int(h), int(m)


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

    courses: tuple[Course, ...] = field(default_factory=tuple)

    # Optional: also write logs to this file (in addition to stdout/journal
    # -- see app/logutil.py). None (the default, and what a settings.toml
    # without a [logging] section gets) means stdout/journal only.
    log_file: str | None = None

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

    # Optional: absolute path to the LIVE, web-served copy of site/ (the
    # separate checkout/host location -- see README.md "Static-site
    # pages"), e.g. "/var/www/example.org". If set, `my-bt status` compares
    # its privacy.html against this settings.toml's retention numbers and
    # warns on drift (see app/cli_checks.py::check_static_site_drift), and
    # `my-bt setup -i` can regenerate it directly there (app/site_render.py)
    # -- no rebuild/reinstall needed just to pick up a config-only change.
    # None (the default) means this check/action is skipped entirely.
    static_site_dir: str | None = None

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

    courses = tuple(
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
        )
        for c in raw.get("course", [])
    )

    shortnames = [c.shortname for c in courses]
    if len(shortnames) != len(set(shortnames)):
        raise ValueError("duplicate course shortname in settings.toml")

    return Settings(
        timezone=site["timezone"],
        admin_email=site["admin_email"],
        base_url=site["base_url"].rstrip("/"),
        static_site_dir=(site.get("static_site_dir") or None),
        caldav_url=cal["caldav_url"],
        caldav_username=cal["caldav_username"],
        caldav_password=_read_secret(cal["caldav_password_file"]),
        booking_calendar=cal["booking_calendar"],
        conflict_calendars=tuple(cal.get("conflict_calendars", [])),
        smtp_host=smtp["host"],
        smtp_port=int(smtp["port"]),
        smtp_username=smtp["username"],
        smtp_password=_read_secret(smtp["password_file"]),
        smtp_from=smtp["from_address"],
        admin_password_hash=_read_secret(admin["password_hash_file"]),
        show_next_slots=int(defaults.get("show_next_slots", 4)),
        show_next_days=int(defaults.get("show_next_days", 42)),
        min_notice_hours=int(defaults.get("min_notice_hours", 2)),
        show_spots_left=bool(defaults.get("show_spots_left", True)),
        spots_left_offset=int(defaults.get("spots_left_offset", 0)),
        min_required_participants=int(defaults.get("min_required_participants", 1)),
        retention_months=int(privacy.get("retention_months", 24)),
        canceled_retention_months=int(privacy.get("canceled_retention_months", 6)),
        erasure_pepper=bytes.fromhex(_read_secret(privacy["erasure_pepper_file"])),
        courses=courses,
        log_file=(logging_cfg.get("log_file") or None),
    )
