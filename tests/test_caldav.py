import unittest
from datetime import datetime, timezone

from app.caldav_client import CalDAVClient, CalDAVConflictError, CalDAVError, Response


class FakeTransport:
    """Records calls and returns scripted responses, keyed by (method, url-prefix)."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def script(self, method: str, url_prefix: str, response: Response):
        self.responses[(method, url_prefix)] = response

    def __call__(self, method, url, body="", extra_headers=None):
        self.calls.append((method, url, body, extra_headers or {}))
        for (m, prefix), resp in self.responses.items():
            if m == method and url.startswith(prefix):
                return resp
        raise AssertionError(f"no scripted response for {method} {url}")


PROPFIND_BODY = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/caldav/Calendar/</D:href>
    <D:propstat><D:prop><D:displayname>Calendar</D:displayname></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/caldav/YogaBookings/</D:href>
    <D:propstat><D:prop><D:displayname>Yoga-Bookings</D:displayname></D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


REPORT_BODY = """<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/caldav/YogaBookings/example-org-yoga-class-1-2026-07-08.ics</D:href>
    <D:propstat><D:prop>
      <D:getetag>"abc123"</D:getetag>
      <C:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:example-org-yoga-class-1-2026-07-08@example.org
DTSTART:20260708T171500Z
DTEND:20260708T185500Z
SUMMARY:Test
END:VEVENT
END:VCALENDAR
</C:calendar-data>
    </D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


class CalDAVClientTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = CalDAVClient("https://dav.mailbox.org/", "user", "pass", transport=self.transport)

    def test_list_calendars_parses_displaynames(self):
        self.transport.script("PROPFIND", "https://dav.mailbox.org/", Response(207, {}, PROPFIND_BODY))
        calendars = self.client.list_calendars()
        self.assertEqual(calendars["Calendar"], "/caldav/Calendar/")
        self.assertEqual(calendars["Yoga-Bookings"], "/caldav/YogaBookings/")

    def test_calendar_href_raises_if_missing(self):
        self.transport.script("PROPFIND", "https://dav.mailbox.org/", Response(207, {}, PROPFIND_BODY))
        with self.assertRaises(CalDAVError):
            self.client.calendar_href("Does Not Exist")

    def test_query_events_parses_uid_and_etag(self):
        self.transport.script(
            "REPORT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(207, {}, REPORT_BODY)
        )
        events = self.client.query_events(
            "/caldav/YogaBookings/",
            datetime(2026, 7, 8, tzinfo=timezone.utc),
            datetime(2026, 7, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(len(events), 1)
        uid, ics, etag = events[0]
        self.assertEqual(uid, "example-org-yoga-class-1-2026-07-08@example.org")
        self.assertEqual(etag, '"abc123"')

    def test_put_event_new_uses_if_none_match(self):
        self.transport.script(
            "PUT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(201, {"etag": '"new"'}, "")
        )
        etag = self.client.put_event("/caldav/YogaBookings/", "some-uid", "ICS...", etag=None)
        self.assertEqual(etag, '"new"')
        method, url, body, headers = self.transport.calls[-1]
        self.assertEqual(headers.get("If-None-Match"), "*")
        self.assertNotIn("If-Match", headers)

    def test_put_event_update_uses_if_match(self):
        self.transport.script(
            "PUT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(204, {}, "")
        )
        self.client.put_event("/caldav/YogaBookings/", "some-uid", "ICS...", etag='"old"')
        method, url, body, headers = self.transport.calls[-1]
        self.assertEqual(headers.get("If-Match"), '"old"')

    def test_put_event_error_status_raises(self):
        self.transport.script(
            "PUT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(500, {}, "server error")
        )
        with self.assertRaises(CalDAVError):
            self.client.put_event("/caldav/YogaBookings/", "some-uid", "ICS...", etag='"old"')

    def test_put_event_412_raises_the_specific_conflict_subclass(self):
        # 2026-07-07, the operator (a real production 500 on /my/confirm,
        # root-caused to a stale-ETag CalDAV 412): calendar_sync.
        # sync_occurrence's retry loop needs to catch THIS conflict case
        # specifically (and re-fetch a fresh ETag), not every CalDAVError
        # indiscriminately -- a genuine, non-transient failure should
        # still propagate on the first attempt.
        self.transport.script(
            "PUT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(412, {}, "conflict")
        )
        with self.assertRaises(CalDAVConflictError):
            self.client.put_event("/caldav/YogaBookings/", "some-uid", "ICS...", etag='"old"')

    def test_delete_event_412_raises_the_specific_conflict_subclass(self):
        self.transport.script(
            "DELETE", "https://dav.mailbox.org/caldav/YogaBookings/", Response(412, {}, "conflict")
        )
        with self.assertRaises(CalDAVConflictError):
            self.client.delete_event("/caldav/YogaBookings/", "some-uid", etag='"old"')

    def test_delete_event_tolerates_404(self):
        self.transport.script(
            "DELETE", "https://dav.mailbox.org/caldav/YogaBookings/", Response(404, {}, "")
        )
        self.client.delete_event("/caldav/YogaBookings/", "some-uid", etag='"old"')  # no raise


if __name__ == "__main__":
    unittest.main()
