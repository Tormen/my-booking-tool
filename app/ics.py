"""Minimal iCalendar (RFC 5545) VEVENT build/parse -- just enough for our
one-event-per-course-occurrence model. No external 'icalendar' package
needed; the subset we use is small and well-defined."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


def _fold(line: str) -> str:
    """RFC 5545 75-octet line folding."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fmt_dt(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class VEvent:
    uid: str
    summary: str
    description: str
    location: str
    start: datetime
    end: datetime
    # 2026-07-07, the operator: "make the reminders (list) a setting. But default
    # to NO reminders" -- was (24*60, 60) (1 day + 1h before) for every
    # caller by default; every real call site now passes its own explicit
    # value from Settings (see app/config.py's
    # trainer_calendar_reminder_minutes/guest_calendar_reminder_minutes),
    # so this bare-class default only matters for a VEvent built without
    # going through Settings at all (e.g. ad-hoc/test use) -- "no reminder"
    # is the safer thing to default to in that case too.
    alarms_minutes_before: tuple[int, ...] = ()
    # Added 2026-07-09 for the emailed guest invite/cancel attachments (see
    # app/calendar_sync.py::guest_invite_ics/guest_cancel_ics) -- all three
    # default to "off"/0/None so the CalDAV-stored event this class was
    # originally built for (app/calendar_sync.py::sync_occurrence, a plain
    # PUT with no METHOD/STATUS semantics) renders byte-identical to before
    # these fields existed.
    #
    # method: VCALENDAR-level METHOD, e.g. "PUBLISH" (a plain "add this to
    # your calendar" notice) or "CANCEL" (paired with status="CANCELLED"
    # below) -- see guest_invite_ics()'s own docstring for why PUBLISH,
    # not REQUEST/RSVP, is the right one for a booking confirmation.
    method: str | None = None
    # sequence: RFC 5545 SEQUENCE -- iTIP's way of saying "this replaces
    # whatever you previously received for this UID". The initial PUBLISH
    # invite is sequence 0 (the default); a later CANCEL for the same
    # booking uses 1, signaling calendar apps that support it to treat it
    # as an update/cancellation of that same event rather than a new one.
    sequence: int = 0
    # status: RFC 5545 STATUS, e.g. "CANCELLED" on a CANCEL event. None
    # (the default) omits the property entirely, same as before this
    # field existed.
    status: str | None = None

    def to_ics(self) -> str:
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
        if self.method:
            lines.append(f"METHOD:{self.method}")
        lines += [
            "PRODID:-//my-booking-tool//booking//EN",
            "BEGIN:VEVENT",
            f"UID:{self.uid}",
            f"DTSTAMP:{_fmt_dt(datetime.now(timezone.utc))}",
            f"DTSTART:{_fmt_dt(self.start)}",
            f"DTEND:{_fmt_dt(self.end)}",
            f"SEQUENCE:{self.sequence}",
            f"SUMMARY:{_escape_text(self.summary)}",
            f"DESCRIPTION:{_escape_text(self.description)}",
            f"LOCATION:{_escape_text(self.location)}",
        ]
        if self.status:
            lines.append(f"STATUS:{self.status}")
        for minutes in self.alarms_minutes_before:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape_text(self.summary)}",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ]
        lines += ["END:VEVENT", "END:VCALENDAR"]
        return "\r\n".join(_fold(l) for l in lines) + "\r\n"


_UID_RE = re.compile(r"^UID:(.+)$", re.MULTILINE)
_DTSTART_RE = re.compile(r"^DTSTART(?:;[^:]*)?:(.+)$", re.MULTILINE)
_DTEND_RE = re.compile(r"^DTEND(?:;[^:]*)?:(.+)$", re.MULTILINE)


def parse_uid(ics_text: str) -> str | None:
    m = _UID_RE.search(ics_text)
    return m.group(1).strip() if m else None


def _parse_dt(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if "T" in value:
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    return datetime.strptime(value, "%Y%m%d")


def parse_window(ics_text: str) -> tuple[datetime, datetime] | None:
    s, e = _DTSTART_RE.search(ics_text), _DTEND_RE.search(ics_text)
    if not s or not e:
        return None
    return _parse_dt(s.group(1)), _parse_dt(e.group(1))
