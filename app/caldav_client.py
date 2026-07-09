"""Minimal CalDAV client: just enough to (a) list events in a time window
for conflict-checking and (b) create/update/delete a single event per course
occurrence. Stdlib only (http.client + xml.etree), no external 'caldav' or
'requests' package.

A `transport` callable can be injected for testing -- see tests/test_caldav.py.
Default transport does real HTTPS requests via http.client.
"""
from __future__ import annotations

import base64
import http.client
import logging
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from .ics import _fmt_dt  # noqa: F401 (re-exported for callers/tests)

log = logging.getLogger("my_booking.caldav")

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

ET.register_namespace("D", DAV_NS)
ET.register_namespace("C", CALDAV_NS)


@dataclass
class Response:
    status: int
    headers: dict
    body: str


class HttpTransport:
    """Real HTTPS transport used in production."""

    def __init__(self, username: str, password: str, timeout: float = 15.0):
        self._auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._timeout = timeout

    def __call__(self, method: str, url: str, body: str = "", extra_headers: dict | None = None) -> Response:
        parts = urlsplit(url)
        conn = http.client.HTTPSConnection(
            parts.netloc, timeout=self._timeout, context=ssl.create_default_context()
        )
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Content-Type": "application/xml; charset=utf-8",
            "User-Agent": "my-booking-tool/1.0",
        }
        if extra_headers:
            headers.update(extra_headers)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request(method, path, body=body.encode("utf-8") if body else None, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
            # DEBUG-only: method/path/status, never the auth header (the
            # request/response bodies are calendar data, not secret, but
            # still no reason to duplicate them into the journal on every
            # single successful request).
            log.debug("%s %s -> HTTP %d", method, path, resp.status)
            if resp.status >= 400:
                # 2026-07-16, the operator, after a persistent-conflict incident
                # that 6x-ing the retry count didn't actually explain:
                # "But there must be another problem with the Calendar.
                # Please do NOT retry more often!!! But rather collect
                # DEBUG OUTPUT please!!!" On any error response (a 412
                # conflict included), log everything needed to root-cause
                # it later -- our own If-Match/If-None-Match (the etag we
                # THOUGHT was current), every other request header except
                # Authorization, and the full (untruncated) response body,
                # which for a WebDAV/CalDAV error is normally an XML
                # <D:error> body naming exactly what precondition failed.
                # Enable via `my-bt -D` / `MY_BOOKING_DEBUG=1` -- see
                # app/logutil.py.
                safe_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                log.debug(
                    "%s %s -> HTTP %d FAILED -- request headers=%r, response headers=%r, "
                    "response body=%r",
                    method, path, resp.status, safe_headers, dict(resp.getheaders()), data,
                )
            return Response(resp.status, dict(resp.getheaders()), data)
        finally:
            conn.close()


class CalDAVError(RuntimeError):
    pass


class CalDAVConflictError(CalDAVError):
    """HTTP 412 Precondition Failed on a PUT/DELETE with If-Match: the
    ETag we read is already stale -- something else (typically another
    near-simultaneous booking/cancellation/confirmation for the SAME
    occurrence) modified this exact calendar event between our read and
    our write. 2026-07-07, the operator (a real production 500 on /my/confirm,
    "PUT ... -> HTTP 412: ... a newer version of the appointment already
    exists"): a plain retry a few seconds later succeeded on its own, i.e.
    genuinely transient -- see calendar_sync.sync_occurrence's own retry
    loop, which is what this distinct exception type exists to let that
    loop catch specifically, instead of treating every CalDAVError
    (including a real, non-transient failure) as equally retryable."""


class CalDAVClient:
    def __init__(self, base_url: str, username: str, password: str, transport=None):
        self.base_url = base_url.rstrip("/") + "/"
        self.transport = transport or HttpTransport(username, password)

    # -- discovery -------------------------------------------------------

    def list_calendars(self) -> dict[str, str]:
        """PROPFIND Depth:1 on base_url; returns {displayname: href}."""
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:"><D:prop>'
            "<D:displayname/><D:resourcetype/>"
            "</D:prop></D:propfind>"
        )
        resp = self.transport(
            "PROPFIND", self.base_url, body=body, extra_headers={"Depth": "1"}
        )
        if resp.status not in (207,):
            raise CalDAVError(f"PROPFIND {self.base_url} -> HTTP {resp.status}")
        root = ET.fromstring(resp.body)
        out: dict[str, str] = {}
        for response in root.findall(f"{{{DAV_NS}}}response"):
            href_el = response.find(f"{{{DAV_NS}}}href")
            name_el = response.find(f".//{{{DAV_NS}}}displayname")
            if href_el is not None and name_el is not None and name_el.text:
                out[name_el.text] = href_el.text
        return out

    def calendar_href(self, display_name: str) -> str:
        calendars = self.list_calendars()
        if display_name in calendars:
            return calendars[display_name]
        raise CalDAVError(
            f"calendar '{display_name}' not found among {list(calendars)} -- "
            "check settings.toml [calendar].booking_calendar / conflict_calendars"
        )

    # -- events ------------------------------------------------------------

    def query_events(self, calendar_href: str, start: datetime, end: datetime) -> list[tuple[str, str, str]]:
        """Returns list of (uid, ics_text, etag) overlapping [start, end)."""
        url = self.base_url.rsplit("/", 1)[0] if False else self._absolute(calendar_href)
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{_fmt_dt(start)}" end="{_fmt_dt(end)}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""
        resp = self.transport("REPORT", url, body=body, extra_headers={"Depth": "1"})
        if resp.status not in (207,):
            raise CalDAVError(f"REPORT {url} -> HTTP {resp.status}")
        root = ET.fromstring(resp.body)
        out = []
        for response in root.findall(f"{{{DAV_NS}}}response"):
            etag_el = response.find(f".//{{{DAV_NS}}}getetag")
            data_el = response.find(f".//{{{CALDAV_NS}}}calendar-data")
            if data_el is not None and data_el.text:
                from .ics import parse_uid
                uid = parse_uid(data_el.text) or ""
                etag = etag_el.text if etag_el is not None else ""
                # 2026-07-16, the operator, diagnosing a persistent-conflict
                # incident ("there must be another problem with the
                # Calendar"): put_event()/delete_event() below both ASSUME
                # this event's href is exactly `<uid>.ics` under
                # calendar_href, since that's what WE named it when we
                # created it. If the server ever reports a DIFFERENT href
                # for this uid (renamed/normalized server-side, percent-
                # encoding, or a second/duplicate resource with a
                # colliding UID), a PUT/DELETE built from our own assumed
                # href wouldn't match the etag we just read for THIS
                # href -- one possible explanation for a 412 that no
                # amount of retrying could ever resolve. Deliberately
                # just logs the raw facts (not an automatic "this is a
                # mismatch" verdict -- a same-meaning href that merely
                # differs in percent-encoding would be a false alarm) so
                # a human can compare them if this happens again with
                # `my-bt -D` / MY_BOOKING_DEBUG=1 on.
                href_el = response.find(f"{{{DAV_NS}}}href")
                log.debug(
                    "query_events: uid=%r etag=%r reported at href=%r (we'd PUT/DELETE it at %r)",
                    uid, etag, href_el.text if href_el is not None else None, f"{url}{uid}.ics",
                )
                out.append((uid, data_el.text, etag))
        return out

    def put_event(self, calendar_href: str, uid: str, ics_text: str, etag: str | None = None) -> str:
        url = self._absolute(calendar_href) + f"{uid}.ics"
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match"] = etag if etag else "*"
        if etag is None:
            # creating a new event: must NOT already exist
            headers["If-None-Match"] = "*"
            del headers["If-Match"]
        resp = self.transport("PUT", url, body=ics_text, extra_headers=headers)
        if resp.status == 412:
            raise CalDAVConflictError(f"PUT {url} -> HTTP 412: {resp.body[:200]}")
        if resp.status not in (200, 201, 204):
            raise CalDAVError(f"PUT {url} -> HTTP {resp.status}: {resp.body[:200]}")
        return resp.headers.get("etag", "")

    def delete_event(self, calendar_href: str, uid: str, etag: str | None = None) -> None:
        url = self._absolute(calendar_href) + f"{uid}.ics"
        headers = {"If-Match": etag} if etag else {}
        resp = self.transport("DELETE", url, extra_headers=headers)
        if resp.status == 412:
            raise CalDAVConflictError(f"DELETE {url} -> HTTP 412: {resp.body[:200]}")
        if resp.status not in (200, 204, 404):
            raise CalDAVError(f"DELETE {url} -> HTTP {resp.status}")

    def _absolute(self, href: str) -> str:
        if href.startswith("http"):
            return href if href.endswith("/") else href + "/"
        parts = urlsplit(self.base_url)
        base = f"{parts.scheme}://{parts.netloc}"
        return base + href if href.endswith("/") else base + href + "/"
