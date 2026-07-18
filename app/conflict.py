"""The [[conflict_calendar]] engine (2026-07-18 settings redesign,
SOLUTION-DESIGN #35): decides, per course occurrence, whether a date is
possible, by consulting every configured READ-ONLY calendar source that
applies to that course.

Two entry modes (see config.ConflictCalendar):
- "blocks":   any matching event overlapping the occurrence window hides
              the date (vacation entries, CANCELED blocker events).
- "requires": a SINGLE matching event must span the whole from-till
              window, or the date is hidden ("courses only happen when
              the work calendar shows an out-of-office event").

Source-error policy (operator's explicit design, 2026-07-18):
- ICS sources keep a last-known-good copy of every successful fetch
  under <data_dir>/conflict_cache/ and fall back to it INDEFINITELY on
  fetch errors -- plus a "WARNING: ..."-subject email to admin_email,
  rate-limited to at most one per day per source. A fetch error with no
  cached copy at all hides the affected dates (fail-closed) with the
  same email.
- CalDAV sources have no meaningful single document to cache; an error
  hides the affected dates (fail-closed) with the same rate-limited
  WARNING email.

The conflict_cache/ dir lives inside data_dir (the one directory the
service user always owns) but is kept OUT of the data-dir git snapshots
via data_dir/.gitignore -- git_snapshot stages `git add -A`, and a 1 MB
third-party calendar export has no business in the booking-data history.
"""
from __future__ import annotations

import logging
import time as time_mod
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import calendar_sync, ics_feed
from .atomic_io import atomic_write_text
from .caldav_client import CalDAVClient, CalDAVError
from .config import ConflictCalendar, Course, Settings

log = logging.getLogger("my-booking.conflict")

_FETCH_TIMEOUT_SECONDS = 10
_ALERT_MIN_INTERVAL = timedelta(days=1)


class ConflictSourceError(Exception):
    """A conflict source could not be consulted (and, for ICS, no cached
    copy exists) -- callers hide the affected dates."""


def _default_fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "my-booking-tool"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "unnamed"


def _effective_status(occ: ics_feed.FeedOccurrence) -> str:
    """One canonical lowercase status per occurrence: Outlook's
    X-MICROSOFT-CDO-BUSYSTATUS when present, else RFC TRANSP
    (TRANSPARENT -> free, default OPAQUE -> busy)."""
    if occ.busy_status:
        return occ.busy_status.lower()
    return "free" if occ.transparent else "busy"


def _matches_entry(entry: ConflictCalendar, occ: ics_feed.FeedOccurrence) -> bool:
    """Does this occurrence COUNT for the entry at all (show_as + title +
    the all-day knobs)? Mode-independent -- blocks/requires decide what a
    counting event MEANS, not which events count."""
    if entry.show_as != "any" and _effective_status(occ) != entry.show_as:
        return False
    if entry.title_contains and entry.title_contains.lower() not in occ.summary.lower():
        return False
    if occ.all_day:
        if not entry.all_day_events_also_count:
            return False
        marker = entry.all_day_non_blocking_title_marker
        if marker and marker.lower() in occ.summary.lower():
            return False
        if entry.all_day_free_events_do_not_block and _effective_status(occ) == "free":
            return False
    return True


class ConflictEngine:
    """Owns per-source fetching/caching/alerting and the per-date
    decision. One instance lives on the App for the whole process, so the
    in-process ICS cache actually caches across requests. Every side
    effect is injectable for tests: `fetch` (ICS HTTP GET), the CalDAV
    `client_factory`, `send_warning_mail`, and `now_fn`."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: Path,
        *,
        booking_client_fn: Callable[[], CalDAVClient],
        booking_href_fn: Callable[[], str],
        client_factory: Callable[[ConflictCalendar], CalDAVClient] | None = None,
        fetch: Callable[[str], str] = _default_fetch,
        send_warning_mail: Callable[[str, str], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.cache_dir = Path(cache_dir)
        self._booking_client_fn = booking_client_fn
        self._booking_href_fn = booking_href_fn
        self._client_factory = client_factory or self._real_client
        self._fetch = fetch
        self._send_warning_mail = send_warning_mail or self._real_send_warning_mail
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        # entry name -> (fetched_monotonic, ParsedFeed); per-process TTL
        # cache so the booking page doesn't re-download a 1 MB feed on
        # every load.
        self._feed_cache: dict[str, tuple[float, ics_feed.ParsedFeed]] = {}
        self._caldav_hrefs: dict[str, tuple[CalDAVClient, str]] = {}

    # -- side-effect defaults --------------------------------------------------

    @staticmethod
    def _real_client(entry: ConflictCalendar) -> CalDAVClient:
        return CalDAVClient(entry.caldav_url, entry.caldav_username, entry.caldav_password)

    def _real_send_warning_mail(self, subject: str, body: str) -> None:
        from . import emailer
        emailer.send_mail(self.settings, self.settings.admin_email, subject, body)

    # -- alert rate limiting ---------------------------------------------------

    def _alert(self, entry: ConflictCalendar, problem: str) -> None:
        """Email the operator about a broken source -- subject starts
        with "WARNING:" (explicit requirement), at most one email per day
        per source (state = one timestamp file next to the cache; a page
        load during an hours-long outage must not send hundreds)."""
        stamp = self.cache_dir / f"{_safe_filename(entry.name)}.alert"
        now = self._now_fn()
        try:
            last = datetime.fromisoformat(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = None
        if last is not None and now - last < _ALERT_MIN_INTERVAL:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(stamp, now.isoformat())
        except OSError:
            log.exception("could not persist alert timestamp for %r", entry.name)
        subject = f"WARNING: conflict calendar '{entry.name}' has a problem"
        body = (
            f"The conflict calendar source '{entry.name}' could not be read:\n\n"
            f"  {problem}\n\n"
            f"{self._alert_consequence(entry)}\n\n"
            "This warning is sent at most once per day per source; `my-bt admin "
            "health` shows the current state at any time.\n"
        )
        try:
            self._send_warning_mail(subject, body)
        except Exception:
            log.exception("could not send conflict-source warning email for %r", entry.name)

    def _alert_consequence(self, entry: ConflictCalendar) -> str:
        cached = self.cache_dir / f"{_safe_filename(entry.name)}.ics"
        if entry.ics_url and cached.exists():
            try:
                age = self._now_fn() - datetime.fromtimestamp(cached.stat().st_mtime, tz=timezone.utc)
                hours = int(age.total_seconds() // 3600)
                return (
                    f"Bookings continue against the last successfully fetched copy "
                    f"(about {hours}h old). Dates may not reflect recent calendar changes."
                )
            except OSError:
                pass
        return "Affected course dates are HIDDEN from the booking page until the source works again."

    # -- ICS source ------------------------------------------------------------

    def _ensure_cache_gitignore(self) -> None:
        """conflict_cache/ sits inside data_dir, which git_snapshot
        stages wholesale (`git add -A`) -- keep the fetched feeds out of
        the booking-data history."""
        gitignore = self.cache_dir.parent / ".gitignore"
        line = self.cache_dir.name + "/"
        try:
            existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            if line not in existing.split("\n"):
                atomic_write_text(gitignore, existing.rstrip("\n") + ("\n" if existing else "") + line + "\n")
        except OSError:
            log.exception("could not update %s", gitignore)

    def _ics_feed(self, entry: ConflictCalendar) -> ics_feed.ParsedFeed:
        cached = self._feed_cache.get(entry.name)
        if cached is not None and time_mod.monotonic() - cached[0] < entry.cache_minutes * 60:
            return cached[1]
        cache_file = self.cache_dir / f"{_safe_filename(entry.name)}.ics"
        try:
            text = self._fetch(entry.ics_url)
            if "BEGIN:VCALENDAR" not in text[:2000]:
                raise ConflictSourceError(f"response from {entry.ics_url} is not an ICS calendar")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_cache_gitignore()
            atomic_write_text(cache_file, text)
        except ConflictSourceError as exc:
            text = self._last_known_good(entry, cache_file, str(exc))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            text = self._last_known_good(entry, cache_file, f"fetch failed: {exc}")
        feed = ics_feed.parse_feed(text)
        self._feed_cache[entry.name] = (time_mod.monotonic(), feed)
        return feed

    def _last_known_good(self, entry: ConflictCalendar, cache_file: Path, problem: str) -> str:
        log.warning("conflict calendar %r: %s", entry.name, problem)
        self._alert(entry, problem)
        try:
            return cache_file.read_text(encoding="utf-8")
        except OSError:
            raise ConflictSourceError(
                f"{problem} (and no cached copy exists yet at {cache_file})"
            ) from None

    # -- CalDAV source ---------------------------------------------------------

    def _caldav_occurrences(
        self, entry: ConflictCalendar, window_start: datetime, window_end: datetime,
    ) -> list[ics_feed.FeedOccurrence]:
        try:
            if entry.use_booking_calendar:
                client, href = self._booking_client_fn(), self._booking_href_fn()
            else:
                cached = self._caldav_hrefs.get(entry.name)
                if cached is None:
                    client = self._client_factory(entry)
                    calendars = client.list_calendars()
                    if entry.calendar not in calendars:
                        raise CalDAVError(
                            f"calendar {entry.calendar!r} not found among {list(calendars)}"
                        )
                    cached = (client, calendars[entry.calendar])
                    self._caldav_hrefs[entry.name] = cached
                client, href = cached
            events = client.query_events(href, window_start, window_end)
        except (CalDAVError, OSError) as exc:
            log.warning("conflict calendar %r: CalDAV error: %s", entry.name, exc)
            self._alert(entry, f"CalDAV error: {exc}")
            raise ConflictSourceError(str(exc)) from None
        tz = ZoneInfo(self.settings.timezone)
        out: list[ics_feed.FeedOccurrence] = []
        for _uid, ics_text, _etag in events:
            # Each REPORT item is a small VCALENDAR of its own -- run it
            # through the same feed parser/expander as an ICS link, so
            # recurring events and VTIMEZONEs behave identically for
            # both source kinds.
            out.extend(ics_feed.expand(ics_feed.parse_feed(ics_text), window_start, window_end, tz))
        return out

    # -- the decision ----------------------------------------------------------

    def occurrence_is_hidden(
        self, course: Course, occ_start: datetime, occ_end: datetime, *, exclude_own: bool = True,
    ) -> bool:
        """True = this occurrence must NOT be bookable ("no slot shown =
        no session"): some blocks-entry has a matching overlapping event,
        some requires-entry has no single matching event spanning its
        window, or a source failed in a fail-closed way."""
        tz = ZoneInfo(self.settings.timezone)
        occ_date = occ_start.astimezone(tz).date()
        for entry in self.settings.conflict_calendars:
            if not entry.applies_to(course.shortname):
                continue
            win_start, win_end = self._entry_window(entry, occ_date, occ_start, occ_end, tz)
            try:
                if entry.ics_url:
                    feed = self._ics_feed(entry)
                    # Whole local day, so all-day events are in scope.
                    day_start = datetime.combine(occ_date, time(0, 0), tzinfo=tz)
                    occs = ics_feed.expand(feed, day_start, day_start + timedelta(days=1), tz)
                else:
                    occs = self._caldav_occurrences(entry, win_start, win_end)
            except ConflictSourceError:
                return True
            occs = [o for o in occs if _matches_entry(entry, o)]
            if exclude_own:
                occs = [o for o in occs if not calendar_sync.is_own_event(o.uid, self.settings)]
            if entry.mode == "blocks":
                if any(self._overlaps(o, occ_date, win_start, win_end) for o in occs):
                    return True
            else:  # requires: a single event must span the whole window
                if not any(self._spans(o, occ_date, win_start, win_end) for o in occs):
                    return True
        return False

    @staticmethod
    def _entry_window(
        entry: ConflictCalendar, occ_date: date, occ_start: datetime, occ_end: datetime, tz,
    ) -> tuple[datetime, datetime]:
        """from/till applied on the occurrence's local date; either side
        left "" keeps the occurrence's own (override-aware) time."""
        def at(hm: str) -> datetime:
            h, m = hm.split(":")
            return datetime.combine(occ_date, time(int(h), int(m)), tzinfo=tz)
        start = at(entry.from_hm) if entry.from_hm else occ_start
        end = at(entry.till_hm) if entry.till_hm else occ_end
        return start, end

    @staticmethod
    def _overlaps(o: ics_feed.FeedOccurrence, occ_date: date, win_start: datetime, win_end: datetime) -> bool:
        if o.all_day:
            return o.day_start <= occ_date < o.day_end
        return o.start < win_end and o.end > win_start

    @staticmethod
    def _spans(o: ics_feed.FeedOccurrence, occ_date: date, win_start: datetime, win_end: datetime) -> bool:
        if o.all_day:
            # A matching all-day event covers the whole day by definition.
            return o.day_start <= occ_date < o.day_end
        return o.start <= win_start and o.end >= win_end
