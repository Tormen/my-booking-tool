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

import hashlib
import logging
import os
import subprocess
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


# What blocker_kind() reports. Kept as constants rather than bare strings
# so /admin's status rendering and this module can never disagree by a
# typo.
BLOCKER_CANCELED = "canceled"
BLOCKER_HIDDEN = "hidden"


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


def _can_satisfy_requires(entry: ConflictCalendar, occ: ics_feed.FeedOccurrence) -> bool:
    """Is this event short enough to be a SLOT rather than an absence?

    Only asked in requires mode. A reserved slot is short -- half an
    hour, two hours -- while a workday block or a week away is long, and
    to "spans the course hours" the two are indistinguishable: a
    week-long out-of-office satisfied every course inside it, so the site
    offered sessions the operator was away for (2026-08-31, live).

    `requires_max_event_hours = 0` (the default) keeps the old behaviour
    exactly. An all-day event counts as its real length, so a cap of any
    ordinary size excludes those too -- which is the same intent, stated
    once instead of twice."""
    if entry.requires_max_event_hours <= 0:
        return True
    hours = (occ.end - occ.start).total_seconds() / 3600.0
    return hours <= entry.requires_max_event_hours


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
        # Record the failing state FIRST, before the rate-limit return
        # below: recovery (see _record_recovery) must be detectable even
        # when this WARNING email is suppressed as a repeat.
        self._mark_failing(entry)
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
            "You will get a follow-up 'RESOLVED:' email once it can be read again.\n"
            "This warning is sent at most once per day per source; `my-bt admin "
            "health` shows the current state at any time.\n"
        )
        try:
            self._send_warning_mail(subject, body)
            # WARNING level (shows in the default log): one greppable line
            # per alert email actually sent, mirroring what landed in the
            # operator's inbox.
            log.warning("conflict calendar %r: sent alert email -- %s", entry.name, subject)
        except Exception:
            log.exception("could not send conflict-source warning email for %r", entry.name)

    def _mark_failing(self, entry: ConflictCalendar) -> None:
        """Persist that this source is currently in a failed state, keeping
        the FIRST-failure timestamp (so the RESOLVED email can say how long
        it was down). Written on every failure regardless of the alert
        rate-limit; a no-op once the marker already exists."""
        marker = self.cache_dir / f"{_safe_filename(entry.name)}.failing"
        if marker.exists():
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(marker, self._now_fn().isoformat())
        except OSError:
            log.exception("could not persist failing marker for %r", entry.name)

    def _record_recovery(self, entry: ConflictCalendar) -> None:
        """Called after a source is read SUCCESSFULLY. If it had been
        failing (a .failing marker exists), clear both that marker and the
        alert rate-limit stamp (so a fresh incident can alert immediately),
        and email a 'RESOLVED:' notice -- the "calendar is back" message.
        A no-op (the overwhelmingly common case) when the source was not
        failing."""
        marker = self.cache_dir / f"{_safe_filename(entry.name)}.failing"
        try:
            since_text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return  # not failing -- nothing to announce
        try:
            since = datetime.fromisoformat(since_text)
            since_str = f"{since_text} ({self._humanize(self._now_fn() - since)})"
        except ValueError:
            since_str = since_text or "an unknown time"
        for f in (marker, self.cache_dir / f"{_safe_filename(entry.name)}.alert"):
            try:
                f.unlink()
            except OSError:
                pass
        subject = f"RESOLVED: conflict calendar '{entry.name}' is reachable again"
        body = (
            f"The conflict calendar source '{entry.name}' can be read again -- "
            "bookings for its affected course dates are back to normal.\n\n"
            f"It had been failing since {since_str}.\n\n"
            "`my-bt admin health` shows the current state at any time.\n"
        )
        # WARNING level so both the state change and the email are visible
        # in the default log (recovery is good news, but INFO is below the
        # default threshold -- same rationale as the failure ERROR lines).
        log.warning("conflict calendar %r: RESOLVED -- reachable again (was failing since %s)",
                    entry.name, since_str)
        try:
            self._send_warning_mail(subject, body)
            log.warning("conflict calendar %r: sent RESOLVED email -- %s", entry.name, subject)
        except Exception:
            log.exception("could not send conflict-source RESOLVED email for %r", entry.name)

    @staticmethod
    def _humanize(delta: timedelta) -> str:
        mins = max(0, int(delta.total_seconds() // 60))
        if mins < 60:
            return f"about {mins} min"
        return f"about {mins // 60}h {mins % 60:02d}min"

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
            age = time_mod.monotonic() - cached[0]
            if entry.debug:
                # WARNING so it shows in the default log with only the
                # per-source `debug = true` flag set (no MY_BOOKING_DEBUG).
                log.warning(
                    "[conflict-debug %s] pid=%d IN-PROCESS CACHE HIT (age %.3fs, ttl %ds) -- "
                    "NO fetch, NO rotation", entry.name, os.getpid(), age, entry.cache_minutes * 60,
                )
            else:
                # DEBUG only (routine): every consult within the TTL reuses
                # this in-process parse. One "fetched" line vs. many of these
                # is how you confirm a single render fetches the feed once --
                # turn on MY_BOOKING_DEBUG (or this source's `debug`) to trace.
                log.debug("conflict feed %r: served from in-process cache", entry.name)
            return cached[1]
        cache_file = self.cache_dir / f"{_safe_filename(entry.name)}.ics"
        if entry.debug:
            return self._ics_feed_debug(entry, cache_file)
        try:
            text = self._fetch(entry.ics_url)
            if "BEGIN:VCALENDAR" not in text[:2000]:
                raise ConflictSourceError(f"response from {entry.ics_url} is not an ICS calendar")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_cache_gitignore()
            rotated = self._rotate_previous(cache_file, text)
            atomic_write_text(cache_file, text)
            # DEBUG only (routine, one line per ACTUAL network fetch): bytes +
            # whether the content changed and so rotated .prev. Failures fetching
            # or rotating are logged at ERROR instead (see _last_known_good /
            # _rotate_previous), so they stay visible without MY_BOOKING_DEBUG.
            log.debug(
                "conflict feed %r: fetched %d bytes (%s)",
                entry.name, len(text),
                "changed -> rotated .prev" if rotated else "unchanged -> .prev kept",
            )
            self._record_recovery(entry)  # readable again after a prior failure
        except ConflictSourceError as exc:
            text = self._last_known_good(entry, cache_file, str(exc))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            text = self._last_known_good(entry, cache_file, f"fetch failed: {exc}")
        feed = ics_feed.parse_feed(text)
        self._feed_cache[entry.name] = (time_mod.monotonic(), feed)
        return feed

    @staticmethod
    def _debug_snapshot(path: Path) -> str:
        """One-line stat + sha256 of a cache file for the verbose debug
        trace. mtime is shown in LOCAL time (naive) so it lines up directly
        with what `stat`/`ls` print on the server; size and a short sha256
        make it obvious whether two snapshots are byte-identical."""
        try:
            st = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except FileNotFoundError:
            return "<absent>"
        except OSError as exc:
            return f"<stat error: {exc}>"
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S.%f")
        return f"size={st.st_size} mtime={mtime} sha256={digest}"

    def _ics_feed_debug(self, entry: ConflictCalendar, cache_file: Path) -> ics_feed.ParsedFeed:
        """Verbose one-fetch trace for a source with `debug = true`
        (2026-07-22). Deliberately does what the operator asked, in this
        exact order, all at WARNING (visible in the default log):

          1. log BEFORE stat+sha256 of .ics and .ics.prev, plus the pid;
          2. back up the CURRENT .ics to .ics.prev with a real
             `/bin/cp -a` (preserving mtime/owner/mode/SELinux context),
             BEFORE the network fetch -- so .ics.prev is a byte-identical
             copy of exactly the .ics that existed pre-fetch;
          3. fetch from the source, log byte count + sha256 + duration;
          4. write the new .ics, log AFTER stat+sha256.

        Because the backup in step 2 is unconditional and happens before
        the fetch, .ics.prev MUST equal the pre-fetch .ics after ONE fetch.
        If a single request still ends with .ics.prev != the .ics you saw
        before it, the log will show a SECOND `FETCH BEGIN ... END` block
        (same pid = one render fetching twice; different pid = a second
        process), which is the whole point of this mode. Diagnostic only --
        this path skips the normal rotate-only-on-change logic."""
        prev_file = cache_file.with_name(cache_file.name + ".prev")
        tag = f"[conflict-debug {entry.name}]"
        pid = os.getpid()
        log.warning("%s pid=%d ===== FETCH BEGIN (in-process cache expired/absent) =====", tag, pid)
        log.warning("%s pid=%d BEFORE  .ics      '%s' %s", tag, pid, cache_file, self._debug_snapshot(cache_file))
        log.warning("%s pid=%d BEFORE  .ics.prev '%s' %s", tag, pid, prev_file, self._debug_snapshot(prev_file))
        if cache_file.exists():
            cp = subprocess.run(
                ["/bin/cp", "-a", str(cache_file), str(prev_file)],
                capture_output=True, text=True, check=False,
            )
            detail = (cp.stderr or cp.stdout).strip()
            log.warning(
                "%s pid=%d BACKUP  /bin/cp -a .ics .ics.prev -> rc=%d%s",
                tag, pid, cp.returncode, f" ({detail})" if detail else "",
            )
            log.warning("%s pid=%d AFTER-CP .ics.prev '%s' %s", tag, pid, prev_file, self._debug_snapshot(prev_file))
        else:
            log.warning("%s pid=%d BACKUP  skipped -- no .ics on disk yet (first-ever fetch)", tag, pid)
        t0 = time_mod.monotonic()
        try:
            text = self._fetch(entry.ics_url)
            dt = time_mod.monotonic() - t0
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
            log.warning(
                "%s pid=%d FETCHED %d bytes in %.3fs sha256=%s", tag, pid, len(text), dt, digest,
            )
            if "BEGIN:VCALENDAR" not in text[:2000]:
                raise ConflictSourceError(f"response from {entry.ics_url} is not an ICS calendar")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_cache_gitignore()
            atomic_write_text(cache_file, text)
            log.warning("%s pid=%d AFTER   .ics      '%s' %s", tag, pid, cache_file, self._debug_snapshot(cache_file))
            self._record_recovery(entry)  # readable again after a prior failure
        except (ConflictSourceError, urllib.error.URLError, OSError, ValueError) as exc:
            log.error("%s pid=%d FETCH FAILED after %.3fs: %s -- using last-known-good",
                      tag, pid, time_mod.monotonic() - t0, exc)
            text = self._last_known_good(entry, cache_file, f"fetch failed: {exc}")
        feed = ics_feed.parse_feed(text)
        self._feed_cache[entry.name] = (time_mod.monotonic(), feed)
        log.warning("%s pid=%d ===== FETCH END =====", tag, pid)
        return feed

    def _rotate_previous(self, cache_file: Path, new_text: str) -> bool:
        """Before overwriting the last-known-good cache with a CHANGED
        feed, move the current copy aside to `<name>.ics.prev`, so there
        is ALWAYS an explicit previous version to diff the just-arrived
        changes against on the server:

            diff <data_dir>/conflict_cache/<name>.ics.prev \\
                 <data_dir>/conflict_cache/<name>.ics

        Only rotated when the content actually changed. An unchanged
        re-fetch -- the common case, once every `cache_minutes` -- leaves
        `.prev` untouched, so it keeps pointing at the genuinely previous
        *distinct* version rather than being flattened into a copy of the
        current one. `.prev` lives inside conflict_cache/, already excluded
        from data-dir git snapshots (see _ensure_cache_gitignore).

        Returns True iff it actually rotated (content changed and the copy
        was written) -- the caller uses this only for the DEBUG "fetched"
        line. A REAL failure reading the current copy or writing `.prev` is
        logged at ERROR (always visible, per the "any error rotating the
        .ics is ERROR" rule) but never raised: preserving a diff copy is an
        operator convenience, never part of the conflict decision."""
        try:
            old_text = cache_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False  # first-ever fetch: no current cache to preserve -- normal, not an error
        except OSError:
            log.exception("conflict cache %s: could not read current copy to rotate .prev", cache_file)
            return False
        if old_text == new_text:
            return False
        prev_file = cache_file.with_name(cache_file.name + ".prev")
        try:
            atomic_write_text(prev_file, old_text)
        except OSError:
            log.exception("conflict cache %s: could not write previous-version copy", prev_file)
            return False
        return True

    def _last_known_good(self, entry: ConflictCalendar, cache_file: Path, problem: str) -> str:
        # ERROR, not WARNING (2026-07-22): a failure to fetch the .ics is
        # always surfaced in the default (MY_BOOKING_DEBUG-off) log, even
        # though bookings continue against the cached copy -- the operator
        # asked that any error fetching or rotating the feed be visible
        # without turning debug on. The DEBUG "fetched N bytes" success line
        # is the only feed-log that stays hidden by default.
        log.error("conflict calendar %r: %s", entry.name, problem)
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
            # ERROR, not WARNING (2026-07-22): same rule as the ICS
            # _last_known_good above -- a conflict source that can't be
            # consulted is always visible in the default log. A CalDAV
            # source fails closed (dates hidden), which is if anything more
            # worth surfacing than the ICS case (no cached copy to fall
            # back on), so it must not be quieter than an ICS failure.
            log.error("conflict calendar %r: CalDAV error: %s", entry.name, exc)
            self._alert(entry, f"CalDAV error: {exc}")
            raise ConflictSourceError(str(exc)) from None
        self._record_recovery(entry)  # query succeeded -- readable again after a prior failure
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

        # Booking-tool internal, always on: a "cancel entire session"
        # blocker event on the booking calendar must hide the date for
        # EVERY course, independent of any [[conflict_calendar]] scoping
        # (courses / all_courses_but). It is the tool's own cancellation
        # mechanism, not a user-configured conflict, so it must not be
        # scopable away. When a blocks-mode booking_calendar entry already
        # applies to this course, its generic overlap check below catches
        # the blocker too, so the dedicated (one extra query) check runs
        # ONLY for a course scoped OUT of every such entry (e.g. a course
        # excluded via all_courses_but so its real availability is decided
        # by a different, requires-mode source). Fail-closed: if the
        # booking calendar can't be read, no booking should happen -- the
        # same policy as any conflict source.
        if not self._booking_calendar_blocks_cover(course.shortname):
            try:
                if self._cancellation_blocker_present(course, occ_date, tz):
                    return True
            except ConflictSourceError:
                return True

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
                candidates = [o for o in occs if _can_satisfy_requires(entry, o)]
                if not any(self._spans(o, occ_date, win_start, win_end) for o in candidates):
                    return True
        return False

    def hidden_dates(
        self, course: Course, occurrences: list[tuple[date, datetime, datetime]]
    ) -> dict[date, bool]:
        """{date: is-it-hidden} for MANY occurrences of one course, with
        each source read ONCE for the whole span instead of once per date.

        Same verdict as calling occurrence_is_hidden() per date -- this is
        purely about the number of round-trips. /admin's Future Sessions
        box lists 52 dates per course; per-date reads would mean 52 CalDAV
        queries per conflict source per tab, which is the difference
        between a page that opens and one that does not.

        Fails closed exactly like the per-date path: a source that cannot
        be read hides every date it applies to, because "we do not know"
        must never render as "bookable"."""
        if not occurrences:
            return {}
        tz = ZoneInfo(self.settings.timezone)
        span_start = min(o[1] for o in occurrences)
        span_end = max(o[2] for o in occurrences)
        first_date, last_date = occurrences[0][0], occurrences[-1][0]
        hidden = {occ_date: False for occ_date, _s, _e in occurrences}

        if not self._booking_calendar_blocks_cover(course.shortname):
            try:
                kinds = self.blocker_kinds_in_window(course, first_date, last_date)
            except ConflictSourceError:
                return {d: True for d in hidden}
            for occ_date in kinds:
                if occ_date in hidden:
                    hidden[occ_date] = True

        for entry in self.settings.conflict_calendars:
            if not entry.applies_to(course.shortname):
                continue
            # ONE read per source for the whole span. The window is padded
            # to whole local days so all-day events at either end are in
            # scope, matching what the per-date path asks for.
            day_start = datetime.combine(first_date, time(0, 0), tzinfo=tz)
            day_end = datetime.combine(last_date, time(0, 0), tzinfo=tz) + timedelta(days=1)
            try:
                if entry.ics_url:
                    feed = self._ics_feed(entry)
                    fetched = ics_feed.expand(feed, day_start, day_end, tz)
                else:
                    fetched = self._caldav_occurrences(
                        entry, min(span_start, day_start), max(span_end, day_end)
                    )
            except ConflictSourceError:
                return {d: True for d in hidden}
            fetched = [o for o in fetched if _matches_entry(entry, o)]
            fetched = [
                o for o in fetched
                if not calendar_sync.is_own_event(o.uid, self.settings)
            ]
            for occ_date, occ_start, occ_end in occurrences:
                if hidden[occ_date]:
                    continue
                win_start, win_end = self._entry_window(entry, occ_date, occ_start, occ_end, tz)
                if entry.mode == "blocks":
                    if any(self._overlaps(o, occ_date, win_start, win_end) for o in fetched):
                        hidden[occ_date] = True
                else:  # requires
                    # Same slot-vs-absence rule as occurrence_is_hidden --
                    # this is the batched path for /admin and /book, and
                    # the two must never disagree about a date.
                    candidates = [o for o in fetched if _can_satisfy_requires(entry, o)]
                    if not any(self._spans(o, occ_date, win_start, win_end) for o in candidates):
                        hidden[occ_date] = True
        return hidden

    def _booking_calendar_blocks_cover(self, course_shortname: str) -> bool:
        """Does some blocks-mode [[conflict_calendar]] entry ON THE BOOKING
        CALENDAR already apply to this course? If so, its normal overlap
        check already catches any CANCELED blocker event on that calendar,
        so the dedicated _cancellation_blocker_present query is redundant.
        Mirrors the booking-calendar-identity test in
        cli_checks.check_caldav_calendars (source = "booking_calendar", or
        the same caldav_url + calendar as [booking_calendar])."""
        for e in self.settings.conflict_calendars:
            if e.mode != "blocks" or not e.applies_to(course_shortname):
                continue
            if e.use_booking_calendar or (
                e.caldav_url == self.settings.caldav_url
                and e.calendar == self.settings.booking_calendar
            ):
                return True
        return False

    def _cancellation_blocker_present(self, course: Course, occ_date: date, tz) -> bool:
        """True iff one of THIS occurrence's own blocker events exists on
        the booking calendar -- either the 'cancel entire session' one or
        (since 2026-08-27) the 'hide this date' one. Both keep the date
        off the booking page; they differ only in what they mean to the
        operator, which blocker_kind() below reports.

        Keyed on the exact blocker UIDs, so a genuine personal event on
        the same calendar is NOT what triggers it -- that stays governed
        by the scoped [[conflict_calendar]] entries. Raises
        ConflictSourceError if the booking calendar can't be read, so the
        caller fails closed."""
        return self.blocker_kind(course, occ_date, tz) is not None

    def blocker_kind(self, course: Course, occ_date: date, tz=None) -> str | None:
        """Which of this tool's own blockers sits on `occ_date`:
        BLOCKER_CANCELED, BLOCKER_HIDDEN, or None. One query, both UIDs --
        /admin needs to tell the two apart (a canceled date is reopened by
        rebooking someone, a hidden one by Unhide), and the booking page
        only needs "either"."""
        tz = tz or ZoneInfo(self.settings.timezone)
        kinds = self.blocker_kinds_in_window(course, occ_date, occ_date)
        return kinds.get(occ_date)

    def blocker_kinds_in_window(
        self, course: Course, first_date: date, last_date: date
    ) -> dict[date, str]:
        """{date: BLOCKER_*} for every one of this course's blockers
        between the two dates inclusive -- in ONE booking-calendar query.

        This is the batched form /admin's Future Sessions box needs: it
        shows 52 dates per course, and asking the CalDAV server once per
        date would be 52 round-trips per tab. The UIDs are deterministic,
        so a single windowed query answers all of them."""
        tz = ZoneInfo(self.settings.timezone)
        win_start = datetime.combine(first_date, time(0, 0), tzinfo=tz)
        win_end = datetime.combine(last_date, time(0, 0), tzinfo=tz) + timedelta(days=1)
        try:
            client, href = self._booking_client_fn(), self._booking_href_fn()
            events = client.query_events(href, win_start, win_end)
        except (CalDAVError, OSError) as exc:
            log.error(
                "blocker check for %r %s..%s: booking calendar error: %s",
                course.shortname, first_date.isoformat(), last_date.isoformat(), exc,
            )
            raise ConflictSourceError(str(exc)) from None

        wanted: dict[str, tuple[date, str]] = {}
        d = first_date
        while d <= last_date:
            wanted[calendar_sync.cancellation_blocker_uid(self.settings, course.shortname, d)] = \
                (d, BLOCKER_CANCELED)
            wanted[calendar_sync.hide_blocker_uid(self.settings, course.shortname, d)] = \
                (d, BLOCKER_HIDDEN)
            d += timedelta(days=1)

        found: dict[date, str] = {}
        for uid, _ics, _etag in events:
            hit = wanted.get(uid)
            if hit is None:
                continue
            occ_date, kind = hit
            # A canceled date is canceled even if a stale hide blocker is
            # also lying around: cancellation is the stronger statement.
            if found.get(occ_date) != BLOCKER_CANCELED:
                found[occ_date] = kind
        return found

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
