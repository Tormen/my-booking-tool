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
    # organizer/attendees: added 2026-07-14 for
    # app.config.Course.host_calendar_entry_cc_list (the operator: "list of email
    # addresses that if set on a course in settings.toml will also be
    # invited as optional (cc) so that they receive the same invite as
    # well"). Both default to "off" (None / empty tuple), same
    # byte-identical-unless-opted-in convention as alarms_minutes_before
    # above -- a course with no cc list configured renders exactly as
    # before these fields existed. organizer is a plain email address (no
    # "mailto:" prefix -- added by to_ics()); attendees are ROLE=
    # OPT-PARTICIPANT (never REQ-PARTICIPANT: this is a courtesy copy, not
    # a scheduling request the recipient is expected to RSVP to) and
    # RSVP=FALSE (no reply expected). Whether an ATTENDEE actually
    # triggers an invite EMAIL from the CalDAV server (vs. just appearing
    # silently in the event's own attendee list) depends entirely on that
    # server's own iTIP/scheduling support -- this class only controls
    # what goes in the .ics itself.
    organizer: str | None = None
    attendees: tuple[str, ...] = ()

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
        if self.organizer:
            lines.append(f"ORGANIZER:mailto:{self.organizer}")
        for email in self.attendees:
            lines.append(f"ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:{email}")
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
_SEQUENCE_RE = re.compile(r"^SEQUENCE:(-?\d+)$", re.MULTILINE)


def parse_uid(ics_text: str) -> str | None:
    m = _UID_RE.search(ics_text)
    return m.group(1).strip() if m else None


def parse_sequence(ics_text: str) -> int:
    """RFC 5545 SEQUENCE of the given VEVENT, or 0 if absent (the spec's
    own default). 2026-07-16, the operator, root-causing a persistent-conflict
    incident down to the actual DEBUG output he collected (not just more
    retries): every single UPDATE to an already-existing operator
    calendar event was failing with HTTP 412 -- not intermittently, EVERY
    time -- while the one occurrence with no prior event (a brand-new
    create) succeeded. The CalDAV server's (Open-Xchange) own error body
    said why: "Concurrent modification [id 1081, client sequence 0,
    actual sequence 1]" -- calendar_sync.sync_occurrence() always builds
    its VEvent with the default sequence=0 (see VEvent's own docstring),
    on every single PUT, forever, never incrementing it. The FIRST PUT
    for a given occurrence (sequence 0, matches the server's own initial
    state) succeeds; every PUT after that still sends sequence 0 while
    the server's own tracked sequence has since advanced past it, and
    Open-Xchange enforces that as a real (and, absent this fix,
    PERMANENT -- no amount of retrying a wrong value ever becomes right)
    conflict, independent of whether the ETag/If-Match itself matched.
    See sync_occurrence()'s own use of this, right below."""
    m = _SEQUENCE_RE.search(ics_text)
    return int(m.group(1)) if m else 0


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
