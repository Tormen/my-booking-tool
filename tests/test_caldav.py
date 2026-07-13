import unittest
from datetime import datetime, timezone
from unittest import mock

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
        # 2026-07-07: a real production 500 on /my/confirm was
        # root-caused to a stale-ETag CalDAV 412 -- calendar_sync.
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

    def test_query_events_logs_uid_etag_and_both_hrefs_at_debug(self):
        # 2026-07-16: retrying more often wasn't fixing the underlying
        # calendar problem, so debug output is collected instead -- one
        # candidate root cause is the
        # server reporting a different href for an event than the one
        # put_event()/delete_event() assume (`<uid>.ics`); this just
        # confirms the raw facts (uid, etag, reported href, assumed
        # href) actually reach the debug log so a human can compare them
        # if that turns out to be it.
        self.transport.script(
            "REPORT", "https://dav.mailbox.org/caldav/YogaBookings/", Response(207, {}, REPORT_BODY)
        )
        with mock.patch("app.caldav_client.log") as m_log:
            self.client.query_events(
                "/caldav/YogaBookings/",
                datetime(2026, 7, 8, tzinfo=timezone.utc),
                datetime(2026, 7, 9, tzinfo=timezone.utc),
            )
        debug_messages = [call.args[0] % call.args[1:] for call in m_log.debug.call_args_list]
        self.assertTrue(any("example-org-yoga-class-1-2026-07-08@example.org" in msg for msg in debug_messages))
        self.assertTrue(any('"abc123"' in msg for msg in debug_messages))
        self.assertTrue(any("/caldav/YogaBookings/example-org-yoga-class-1-2026-07-08.ics" in msg
                             for msg in debug_messages))

    def test_error_response_is_logged_at_debug_with_headers_and_full_body(self):
        # Same incident: a 412 (or any error) response's FULL body/headers
        # (minus Authorization) should reach the debug log via
        # HttpTransport, not just the 200-char-truncated message that ends
        # up in the raised exception/warning line.
        self.transport.script(
            "PUT", "https://dav.mailbox.org/caldav/YogaBookings/",
            Response(412, {"ETag": '"server-side"'}, "<D:error xmlns:D=\"DAV:\">full diagnostic detail</D:error>"),
        )
        # FakeTransport doesn't run HttpTransport's own logging (it stands
        # in for the whole transport callable) -- this test exercises
        # HttpTransport directly instead, which is what production uses.
        from app.caldav_client import HttpTransport

        class _FakeConn:
            def __init__(self, *a, **kw):
                pass

            def request(self, *a, **kw):
                pass

            def getresponse(self):
                class _Resp:
                    status = 412
                    def read(self_inner):
                        return b'<D:error xmlns:D="DAV:">full diagnostic detail</D:error>'
                    def getheaders(self_inner):
                        return [("ETag", '"server-side"')]
                return _Resp()

            def close(self):
                pass

        transport = HttpTransport("user", "pass")
        with mock.patch("http.client.HTTPSConnection", return_value=_FakeConn()), \
             mock.patch("app.caldav_client.log") as m_log:
            resp = transport("PUT", "https://dav.mailbox.org/caldav/YogaBookings/some-uid.ics", body="ICS...")
        self.assertEqual(resp.status, 412)
        debug_messages = [call.args[0] % call.args[1:] for call in m_log.debug.call_args_list]
        self.assertTrue(any("full diagnostic detail" in msg for msg in debug_messages))
        self.assertTrue(any("FAILED" in msg for msg in debug_messages))
        # The Authorization header must never appear in a debug log line.
        self.assertFalse(any("Basic " in msg for msg in debug_messages))


if __name__ == "__main__":
    unittest.main()
