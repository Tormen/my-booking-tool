"""Read-only iCalendar FEED parsing, for [[conflict_calendar]] ICS
sources (published calendar links, e.g. Outlook/OWA "calendar.ics").

app/ics.py stays the minimal one-VEVENT build/parse used for our own
synced events; THIS module is the other direction -- consuming somebody
else's whole calendar export, which needs three things ics.py
deliberately doesn't have (verified against the real feed this feature
was built for: ~1500 events, 87 RRULEs, 309 RECURRENCE-ID overrides,
Windows-named TZIDs):

- whole-file parsing into events (SUMMARY / DTSTART / DTEND / DURATION /
  TRANSP / X-MICROSOFT-CDO-BUSYSTATUS / STATUS / RRULE / EXDATE /
  RECURRENCE-ID),
- VTIMEZONE-based offset resolution: Outlook ships Windows TZID names
  ("W. Europe Standard Time") but ALSO ships each zone's transition
  rules right in the feed -- offsets are resolved from those rules
  directly, so no Windows->IANA name mapping table to maintain,
- RRULE expansion: DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT,
  UNTIL, BYDAY (incl. ordinal forms like 2TU / -1FR), BYMONTHDAY,
  BYMONTH, plus EXDATE and RECURRENCE-ID instance overrides. An RRULE
  part outside that subset logs one WARNING and the event falls back to
  its DTSTART occurrence only -- visible, never silently dropped.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo

log = logging.getLogger("my-booking.ics-feed")

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Iteration safety net per event: a weekly rule that started years before
# today's window still terminates quickly (few hundred steps); anything
# hitting this cap is malformed enough to stop expanding.
_MAX_INSTANCES = 10_000


def _unfold(text: str) -> str:
    return (
        text.replace("\r\n ", "").replace("\r\n\t", "")
        .replace("\n ", "").replace("\n\t", "")
    )


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """'NAME;PARAM=x;OTHER=y:value' -> (NAME, {PARAM: x, ...}, value).
    Parameter values may be double-quoted (RFC 5545 allows a colon inside
    quotes -- 'TZID="A: B"'), so the name/value split scans for the first
    colon outside quotes instead of a plain find(":")."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            name_part, value = line[:i], line[i + 1:]
            break
    else:
        return None
    pieces = name_part.split(";")
    params = {}
    for p in pieces[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return pieces[0].upper(), params, value


# -- VTIMEZONE ----------------------------------------------------------------

@dataclass(frozen=True)
class _TzRule:
    """One STANDARD/DAYLIGHT component: 'from `month`'s `week`-th
    `weekday` at `at_time` local, the UTC offset is `offset`'."""
    offset: timedelta
    month: int | None      # None = fixed-offset zone (no transitions)
    week: int              # 1..4, or -1 = last
    weekday: int           # 0=Mon .. 6=Sun
    at_time: time

    def transition(self, year: int) -> datetime:
        assert self.month is not None
        if self.week > 0:
            d = date(year, self.month, 1)
            d += timedelta(days=(self.weekday - d.weekday()) % 7)
            d += timedelta(weeks=self.week - 1)
        else:
            d = (date(year, self.month + 1, 1) if self.month < 12 else date(year + 1, 1, 1))
            d -= timedelta(days=1)
            d -= timedelta(days=(d.weekday() - self.weekday) % 7)
        return datetime.combine(d, self.at_time)


class FeedTimezone:
    """Offset resolver built from one VTIMEZONE block. For a zone with
    both STANDARD and DAYLIGHT rules, the offset for a naive local
    datetime is the TZOFFSETTO of the most recent transition at or before
    it (wrapping to the previous year's last transition). Sub-hour
    precision at the exact transition moment doesn't matter here --
    courses don't run at 2-3am on changeover night."""

    def __init__(self, tzid: str, rules: list[_TzRule]):
        self.tzid = tzid
        self._rules = rules

    def utc_offset(self, naive: datetime) -> timedelta:
        with_transitions = [r for r in self._rules if r.month is not None]
        if not with_transitions:
            return self._rules[0].offset if self._rules else timedelta(0)
        candidates = []
        for year in (naive.year - 1, naive.year):
            for r in with_transitions:
                candidates.append((r.transition(year), r.offset))
        candidates.sort(key=lambda c: c[0])
        offset = candidates[0][1]
        for when, off in candidates:
            if when <= naive:
                offset = off
        return offset

    def to_utc(self, naive: datetime) -> datetime:
        return (naive - self.utc_offset(naive)).replace(tzinfo=timezone.utc)


_UTC_OFFSET_RE = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?$")


def _parse_utc_offset(value: str) -> timedelta:
    m = _UTC_OFFSET_RE.match(value.strip())
    if not m:
        return timedelta(0)
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(
        hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4) or 0)
    )


def _parse_vtimezone(lines: list[str]) -> FeedTimezone | None:
    tzid = ""
    rules: list[_TzRule] = []
    comp: dict[str, str] | None = None

    def close_component():
        nonlocal comp
        if comp is None:
            return
        offset = _parse_utc_offset(comp.get("TZOFFSETTO", ""))
        month = week = weekday = None
        at_time = time(2, 0)
        dtstart = comp.get("DTSTART", "")
        if "T" in dtstart:
            hhmmss = dtstart.split("T", 1)[1]
            if len(hhmmss) >= 6 and hhmmss[:6].isdigit():
                at_time = time(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
        rrule = comp.get("RRULE", "")
        m_month = re.search(r"BYMONTH=(\d+)", rrule)
        m_byday = re.search(r"BYDAY=(-?\d+)([A-Z]{2})", rrule)
        if m_month and m_byday and m_byday.group(2) in _WEEKDAYS:
            month = int(m_month.group(1))
            week = int(m_byday.group(1))
            weekday = _WEEKDAYS[m_byday.group(2)]
        rules.append(_TzRule(
            offset=offset, month=month,
            week=week if week is not None else 1,
            weekday=weekday if weekday is not None else 0,
            at_time=at_time,
        ))
        comp = None

    for line in lines:
        prop = _split_property(line)
        if prop is None:
            continue
        name, _params, value = prop
        if name == "TZID":
            tzid = value.strip()
        elif name == "BEGIN" and value.strip().upper() in ("STANDARD", "DAYLIGHT"):
            comp = {}
        elif name == "END" and value.strip().upper() in ("STANDARD", "DAYLIGHT"):
            close_component()
        elif comp is not None:
            comp[name] = value
    return FeedTimezone(tzid, rules) if tzid else None


# -- events -------------------------------------------------------------------

@dataclass(frozen=True)
class FeedEvent:
    """One VEVENT as parsed -- possibly a recurrence MASTER (has rrule)
    or an instance OVERRIDE (has recurrence_id). `start`/`end` are naive
    local datetimes plus `tzid` (resolved at expansion time), aware UTC
    datetimes (from `...Z` forms), or plain dates for all-day events."""
    uid: str = ""
    summary: str = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    tzid: str = ""
    all_day: bool = False
    transparent: bool = False
    busy_status: str = ""            # X-MICROSOFT-CDO-BUSYSTATUS, uppercased
    cancelled: bool = False          # STATUS:CANCELLED
    rrule: str = ""
    exdates: tuple[datetime | date, ...] = ()
    recurrence_id: datetime | date | None = None


@dataclass(frozen=True)
class FeedOccurrence:
    """One concrete occurrence after expansion. Timed: aware-UTC
    start/end. All-day: `day_start`/`day_end` dates (end exclusive, per
    RFC 5545 DTEND;VALUE=DATE) with start/end as UTC midnights in the
    caller's default timezone -- comparisons for all-day events should
    use the dates, not the synthesized datetimes."""
    start: datetime
    end: datetime
    all_day: bool
    summary: str
    busy_status: str
    transparent: bool
    uid: str = ""
    day_start: date | None = None
    day_end: date | None = None


def _parse_dt_value(value: str, params: dict[str, str]) -> tuple[datetime | date | None, str, bool]:
    """-> (parsed, tzid, is_all_day)."""
    value = value.strip()
    tzid = params.get("TZID", "")
    if params.get("VALUE", "").upper() == "DATE" or ("T" not in value and len(value) == 8):
        try:
            return datetime.strptime(value, "%Y%m%d").date(), "", True
        except ValueError:
            return None, "", True
    try:
        if value.endswith("Z"):
            return (
                datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
                "", False,
            )
        return datetime.strptime(value, "%Y%m%dT%H%M%S"), tzid, False
    except ValueError:
        return None, "", False


_DURATION_RE = re.compile(
    r"^(?P<sign>-)?P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _parse_duration(value: str) -> timedelta | None:
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    td = timedelta(
        days=int(m.group("days") or 0), hours=int(m.group("hours") or 0),
        minutes=int(m.group("minutes") or 0), seconds=int(m.group("seconds") or 0),
    )
    return -td if m.group("sign") else td


@dataclass
class ParsedFeed:
    events: list[FeedEvent] = field(default_factory=list)
    timezones: dict[str, FeedTimezone] = field(default_factory=dict)
    # TZIDs referenced by events but never declared as a VTIMEZONE --
    # Outlook does this routinely (the real feed references "Central
    # Europe Standard Time"/"Romance Standard Time" without shipping
    # either; both are CET/CEST, so the site-timezone fallback is even
    # correct there). Warned ONCE per feed per name, not once per
    # occurrence -- expansion visits thousands of instances.
    _warned_tzids: set[str] = field(default_factory=set)


def parse_feed(text: str) -> ParsedFeed:
    """Parse a whole ICS document. Malformed events are skipped (with a
    debug log), never fatal -- one broken entry in a 1500-event feed must
    not take the conflict check down with it."""
    feed = ParsedFeed()
    lines = _unfold(text).replace("\r\n", "\n").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("BEGIN:VTIMEZONE"):
            block = []
            i += 1
            while i < n and not lines[i].startswith("END:VTIMEZONE"):
                block.append(lines[i])
                i += 1
            tz = _parse_vtimezone(block)
            if tz is not None:
                feed.timezones[tz.tzid] = tz
        elif line.startswith("BEGIN:VEVENT"):
            block = []
            i += 1
            while i < n and not lines[i].startswith("END:VEVENT"):
                block.append(lines[i])
                i += 1
            ev = _parse_vevent(block)
            if ev is not None:
                feed.events.append(ev)
        i += 1
    return feed


def _parse_vevent(lines: list[str]) -> FeedEvent | None:
    uid = summary = busy = rrule = tzid = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    duration: timedelta | None = None
    all_day = transparent = cancelled = False
    exdates: list[datetime | date] = []
    recurrence_id: datetime | date | None = None

    for line in lines:
        prop = _split_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "UID":
            uid = value.strip()
        elif name == "SUMMARY":
            summary = _unescape(value).strip()
        elif name == "DTSTART":
            start, tzid, all_day = _parse_dt_value(value, params)
        elif name == "DTEND":
            end, _tz, _ad = _parse_dt_value(value, params)
        elif name == "DURATION":
            duration = _parse_duration(value)
        elif name == "TRANSP":
            transparent = value.strip().upper() == "TRANSPARENT"
        elif name == "X-MICROSOFT-CDO-BUSYSTATUS":
            busy = value.strip().upper()
        elif name == "STATUS":
            cancelled = value.strip().upper() == "CANCELLED"
        elif name == "RRULE":
            rrule = value.strip()
        elif name == "EXDATE":
            for piece in value.split(","):
                parsed, _tz, _ad = _parse_dt_value(piece, params)
                if parsed is not None:
                    exdates.append(parsed)
        elif name == "RECURRENCE-ID":
            recurrence_id, _tz, _ad = _parse_dt_value(value, params)

    if start is None:
        return None
    if end is None:
        if duration is not None and isinstance(start, datetime):
            end = start + duration
        elif all_day and isinstance(start, date):
            end = start + timedelta(days=1)
        else:
            end = start
    return FeedEvent(
        uid=uid, summary=summary, start=start, end=end, tzid=tzid,
        all_day=all_day, transparent=transparent, busy_status=busy,
        cancelled=cancelled, rrule=rrule, exdates=tuple(exdates),
        recurrence_id=recurrence_id,
    )


# -- RRULE expansion ----------------------------------------------------------

_SUPPORTED_RRULE_PARTS = {
    "FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST",
}
_ORDINAL_BYDAY_RE = re.compile(r"^(-?\d+)?([A-Z]{2})$")


def _parse_rrule(value: str) -> dict | None:
    """-> parts dict, or None when the rule uses something outside the
    supported subset (caller logs and falls back to single-occurrence)."""
    parts: dict = {}
    for piece in value.split(";"):
        if not piece or "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        k = k.upper()
        if k not in _SUPPORTED_RRULE_PARTS:
            return None
        parts[k] = v
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    byday = []
    for tok in parts.get("BYDAY", "").split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        m = _ORDINAL_BYDAY_RE.match(tok)
        if not m or m.group(2) not in _WEEKDAYS:
            return None
        byday.append((int(m.group(1)) if m.group(1) else None, _WEEKDAYS[m.group(2)]))
    return {
        "freq": freq,
        "interval": max(1, int(parts.get("INTERVAL", 1))),
        "count": int(parts["COUNT"]) if "COUNT" in parts else None,
        "until": parts.get("UNTIL", ""),
        "byday": byday,
        "bymonthday": int(parts["BYMONTHDAY"]) if "BYMONTHDAY" in parts else None,
        "bymonth": int(parts["BYMONTH"]) if "BYMONTH" in parts else None,
    }


def _nth_weekday(year: int, month: int, week: int, weekday: int) -> date | None:
    try:
        if week > 0:
            d = date(year, month, 1)
            d += timedelta(days=(weekday - d.weekday()) % 7)
            d += timedelta(weeks=week - 1)
        else:
            d = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            d -= timedelta(days=1)
            d -= timedelta(days=(d.weekday() - weekday) % 7)
            d += timedelta(weeks=week + 1)
        return d if d.month == month else None
    except ValueError:
        return None


def _rule_dates(first: date, rule: dict, horizon: date):
    """Yield the LOCAL dates of the recurrence set, in order, starting at
    the DTSTART date, stopping past `horizon` (COUNT/UNTIL are applied by
    the caller, which also needs the pre-EXDATE instance stream)."""
    freq, interval = rule["freq"], rule["interval"]
    if freq == "DAILY":
        d = first
        while d <= horizon:
            yield d
            d += timedelta(days=interval)
    elif freq == "WEEKLY":
        weekdays = sorted(wd for _n, wd in rule["byday"]) or [first.weekday()]
        week_anchor = first - timedelta(days=first.weekday())  # WKST=MO
        while week_anchor <= horizon:
            for wd in weekdays:
                d = week_anchor + timedelta(days=wd)
                if d >= first:
                    yield d
            week_anchor += timedelta(weeks=interval)
    elif freq == "MONTHLY":
        year, month = first.year, first.month
        ordinal = next(((n, wd) for n, wd in rule["byday"] if n is not None), None)
        while date(year, month, 1) <= horizon:
            if ordinal is not None:
                d = _nth_weekday(year, month, ordinal[0], ordinal[1])
            else:
                day = rule["bymonthday"] or first.day
                try:
                    d = date(year, month, day)
                except ValueError:
                    d = None
            if d is not None and d >= first:
                yield d
            month += interval
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    elif freq == "YEARLY":
        year = first.year
        month = rule["bymonth"] or first.month
        ordinal = next(((n, wd) for n, wd in rule["byday"] if n is not None), None)
        while date(year, 1, 1) <= horizon:
            if ordinal is not None:
                d = _nth_weekday(year, month, ordinal[0], ordinal[1])
            else:
                try:
                    d = date(year, month, first.day)
                except ValueError:
                    d = None
            if d is not None and d >= first:
                yield d
            year += interval


def _until_passed(rule_until: str, occ_start_utc: datetime, occ_date: date) -> bool:
    if not rule_until:
        return False
    value = rule_until.strip()
    try:
        if value.endswith("Z"):
            return occ_start_utc > datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if "T" in value:
            return occ_start_utc > datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return occ_date > datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return False


def expand(
    feed: ParsedFeed,
    window_start: datetime,
    window_end: datetime,
    default_tz: tzinfo,
) -> list[FeedOccurrence]:
    """All concrete occurrences overlapping [window_start, window_end)
    (aware UTC), with RECURRENCE-ID overrides applied and EXDATEs
    removed. `default_tz` anchors floating local times and all-day
    midnights (pass the [site].timezone zoneinfo)."""
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)

    overrides: dict[tuple[str, str], FeedEvent] = {}
    master_uids = {ev.uid for ev in feed.events if ev.uid and ev.rrule}
    for ev in feed.events:
        if ev.uid and ev.recurrence_id is not None:
            overrides[(ev.uid, _instance_key(ev.recurrence_id))] = ev

    out: list[FeedOccurrence] = []
    for ev in feed.events:
        if ev.recurrence_id is not None:
            # Overrides are emitted while walking their master below; an
            # override whose master isn't in the feed at all is emitted
            # standalone so it isn't lost.
            if ev.uid not in master_uids:
                _emit(out, ev, ev, feed, default_tz, window_start, window_end)
            continue
        if not ev.rrule:
            _emit(out, ev, ev, feed, default_tz, window_start, window_end)
            continue

        rule = _parse_rrule(ev.rrule)
        if rule is None:
            log.warning(
                "unsupported RRULE %r (uid %s) -- treating as single occurrence",
                ev.rrule, ev.uid or "?",
            )
            _emit(out, ev, ev, feed, default_tz, window_start, window_end)
            continue

        first_date = ev.start if isinstance(ev.start, date) and not isinstance(ev.start, datetime) else ev.start.date()
        horizon = (window_end + timedelta(days=1)).date()
        emitted = 0
        for occ_date in _rule_dates(first_date, rule, horizon):
            emitted += 1
            if emitted > _MAX_INSTANCES:
                log.warning("RRULE expansion cap hit (uid %s) -- stopping", ev.uid or "?")
                break
            if rule["count"] is not None and emitted > rule["count"]:
                break
            instance = _materialize(ev, occ_date, feed, default_tz)
            if instance is None:
                continue
            occ_start_utc, _occ_end, key = instance
            if _until_passed(rule["until"], occ_start_utc, occ_date):
                break
            if any(_instance_key(x) == key for x in ev.exdates):
                continue
            override = overrides.get((ev.uid, key)) if ev.uid else None
            source = override if override is not None else ev
            _emit(out, source, ev, feed, default_tz, window_start, window_end,
                  master_occurrence_date=occ_date)
    return out


def _instance_key(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


def _to_utc(value: datetime, tzid: str, feed: ParsedFeed, default_tz: tzinfo) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    tz = feed.timezones.get(tzid)
    if tz is not None:
        return tz.to_utc(value)
    if tzid and tzid not in feed._warned_tzids:
        feed._warned_tzids.add(tzid)
        log.warning("undeclared TZID %r in feed -- falling back to the site timezone", tzid)
    return value.replace(tzinfo=default_tz).astimezone(timezone.utc)


def _materialize(
    ev: FeedEvent, occ_date: date, feed: ParsedFeed, default_tz: tzinfo,
) -> tuple[datetime, datetime, str] | None:
    """The (start_utc, end_utc, instance_key) a master event has on one
    recurrence-set date. Timed masters keep DTSTART's wall-clock time and
    wall-clock duration on every instance."""
    if ev.all_day:
        length = (ev.end - ev.start).days if isinstance(ev.end, date) else 1
        start_local = datetime.combine(occ_date, time(0, 0))
        end_local = start_local + timedelta(days=max(1, length))
        return (
            start_local.replace(tzinfo=default_tz).astimezone(timezone.utc),
            end_local.replace(tzinfo=default_tz).astimezone(timezone.utc),
            occ_date.strftime("%Y%m%d"),
        )
    if not isinstance(ev.start, datetime) or not isinstance(ev.end, datetime):
        return None
    duration = ev.end - ev.start
    if ev.start.tzinfo is not None:
        # A `...Z` master keeps its UTC wall-clock time on every instance.
        utc_naive = ev.start.astimezone(timezone.utc).replace(tzinfo=None)
        start_utc = datetime.combine(occ_date, utc_naive.time()).replace(tzinfo=timezone.utc)
        return start_utc, start_utc + duration, occ_date.strftime("%Y%m%d") + "T" + utc_naive.strftime("%H%M%S")
    local_start = datetime.combine(occ_date, ev.start.time())
    key = local_start.strftime("%Y%m%dT%H%M%S")
    start_utc = _to_utc(local_start, ev.tzid, feed, default_tz)
    return start_utc, start_utc + duration, key


def _emit(
    out: list[FeedOccurrence],
    source: FeedEvent,
    master: FeedEvent,
    feed: ParsedFeed,
    default_tz: tzinfo,
    window_start: datetime,
    window_end: datetime,
    master_occurrence_date: date | None = None,
) -> None:
    """Append `source`'s occurrence if it overlaps the window. `source`
    is either the master itself (plain events; non-overridden instances,
    where `master_occurrence_date` names the instance) or a
    RECURRENCE-ID override event (its own DTSTART/DTEND/fields win)."""
    if source.cancelled:
        return
    if source.all_day:
        if not isinstance(source.start, date):
            return
        day_start = (
            master_occurrence_date
            if source is master and master_occurrence_date is not None
            else source.start
        )
        length = (source.end - source.start).days if isinstance(source.end, date) else 1
        day_end = day_start + timedelta(days=max(1, length))
        start_utc = datetime.combine(day_start, time(0, 0)).replace(tzinfo=default_tz).astimezone(timezone.utc)
        end_utc = datetime.combine(day_end, time(0, 0)).replace(tzinfo=default_tz).astimezone(timezone.utc)
    else:
        if not isinstance(source.start, datetime) or not isinstance(source.end, datetime):
            return
        if source is master and master_occurrence_date is not None:
            materialized = _materialize(master, master_occurrence_date, feed, default_tz)
            if materialized is None:
                return
            start_utc, end_utc, _key = materialized
        else:
            start_utc = _to_utc(source.start, source.tzid, feed, default_tz)
            end_utc = _to_utc(source.end, source.tzid, feed, default_tz)
        day_start = day_end = None
    if start_utc >= window_end or end_utc <= window_start:
        return
    out.append(FeedOccurrence(
        start=start_utc, end=end_utc, all_day=source.all_day,
        summary=source.summary, busy_status=source.busy_status,
        transparent=source.transparent, uid=source.uid or master.uid,
        day_start=day_start if source.all_day else None,
        day_end=day_end if source.all_day else None,
    ))
