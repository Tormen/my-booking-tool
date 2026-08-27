"""Keeps the single course-occurrence VEVENT in sync with registrations.csv.

UID pattern `<slug>-<shortname>-<date>@<domain>` (slug/domain derived from
this deployment's own `settings.base_url`, see `_uid_parts` below) lets the
conflict checker (slots.py, via `is_own_event`) recognize and skip our own
generated events -- otherwise a course would always "conflict with
itself". Deriving this from `base_url` (rather than a hardcoded domain)
matters once more than one deployment of this tool exists: two installs
sharing a calendar (or one install's test/staging calendar reusing a
production one) must never recognize each other's events as "our own".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .atomic_io import atomic_write_text, fsync_dir
from .caldav_client import CalDAVClient, CalDAVConflictError, CalDAVError
from .cancellation import host_cancel_occurrence_url, host_cancel_url, html_to_text
from .config import Course, Settings
from .ics import VEvent, parse_sequence
from .storage import (
    STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST, STATUS_CONFIRMED, STATUS_WAITLISTED,
    Registration, Store, User, format_display_timestamp,
)

log = logging.getLogger("my_booking.calendar_sync")

# 2026-07-07: a real production 500 on /my/confirm, root-caused via
# journalctl to "PUT ... -> HTTP 412 ... a newer version of the appointment
# already exists". Two near-simultaneous requests touching the SAME
# occurrence (e.g. two guests booking/confirming for the same course+date
# within a few seconds of each other) can each read this event's current
# ETag, then race to PUT/DELETE it -- whichever loses gets a 412. A plain
# retry a few seconds later succeeded on its own in that incident, i.e.
# genuinely transient, so this many attempts (re-reading a fresh ETag each
# time) is cheap insurance against exactly that, not a sign of a deeper
# problem if it occasionally takes 2. Zero delay between attempts here --
# this is the path a live guest booking/cancellation request takes, so it
# should fail fast rather than make a browser hang for several extra
# seconds. Used for EVERY caller, including the bulk resync below (see
# the 2026-07-16 note just below on why that no longer gets a separate,
# more-patient constant of its own).
#
# Diagnostic note, resolved 2026-07-16: sync_occurrence()'s conflict
# handling logs, at DEBUG level, whether the re-read ETag actually
# CHANGED between attempts -- added when it was still an open question
# whether this was a genuinely concurrent writer (ETag should differ,
# often differently again, each retry) or "something else". DEBUG
# output collected from a real run confirmed it WAS
# something else: the ETag was identical on every retry, and the
# server's own error named the real cause -- see parse_sequence()'s
# docstring in app/ics.py and this function's own 2026-07-16 update
# just below for the actual bug (a permanently-stale SEQUENCE, not a
# race) and its fix. The diagnostics stay in place regardless, in case
# a genuinely different incident turns up here in the future.
_SYNC_CONFLICT_MAX_ATTEMPTS = 3

# 2026-07-16: a previous
# version of this comment introduced _BULK_RESYNC_MAX_ATTEMPTS=6 with
# increasing backoff, reasoning that 3 DIFFERENT occurrences hitting a
# persistent conflict in the same run was probably just an active
# concurrent writer that needed more time. That reasoning didn't hold up:
# simply retrying longer isn't the right fix for something that hit 3 unrelated occurrences at
# once; that pattern is just as consistent with a real, structural bug
# (e.g. a stale-etag comparison bug of ours, or the server naming
# resources differently than we assume -- see query_events()'s own
# note on this) as with "still-mid-flight concurrent writer", and
# retrying harder does nothing to tell those apart, it just delays
# finding out. So: reverted back to the SAME 3-attempts/zero-delay
# behavior as a live request for the bulk resync path too (no special
# case) -- see _SYNC_CONFLICT_MAX_ATTEMPTS above, now used everywhere.
# In its place, sync_occurrence()'s conflict handling below now emits
# much richer DEBUG-level diagnostics (full request/response detail,
# etag-before-vs-after) so that the NEXT time this happens, enabling
# `my-bt -D` / `MY_BOOKING_DEBUG=1` actually gives enough to root-cause
# it, instead of trying to paper over it with more patience.

# 2026-07-09, the standing rule (see SOLUTION-DESIGN.md section 24):
# any change to the CALENDAR INVITE(s) (host and/or attendee) must
# ensure existing (future) calendar invites are updated too, either on
# install or the next time this calendar invite is touched. The "next moment you touch it again" half
# is already free (sync_occurrence() always recomputes from scratch). This
# constant plus resync_if_format_changed() below cover the "on install"
# half: bump this integer by 1 in the SAME commit as any change to what
# sync_occurrence() puts in the HOST event's description (new line,
# reworded line, removed line -- anything visible on the operator's own
# calendar entry). resync_if_format_changed() compares this against a
# marker file under the data dir and re-syncs every future occurrence
# automatically, once, the next time `my-bt setup -i` runs -- so an
# operator upgrading the package and re-running setup gets existing
# invites caught up without having to separately remember to run
# `my-bt admin resync-calendar` by hand every time.
CALENDAR_INVITE_FORMAT_VERSION = 1

# Public (no leading underscore) -- app.cli_checks.check_calendar_invite_
# format() also needs to read this same marker file (a cheap, local,
# no-network re-check of whether the last resync actually finished, shown
# in `my-bt admin setup`/`admin health`/`setup -i`'s own reports so a
# resync that silently failed -- see resync_if_format_changed()'s own
# docstring on the 2026-07-15 incident -- doesn't just vanish, unreported,
# into a raw print_fn() line that nothing else re-checks).
CALENDAR_INVITE_FORMAT_VERSION_MARKER_NAME = ".calendar_invite_format_version"

# 2026-07-15/16: a real production `setup -i` run showed 3 occurrences
# hitting persistent CalDAV conflicts (stale ETag, still conflicting after
# every retry) during a resync, got skipped (see resync_all_future_
# calendar_events()'s own 2026-07-15 docstring update), and the run still
# printed "[ok] calendar invite format changed -- resynced 6 upcoming
# occurrence(s)" and finished with "Done -- all checks pass now" --
# because that message only ever reported the SUCCESS count, never
# whether anything was skipped. This marker
# records exactly which occurrences (if any) were skipped on the LAST
# resync attempt, so app.cli_checks.check_calendar_invite_resync_skips()
# can keep flagging it in every later `admin health`/`admin setup` run
# too -- not just the one run where it happened, which is easy to miss
# in a long interactive walkthrough's scrollback.
CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME = ".calendar_invite_resync_skipped"


@dataclass
class ResyncResult:
    """Return type for resync_all_future_calendar_events()/resync_if_
    format_changed() -- `fixed` alone (the old plain-int return value)
    silently lost exactly the information needed: whether
    EVERYTHING resynced cleanly, or some occurrences were skipped after
    a persistent CalDAV conflict. `skipped` is one human-readable line
    per skipped occurrence (course + date + the error that caused it),
    matching what's already logged via log.warning() below -- empty
    means a fully clean run."""
    fixed: int
    skipped: list[str] = field(default_factory=list)


def record_resync_skips(data_dir: str | Path, result: ResyncResult) -> None:
    """Persists `result.skipped` (if any) to CALENDAR_INVITE_RESYNC_
    SKIPPED_MARKER_NAME under `data_dir`, or removes that marker if the
    run was fully clean -- so a LATER `my-bt admin health`/`admin setup`
    (deliberately network-free, never re-runs the actual resync) can
    still see "the last resync attempt left N occurrence(s) unresolved"
    long after the run that discovered it. Called by both
    resync_if_format_changed() (the automatic path) and `my-bt admin
    resync-calendar` (the manual one) -- either is a real "attempt", so
    either should update this record."""
    marker_path = Path(data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
    if result.skipped:
        # secure=True: same shared data_dir, same root-run-my-bt exposure
        # as the format-version marker above -- see its own comment.
        atomic_write_text(marker_path, "\n".join(result.skipped) + "\n", secure=True, mode=0o640)
    elif marker_path.exists():
        marker_path.unlink()
        fsync_dir(marker_path.parent)


def _self_or_guest(r: Registration, users_by_id: dict[str, User]) -> str:
    """"self" for whoever filled out the booking form, "guest of <name>" for
    everyone they brought along -- same invited_by_user_id convention as
    admin_overview's Party column (see Registration's own docstring)."""
    if r.invited_by_user_id:
        leader = users_by_id.get(r.invited_by_user_id)
        return f"guest of {leader.name if leader else '(unknown)'}"
    return "self"


def _name_email(r: Registration, users_by_id: dict[str, User]) -> tuple[str, str]:
    user = users_by_id.get(r.user_id)
    if user is None:
        # Shouldn't normally happen (every registration_id points at a real
        # or archived user row), but a calendar invite is exactly the wrong
        # place to raise over it -- show a placeholder instead.
        return "(unknown)", "(unknown)"
    return user.name, user.email


def _uid_parts(settings: Settings) -> tuple[str, str]:
    """(slug, domain) for this deployment, from settings.base_url's
    hostname -- e.g. https://booking.example.org -> ("booking-example-org",
    "booking.example.org"). Falls back to a fixed placeholder domain if
    base_url is somehow unparseable, rather than raising, since this is
    only ever used to build/recognize a UID, not to make network calls."""
    host = urlparse(settings.base_url).hostname or "my-booking-tool.invalid"
    return host.replace(".", "-"), host


def event_uid(settings: Settings, course_shortname: str, occurrence_date: date) -> str:
    slug, domain = _uid_parts(settings)
    return f"{slug}-{course_shortname}-{occurrence_date.isoformat()}@{domain}"


def is_own_event(uid: str, settings: Settings) -> bool:
    slug, domain = _uid_parts(settings)
    return uid.startswith(f"{slug}-") and uid.endswith(f"@{domain}")


def occurrence_start_end(course: Course, occurrence_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Shared start/end datetime pair for one course occurrence -- factored
    out of sync_occurrence() (2026-07-09) so guest_invite_ics()/
    guest_cancel_ics() below build the EXACT same window the operator's own
    synced calendar event uses, rather than a second, easy-to-drift-apart
    copy of this three-line calculation.

    2026-07-16: uses Course.start_hm_for()/duration_minutes_for()
    (course.date_overrides-aware), not the plain start_hm()/
    duration_minutes -- an exceptional date's actual CalDAV event (both
    the operator's own synced entry and the guest's .ics attachment) must
    reflect the REAL, shifted time, not the course's normal weekly one,
    exactly like app/slots.py::build_occurrences already does for the
    booking page itself."""
    occ_date_str = occurrence_date.isoformat()
    h, m = course.start_hm_for(occ_date_str)
    start = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day, h, m, tzinfo=tz)
    return start, start + timedelta(minutes=course.duration_minutes_for(occ_date_str))


def sync_occurrence(
    client: CalDAVClient,
    calendar_href: str,
    store: Store,
    settings: Settings,
    course: Course,
    occurrence_date: date,
) -> None:
    """Call this after every registration/cancellation for the occurrence.

    2026-07-16: a prior version of this function accepted
    `max_attempts`/`retry_delay_seconds`/`sleep_fn` overrides so the bulk
    resync below could retry harder than a live request. Reverted --
    retrying longer wasn't actually the right fix. Every
    caller (live booking/cancellation AND the bulk resync) now uses the
    exact same 3-attempts/zero-delay behavior; see
    _SYNC_CONFLICT_MAX_ATTEMPTS's own docstring for why more patience
    isn't the fix being reached for here.

    The DEBUG output collected from a real production log
    (`MY_BOOKING_DEBUG=1`) found the actual bug. Every single UPDATE to an
    already-existing operator event was failing with HTTP 412 -- not
    intermittently, EVERY time, while a brand-new create succeeded fine
    -- and the ETag reported as "current" after re-reading was IDENTICAL
    to the one just rejected, every retry. Open-Xchange's own error body
    said why: "Concurrent modification [id 1081, client sequence 0,
    actual sequence 1]" -- this function always built its VEvent with
    the default `sequence=0` (see VEvent's own docstring) and never
    incremented it, so every PUT after the very first one for a given
    occurrence sent a permanently-stale SEQUENCE that could never
    satisfy the server, no matter how many times or how patiently it was
    retried. Fixed: read the CURRENT event's own SEQUENCE (from the same
    query_events() call already used for the ETag) and PUT with
    current+1 -- re-read fresh on every retry attempt too, in case the
    server's own tracked sequence moved again in between. See
    app.ics.parse_sequence()'s own docstring for the full incident.

    The conflict-handling loops below still log richer DEBUG-level
    diagnostics on top of this fix (etag before/after re-read, full
    server response) -- see each `except CalDAVConflictError` block --
    since a real, if different, future incident could still turn up
    there.

    The invite lists THREE groups: active (confirmed), waiting
    (waitlisted), and canceled (STATUS_CANCELED_BY_GUEST or
    STATUS_CANCELED_BY_HOST). Each line is a table row: status/position,
    Name, Email, Self/Guest (whether they booked themselves or came along
    as someone else's guest -- see _self_or_guest), the timestamp of that
    registrant's LAST action (registered_at for active/waiting, canceled_at
    + canceled_by for canceled), and a cancel link (2026-07-06: added the
    Name/Email/Self-Guest columns so the operator can see WHO is on the
    calendar without cross-referencing the CSV -- previously the invite only showed
    counts and cancel links, no identities at all).
    Canceled registrants are never dropped from the invite -- they stay
    visible, separately labeled, so the host can see who left and when
    without needing to cross-reference the CSV. Only when there are ZERO
    active (confirmed) registrants left is the event actually REMOVED from
    the calendar, regardless of how many are canceled or still waitlisted
    (see the `if not active:` branch below) -- a course with any confirmed
    spot filled always keeps its calendar entry, updated in place.
    """
    tz = ZoneInfo(settings.timezone)
    regs = store.registrations_for_occurrence(course.shortname, occurrence_date.isoformat())
    active = [r for r in regs if r.status == STATUS_CONFIRMED]
    waiting = [r for r in regs if r.status == STATUS_WAITLISTED]
    canceled = [r for r in regs if r.status in (STATUS_CANCELED_BY_GUEST, STATUS_CANCELED_BY_HOST)]
    uid = event_uid(settings, course.shortname, occurrence_date)

    def current_event_state() -> tuple[str | None, int]:
        """(Re-)reads this occurrence's own event's CURRENT (etag,
        sequence), fresh -- called once up front, and again by the retry
        loops below each time a 412 shows what we had was already stale.
        (None, 0) if the event doesn't exist yet -- a brand-new event
        correctly starts at SEQUENCE 0, RFC 5545's own default; there's
        nothing to increment past yet. See parse_sequence()'s own
        docstring for why sequence is read here at all (2026-07-16
        incident: a fixed sequence=0 forever meant every UPDATE PUT was
        permanently rejected by the server, not a transient race)."""
        for u, ics_text, etag in client.query_events(
            calendar_href,
            datetime.combine(occurrence_date, datetime.min.time(), tzinfo=tz),
            datetime.combine(occurrence_date + timedelta(days=1), datetime.min.time(), tzinfo=tz),
        ):
            if u == uid:
                return etag, parse_sequence(ics_text)
        return None, 0

    etag, _sequence = current_event_state()

    if not active:
        # No confirmed registrants -- delete the event even if there's a
        # waitlist. Deliberate: the calendar event represents "this course
        # is actually happening", not "someone is interested in it", so a
        # fully-waitlisted occurrence (0 confirmed) has no calendar entry
        # at all until/unless someone gets promoted into a confirmed spot.
        if etag is not None:
            for attempt in range(1, _SYNC_CONFLICT_MAX_ATTEMPTS + 1):
                try:
                    client.delete_event(calendar_href, uid, etag)
                    break
                except CalDAVConflictError as exc:
                    fresh_etag, _fresh_sequence = current_event_state()
                    log.debug(
                        "conflict deleting calendar event %s (attempt %d/%d): attempted "
                        "etag=%r, server's current etag now=%r%s -- %s",
                        uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS, etag, fresh_etag,
                        " [UNCHANGED -- see _SYNC_CONFLICT_MAX_ATTEMPTS's docstring, this is "
                        "NOT what a concurrent write should look like]" if fresh_etag == etag else "",
                        exc,
                    )
                    if attempt == _SYNC_CONFLICT_MAX_ATTEMPTS:
                        raise
                    log.warning(
                        "stale ETag deleting calendar event %s (attempt %d/%d) -- retrying with a "
                        "fresh one (run with `my-bt -D` / MY_BOOKING_DEBUG=1 for full diagnostics "
                        "if this keeps happening)",
                        uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS,
                    )
                    etag = fresh_etag
                    if etag is None:
                        break  # someone else's concurrent change already removed it -- nothing left to do
        return

    start, end = occurrence_start_end(course, occurrence_date, tz)

    # One lookup covering everyone on this occurrence, live or archived (an
    # erased registrant can still be a CANCELED row here) -- read_users(
    # scope="all") so a since-erased participant still resolves to their
    # (now "[erased]"/hashed) row instead of "(unknown)".
    users_by_id = {u["user_id"]: User(**u) for u in store.read_users(scope="all")}

    lines = [f"{course.title} -- {len(active)}/{course.capacity} registered"]
    if waiting:
        lines.append(f"{len(waiting)} on waitlist")
    if canceled:
        lines.append(f"{len(canceled)} canceled")
    if active or waiting:
        # 2026-07-13: the CALDAV invite needs both a cancel link per
        # participant AND a course cancel link for ALL of them -- a
        # second, ALWAYS-present link alongside every individual
        # participant's own "cancel:" line below, for the "illness/venue
        # unavailable, cancel the whole session at once" case (see
        # app.cancel_flow.cancel_occurrence and
        # app/webapp.py::host_cancel_occurrence). Host/operator-only, same
        # trust boundary as every other "cancel:" line here -- never sent
        # to a guest (see guest_invite_ics()'s own docstring: a guest's
        # personal .ics has no participant list and no cancel links at
        # all).
        lines.append(
            f"cancel entire session (all participants): "
            f"{host_cancel_occurrence_url(settings, course.shortname, occurrence_date.isoformat())}"
        )

    if active or waiting:
        lines.append("")
        lines.append("Participants:")
        lines.append("Status | Name | Email | Self/Guest | Registered | Cancel")
        for r in active:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- {r.status} | {name} | {email} | {_self_or_guest(r, users_by_id)} | "
                f"registered {format_display_timestamp(r.registered_at)} | "
                f"cancel: {host_cancel_url(settings, r.registration_id)}"
            )
        for r in waiting:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- waitlisted #{waiting.index(r) + 1} | {name} | {email} | "
                f"{_self_or_guest(r, users_by_id)} | registered {format_display_timestamp(r.registered_at)} | "
                f"cancel: {host_cancel_url(settings, r.registration_id)}"
            )
    if canceled:
        # Separate group, listed last -- kept OUT of the active/waiting
        # counts/lines above (this is display-only context on who left and
        # when, not part of "who currently holds a spot"). Shows
        # canceled_at (their last action -- NOT registered_at, which would
        # be stale/misleading here) and canceled_by ("guest" or "host") for
        # context, same vocabulary as Store.cancel()'s own parameter.
        lines.append("")
        lines.append("Canceled:")
        lines.append("Status | Name | Email | Self/Guest | Canceled | Cancel")
        for r in canceled:
            name, email = _name_email(r, users_by_id)
            lines.append(
                f"- {r.status} | {name} | {email} | {_self_or_guest(r, users_by_id)} | "
                f"canceled {format_display_timestamp(r.canceled_at)} by {r.canceled_by} | "
                f"cancel: {host_cancel_url(settings, r.registration_id)}"
            )
    summary = f"{course.title} ({len(active)}/{course.capacity}"
    summary += f"+{len(waiting)}wl)" if waiting else ")"
    event = VEvent(
        uid=uid,
        summary=summary,
        description="\n".join(lines),
        location=course.location,
        start=start,
        end=end,
        # 2026-07-16: sequence=0 for a brand-new event (etag is None --
        # matches VEvent's own default/RFC 5545's), otherwise the
        # CURRENT server-side sequence + 1 -- see parse_sequence()'s own
        # docstring for why this can't just stay the field default
        # forever. Re-set on each retry attempt below too.
        sequence=(_sequence + 1) if etag is not None else 0,
        alarms_minutes_before=settings.trainer_calendar_reminder_minutes,
        # 2026-07-14: host_calendar_entry_cc_list -- see
        # Course's own field docstring. organizer is only set when there's
        # actually a cc list to attach (an ATTENDEE with no ORGANIZER is
        # invalid iTIP); this deployment's own caldav_username is the
        # natural "who owns this calendar" identity, since that's exactly
        # whose calendar this event is being PUT onto.
        organizer=settings.caldav_username if course.host_calendar_entry_cc_list else None,
        attendees=course.host_calendar_entry_cc_list,
    )
    for attempt in range(1, _SYNC_CONFLICT_MAX_ATTEMPTS + 1):
        try:
            client.put_event(calendar_href, uid, event.to_ics(), etag=etag)
            return
        except CalDAVConflictError as exc:
            fresh_etag, fresh_sequence = current_event_state()
            log.debug(
                "conflict updating calendar event %s (attempt %d/%d): attempted "
                "etag=%r sequence=%r, server's current etag now=%r sequence now=%r%s -- %s",
                uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS, etag, event.sequence, fresh_etag, fresh_sequence,
                " [ETAG UNCHANGED -- see _SYNC_CONFLICT_MAX_ATTEMPTS's docstring, this is "
                "NOT what a concurrent write should look like]" if fresh_etag == etag else "",
                exc,
            )
            if attempt == _SYNC_CONFLICT_MAX_ATTEMPTS:
                raise
            log.warning(
                "stale ETag updating calendar event %s (attempt %d/%d) -- retrying with a fresh "
                "one (run with `my-bt -D` / MY_BOOKING_DEBUG=1 for full diagnostics if this keeps "
                "happening)",
                uid, attempt, _SYNC_CONFLICT_MAX_ATTEMPTS,
            )
            etag = fresh_etag
            # 2026-07-16: re-derive sequence too, not just etag -- a
            # retry that keeps resending the SAME (now stale) sequence
            # is exactly the bug this fix exists to prevent (see
            # parse_sequence()'s own docstring on the real incident).
            event.sequence = (fresh_sequence + 1) if fresh_etag is not None else 0


def resync_after_course_rename(
    client: CalDAVClient,
    calendar_href: str,
    store: Store,
    settings: Settings,
    old_shortname: str,
    new_shortname: str,
    today: date | None = None,
) -> int:
    """After a course's shortname changes (see Store.rename_course_
    shortname for the registrations.csv side of that -- run this AFTER
    that, once settings.toml already has `new_shortname`), every future
    occurrence that already had a live calendar event is still sitting
    on the calendar under the OLD event_uid: the shortname is baked
    directly into the uid (see event_uid's own docstring), and nothing
    will ever again compute that old uid to find and remove it, since
    sync_occurrence() always derives uid from the course's CURRENT
    shortname. Left alone, the NEXT booking/cancellation on that
    occurrence would silently create a second, fresh event under the new
    uid while the stale one keeps sitting there forever, unrecognized and
    never cleaned up -- a real duplicate hit in production,
    2026-07-08, when a course was renamed (lux-wed-mindfulness ->
    lux-wed-mind), which prompted this migration command for existing data.

    For each occurrence_date (today or later) that has at least one
    CONFIRMED registration under `new_shortname` (i.e. would currently
    have a live calendar entry, per sync_occurrence's own "0 confirmed --
    no calendar entry at all" rule): looks up the OLD uid's event in that
    day's window and deletes it if found, then calls sync_occurrence()
    to (re)create it cleanly under the new uid in one step. A day with
    nothing confirmed is skipped entirely -- it never had a calendar
    event to begin with.

    Returns how many occurrences were touched this way."""
    today = today or date.today()
    course = settings.course(new_shortname)
    if course is None:
        raise ValueError(
            f"no course with shortname {new_shortname!r} in settings.toml -- "
            "update settings.toml's [[course]] shortname BEFORE running this"
        )

    tz = ZoneInfo(settings.timezone)
    today_iso = today.isoformat()
    rows = store.read_registrations(scope="live")
    dates = sorted({
        r["occurrence_date"] for r in rows
        if r["course_shortname"] == new_shortname
        and r["status"] == STATUS_CONFIRMED
        and r["occurrence_date"] >= today_iso
    })

    fixed = 0
    for d_iso in dates:
        occ_date = date.fromisoformat(d_iso)
        old_uid = event_uid(settings, old_shortname, occ_date)
        for uid, _ics, etag in client.query_events(
            calendar_href,
            datetime.combine(occ_date, datetime.min.time(), tzinfo=tz),
            datetime.combine(occ_date + timedelta(days=1), datetime.min.time(), tzinfo=tz),
        ):
            if uid == old_uid:
                client.delete_event(calendar_href, uid, etag)
                break
        sync_occurrence(client, calendar_href, store, settings, course, occ_date)
        fixed += 1
    return fixed


def resync_all_future_calendar_events(
    client: CalDAVClient,
    calendar_href: str,
    store: Store,
    settings: Settings,
    today: date | None = None,
) -> ResyncResult:
    """Re-syncs every course's future occurrence that currently has a live
    calendar entry (>=1 CONFIRMED registration -- see sync_occurrence's own
    "0 confirmed = no event at all" rule), recomputing each one's
    description from the CURRENT registration data, exactly as if a fresh
    booking/cancellation had just happened on it.

    2026-07-09, after noticing a real occurrence's calendar event
    was still missing the "cancel entire session" line added on 2026-07-13
    (2026-07-11's invite showing only per-participant cancel
    links): any change to the CALENDAR INVITE(s) (host
    and/or attendee) must ensure existing (future) calendar
    invites are updated too, either on install or the next
    time this calendar invite is touched. An already-EMAILED guest .ics
    can't be edited after the fact, so there's nothing to do for invites
    already emailed -- only for future invites, which is already the
    case. Scope confirmed: this is about the
    operator's own live CalDAV event only.

    An occurrence that gets ANY new booking/cancellation before it starts
    already picks up whatever the CURRENT description format is for
    free -- sync_occurrence() always recomputes the FULL description from
    scratch, never a diff against what was there before. This command
    exists for the occurrences that DON'T get touched again before they
    happen (fully booked, nobody cancels) -- without it, those would keep
    showing whatever format was current the last time someone
    booked/canceled on them, indefinitely. Run this once, by hand, any
    time `sync_occurrence()`'s own description format changes -- see
    SOLUTION-DESIGN.md's own standing note on this, and `my-bt admin
    resync-calendar`.

    Same "confirmed registrations >= today" occurrence-discovery logic as
    resync_after_course_rename() above, just across every configured
    course rather than one -- see that function's own docstring for why
    ONLY confirmed (never waitlisted-only) occurrences are considered:
    those are the only ones sync_occurrence() would currently keep a
    calendar entry for at all.

    2026-07-15, real production failure (VPS, `my-bt admin setup -i`'s
    new step 13 -- see resync_if_format_changed()): one single occurrence
    hit a PERSISTENT CalDAVConflictError (stale ETag, still conflicting
    after all _SYNC_CONFLICT_MAX_ATTEMPTS retries -- something else kept
    touching that exact event, e.g. a real booking/cancellation landing
    on it concurrently). sync_occurrence() re-raises on its own final
    attempt; this loop used to let that abort the ENTIRE batch, silently
    dropping every occurrence not yet reached (and, worse, meaning
    resync_if_format_changed() never got to write its marker, so the next
    `setup -i` run would hit the exact same occurrence and fail exactly
    the same way -- forever, since nothing here ever un-sticks a
    genuinely persistent conflict). One occurrence's failure now only
    skips THAT occurrence (logged as a warning) -- every other occurrence
    still gets its fresh resync.

    2026-07-15/16: a real production run showed exactly this
    happening to 3 occurrences: the plain-int return value this used to
    have made the skip itself invisible to every caller -- `setup -i`
    printed "[ok] ... resynced 6 upcoming occurrence(s)" with no hint 3
    others were skipped, and "Done -- all checks pass now" right below
    it, even though the underlying result was NOT actually fully clean.
    Returns a ResyncResult now instead:
    `fixed` is still the success count, but `skipped` (one line per
    skipped occurrence) is what actually gets checked/reported from here
    on -- see record_resync_skips() and app.cli_checks.
    check_calendar_invite_resync_skips().

    2026-07-16: a prior
    version of this gave each occurrence extra attempts with backoff here
    (a background job can afford to wait out a concurrent writer). That
    was reverted -- retrying longer wasn't actually the right fix -- so this now uses the exact same
    sync_occurrence() behavior (3 attempts, zero delay) as a live
    request, no special-casing. See _SYNC_CONFLICT_MAX_ATTEMPTS's own
    docstring for the richer DEBUG-level diagnostics added in its place."""
    today = today or date.today()
    today_iso = today.isoformat()
    rows = store.read_registrations(scope="live")
    fixed = 0
    skipped: list[str] = []
    for course in settings.courses:
        dates = sorted({
            r["occurrence_date"] for r in rows
            if r["course_shortname"] == course.shortname
            and r["status"] == STATUS_CONFIRMED
            and r["occurrence_date"] >= today_iso
        })
        for d_iso in dates:
            try:
                sync_occurrence(client, calendar_href, store, settings, course, date.fromisoformat(d_iso))
            except CalDAVError as exc:
                log.warning(
                    "resync: couldn't re-sync %s on %s -- skipping it, not the rest of the batch: %s "
                    "(re-run with `my-bt -D` / MY_BOOKING_DEBUG=1 for full request/response "
                    "diagnostics if this keeps happening)",
                    course.shortname, d_iso, exc,
                )
                skipped.append(f"{course.shortname} on {d_iso}: {exc}")
                continue
            fixed += 1
    return ResyncResult(fixed=fixed, skipped=skipped)


def resync_if_format_changed(
    client: CalDAVClient,
    calendar_href: str,
    store: Store,
    settings: Settings,
    data_dir: str | Path,
    *,
    today: date | None = None,
    format_version: int = CALENDAR_INVITE_FORMAT_VERSION,
) -> ResyncResult | None:
    """Runs resync_all_future_calendar_events() automatically if EITHER of
    two things is true: `format_version` (CALENDAR_INVITE_FORMAT_VERSION
    by default) doesn't match what's recorded in a small marker file
    under `data_dir` -- see that constant's own docstring for the full
    "on install" story -- OR a previous attempt left unresolved skips
    recorded (CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME, see
    record_resync_skips()). Returns the ResyncResult (same as
    resync_all_future_calendar_events(), so a fixed=0 result is a valid
    "ran, nothing to do" outcome) if it ran, or None if NEITHER
    condition applies (truly nothing to do, both markers left alone).

    2026-07-16: previously, the fix for a persistent-conflict
    incident wouldn't take effect until someone remembered to separately
    run `my-bt admin resync-calendar` by hand -- fixed so `my-bt setup`
    picks this up automatically instead. The format-version check
    alone used to be the ONLY thing this function looked at, so a
    format-unchanged marker made it return None immediately even when
    check_calendar_invite_resync_skips() had known, already-recorded
    failures sitting there from a previous run -- those would just sit
    forever until someone manually ran the resync command, regardless of
    whether whatever had been breaking them (e.g. the 2026-07-16 stale-
    SEQUENCE bug) had since been fixed. Now a non-empty skip marker is
    its own, independent reason to retry, same as a stale format-version
    marker -- `setup -i` retries known failures itself instead of
    silently requiring a human to remember to.

    Writes the new version to the marker only AFTER resync_all_future_
    calendar_events() RETURNS -- if it raises (a hard failure before/
    outside its own per-occurrence loop, e.g. the CalDAV server being
    unreachable at all), the marker is left at its old value so the next
    run tries again, instead of silently recording "done" for a resync
    that didn't actually happen. A single occurrence with a persistent
    conflict, on the other hand, no longer counts as a hard failure here
    -- see that function's own 2026-07-15 docstring update: it's skipped
    and logged, not raised, so the marker still gets written and every
    OTHER occurrence still gets resynced; a real production incident hit
    exactly this (one occurrence stuck in a stale-ETag loop) before that
    fix, which meant the marker could never be written and every future
    `setup -i` run failed on the same occurrence, forever.

    Also calls record_resync_skips() -- see its own docstring -- so a
    skip discovered HERE stays visible in every later `admin health`/
    `admin setup` run too, not just this one's own printed output
    (2026-07-15/16: the previous version of this function returned a
    plain int, which silently dropped exactly that information).

    A missing format-version marker (fresh install, or a data dir that
    predates this feature) is treated as "definitely stale" -- always
    resyncs once (a no-op scan if there's nothing booked yet) and writes
    the marker, so every install ends up with one recorded regardless of
    history."""
    marker_path = Path(data_dir) / CALENDAR_INVITE_FORMAT_VERSION_MARKER_NAME
    try:
        recorded = marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = None
    format_is_stale = recorded != str(format_version)

    skip_marker_path = Path(data_dir) / CALENDAR_INVITE_RESYNC_SKIPPED_MARKER_NAME
    try:
        has_pending_skips = bool(skip_marker_path.read_text(encoding="utf-8").strip())
    except OSError:
        has_pending_skips = False

    if not format_is_stale and not has_pending_skips:
        return None
    result = resync_all_future_calendar_events(client, calendar_href, store, settings, today=today)
    # 2026-07-15: atomic_write_text (temp file + fsync + rename + dir
    # fsync), not a bare write_text() -- a torn write here on a hard
    # crash would leave a marker that's neither the old nor the new
    # version, misreporting drift either way. See app/atomic_io.py.
    # Written even when we only ran because of pending skips (format_is_
    # stale False) -- harmless (same value it already had) and keeps
    # this the one place that owns writing it.
    # 2026-07-10: secure=True -- this marker lives in the same shared
    # data_dir as users.csv/registrations.csv, written by the exact same
    # root-run `my-bt admin resync-calendar`/`setup -i` that broke those
    # (see app.atomic_io.secure_data_path's own docstring); nothing about
    # that failure mode is CSV-specific, so this gets the same treatment.
    atomic_write_text(marker_path, f"{format_version}\n", secure=True, mode=0o640)
    record_resync_skips(data_dir, result)
    return result


# -- "Cancel entire session" blocker events (2026-07-14) ---------------------
#
# The host asked for the calendar itself to be the mechanism: "A canceled
# event by me creates this blocker event in [the booking calendar], which
# then must trigger this date to NOT be shown." The booking page already
# hides any date whose course hours overlap a calendar event that isn't
# the tool's own sync event (the real-time conflict check in
# app/webapp.py::_conflict_checker / app/slots.py) -- so canceling an
# entire session now PUTs a visible "CANCELED: <course>" event at the
# course hours, and that existing mechanism does the blocking. Deleting
# the blocker event in the calendar reopens the date -- the host's own
# natural control, no admin page needed. (This replaced a same-day
# interim canceled_occurrences.csv marker -- see SOLUTION-DESIGN.md #33.)


def cancellation_blocker_uid(settings: Settings, course_shortname: str, occurrence_date: date) -> str:
    """Deterministic UID for one occurrence's blocker -- deterministic so
    a double-tapped cancel link maps to the SAME event (create is then
    idempotent via If-None-Match, see create_cancellation_blocker) and so
    a host reinstate can delete it without searching. Deliberately does
    NOT match is_own_event() (the "canceled-" prefix comes before the
    slug, and is_own_event requires the uid to START with the slug):
    "own" events are exactly what the conflict check skips, and the whole
    point of this one is to BE a conflict."""
    slug, domain = _uid_parts(settings)
    return f"canceled-{slug}-{course_shortname}-{occurrence_date.isoformat()}@{domain}"


def hide_blocker_uid(settings: Settings, course_shortname: str, occurrence_date: date) -> str:
    """Deterministic UID for one occurrence's HIDE blocker. Same shape and
    the same reasoning as cancellation_blocker_uid above (deterministic,
    and deliberately not matching is_own_event) but its own prefix, so a
    hidden date and a canceled one are never mistaken for each other --
    by this code, or by the operator looking at their calendar app."""
    slug, domain = _uid_parts(settings)
    return f"hidden-{slug}-{course_shortname}-{occurrence_date.isoformat()}@{domain}"


def _put_blocker(
    client: CalDAVClient,
    calendar_href: str,
    settings: Settings,
    course: Course,
    occurrence_date: date,
    uid: str,
    summary: str,
    lines: list[str],
) -> None:
    """Shared body of the two blocker writers below: build the event at
    the occurrence's real (override-aware) hours and PUT it, treating an
    existing one as a no-op.

    Factored out when the HIDE blocker arrived (2026-08-27) rather than
    copied: these two must stay identical in their timing, their
    create-is-idempotent behaviour and their fail-closed contract, and
    two copies of that is how they would quietly stop being identical."""
    tz = ZoneInfo(settings.timezone)
    start, end = occurrence_start_end(course, occurrence_date, tz)
    event = VEvent(
        uid=uid,
        summary=summary,
        description="\n".join(lines),
        location=course.location,
        start=start,
        end=end,
    )
    try:
        client.put_event(calendar_href, event.uid, event.to_ics(), etag=None)
    except CalDAVConflictError:
        log.debug("blocker %s already exists -- keeping it", uid)


def create_cancellation_blocker(
    client: CalDAVClient,
    calendar_href: str,
    settings: Settings,
    course: Course,
    occurrence_date: date,
    message: str = "",
) -> None:
    """PUTs the blocker event for one canceled occurrence onto the
    booking calendar, at the occurrence's real (override-aware) hours.
    The description tells the host, right in their calendar app, how to
    reopen the date. An already-existing blocker (double-tapped cancel
    link -- put_event's If-None-Match create raises CalDAVConflictError)
    is a no-op: the block is in place either way. Any OTHER CalDAV
    failure propagates -- callers cancel registrations only AFTER this
    succeeds (fail-closed: a session must never end up canceled but
    still bookable because this PUT silently failed).

    NOTE: the conflict check hides the date off this blocker either via a
    blocks-mode [[conflict_calendar]] entry that covers the booking
    calendar AND applies to this course, OR -- for a course scoped out of
    every such entry (e.g. via all_courses_but) -- via the always-on,
    UID-keyed blocker check in
    conflict.ConflictEngine.occurrence_is_hidden, which is independent of
    scoping precisely so this mechanism can never be configured away."""
    lines = []
    if message:
        lines.append(message)
        lines.append("")
    lines += [
        f"This blocker keeps {occurrence_date.isoformat()} unbookable for",
        f"{course.title} ({settings.base_url}/book/{course.shortname}).",
        "Delete this event to reopen the date for booking",
        "(rebooking a canceled participant from /admin reopens it too).",
    ]
    _put_blocker(
        client, calendar_href, settings, course, occurrence_date,
        uid=cancellation_blocker_uid(settings, course.shortname, occurrence_date),
        summary=f"CANCELED: {course.title}",
        lines=lines,
    )


def create_hide_blocker(
    client: CalDAVClient,
    calendar_href: str,
    settings: Settings,
    course: Course,
    occurrence_date: date,
) -> None:
    """PUTs the HIDE blocker for one occurrence (2026-08-27).

    Hiding is NOT cancelling: the session still takes place and anyone
    already booked keeps their place -- the date simply stops being
    offered to new guests. /admin only offers it while a date has NO
    bookings, so there is no state in which a booked guest silently loses
    their session.

    A calendar event rather than a flag in a file, deliberately: the
    operator's own standing point that when they are on the go they have
    only their calendar. Deleting this event anywhere -- phone included --
    offers the date again, with no admin console involved."""
    _put_blocker(
        client, calendar_href, settings, course, occurrence_date,
        uid=hide_blocker_uid(settings, course.shortname, occurrence_date),
        summary=f"NOT OFFERED: {course.title}",
        lines=[
            f"This blocker keeps {occurrence_date.isoformat()} closed to NEW",
            f"bookings for {course.title}",
            f"({settings.base_url}/book/{course.shortname}).",
            "The session itself is NOT canceled.",
            "Delete this event to offer the date again.",
        ],
    )


def delete_hide_blocker(
    client: CalDAVClient,
    calendar_href: str,
    settings: Settings,
    course_shortname: str,
    occurrence_date: date,
) -> None:
    """Removes one occurrence's HIDE blocker -- "Unhide" in /admin, and
    exactly what deleting the event by hand in a calendar app does."""
    client.delete_event(
        calendar_href, hide_blocker_uid(settings, course_shortname, occurrence_date)
    )


def delete_cancellation_blocker(
    client: CalDAVClient,
    calendar_href: str,
    settings: Settings,
    course_shortname: str,
    occurrence_date: date,
) -> None:
    """Removes one occurrence's blocker event -- called when the host
    REINSTATES a registration on it (putting someone back in means the
    session IS happening, and a blocker would keep hiding the date from
    new bookings). No-op if there's no blocker (the overwhelmingly
    common case -- most reinstates follow a single-row cancel;
    delete_event already tolerates 404)."""
    client.delete_event(
        calendar_href, cancellation_blocker_uid(settings, course_shortname, occurrence_date)
    )


# -- Emailed guest invite/cancel attachments (2026-07-09: also attach a
# calendar invite to the email sent to the participant) ---------------------
#
# Deliberately separate from sync_occurrence() above: that function builds
# ONE shared operator-facing event (all participants + a "Participants:"
# table) and PUTs it to the operator's own CalDAV calendar. These two
# functions instead build a personal, single-guest .ics MEANT AS AN EMAIL
# ATTACHMENT -- never sent to CalDAV, never containing anyone else's name/
# email. Same UID as the operator's own event (via event_uid()) since it's
# the same real-world occurrence, so if a client ever DOES correlate by UID
# across the two, the identity still matches correctly.


def guest_invite_ics(settings: Settings, course: Course, occurrence_date: date) -> tuple[str, str]:
    """Returns (filename, ics_text) for the "add this to your calendar"
    attachment on a CONFIRMED booking's own email (see
    app/webapp.py::_send_booking_result_guest_email and
    app/cancel_flow.py's promoted-from-waitlist email) -- both are the only
    two points where a guest goes from "not confirmed" to "actually holds a
    real spot", which is the only time there's a real event worth adding to
    a personal calendar. Deliberately NOT sent on a waitlisted email: there's
    no confirmed slot yet, nothing real to add.

    METHOD:PUBLISH, not REQUEST: this is a plain "here's your booking, add
    it if you like" notice, not a meeting invite with ORGANIZER/ATTENDEE/
    RSVP tracking. A REQUEST's Accept/Decline buttons (rendered natively by
    Outlook/Google Calendar) would imply declining changes something -- it
    wouldn't, canceling still only ever happens via the real cancel link --
    so offering that button would be actively misleading. This is exactly
    what real booking confirmations (train tickets, cinema, restaurant
    reservations) use for the same reason.
    """
    tz = ZoneInfo(settings.timezone)
    start, end = occurrence_start_end(course, occurrence_date, tz)
    description = html_to_text(course.description) if course.description else course.title
    event = VEvent(
        uid=event_uid(settings, course.shortname, occurrence_date),
        summary=course.title,
        description=description,
        location=course.location,
        start=start,
        end=end,
        method="PUBLISH",
        alarms_minutes_before=settings.participant_calendar_reminder_minutes,
    )
    return f"{course.shortname}-{occurrence_date.isoformat()}.ics", event.to_ics()


def guest_cancel_ics(settings: Settings, course: Course, occurrence_date: date) -> tuple[str, str]:
    """Returns (filename, ics_text) for a METHOD:CANCEL attachment on a
    cancellation email (2026-07-09: adds a CANCEL-ics too, as a courtesy)
    -- same UID as the original guest_invite_ics() above,
    SEQUENCE bumped to 1 and STATUS:CANCELLED set, so a calendar app that
    both (a) previously imported the PUBLISH invite via UID and (b) honors
    plain (non-REQUEST) CANCEL/SEQUENCE semantics will remove or gray out
    the entry on its own. Support for that varies by client -- there's no
    ORGANIZER/ATTENDEE relationship to formally correlate through (see
    guest_invite_ics()'s own docstring on why this deliberately isn't a
    REQUEST/RSVP flow) -- but it's the standard, correct thing to send
    regardless, and costs nothing for a client that ignores it: worst case
    the guest just deletes the (now stale) entry themselves, exactly as if
    this attachment didn't exist. No VALARMs on a cancellation -- nothing
    left to be reminded about."""
    tz = ZoneInfo(settings.timezone)
    start, end = occurrence_start_end(course, occurrence_date, tz)
    event = VEvent(
        uid=event_uid(settings, course.shortname, occurrence_date),
        summary=course.title,
        description=f"Canceled: {course.title}",
        location=course.location,
        start=start,
        end=end,
        method="CANCEL",
        sequence=1,
        status="CANCELLED",
        alarms_minutes_before=(),
    )
    return f"{course.shortname}-{occurrence_date.isoformat()}-canceled.ics", event.to_ics()
