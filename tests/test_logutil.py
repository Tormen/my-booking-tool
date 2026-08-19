"""Regression coverage for the 2026-07-05 fix: logging.Formatter's
%(asctime)s defaults to the server's LOCAL system time, but every other
timestamp in this app is UTC (storage.now_iso(), watchdog.py's own
datetime.now(timezone.utc) calls) -- and app/watchdog.py's
_parse_app_log_timestamp() parses this exact asctime format and labels it
UTC directly. That was only "correct by accident" as long as the server's
OS timezone happened to be UTC; changing the server's system timezone
(e.g. to Europe/Brussels) would have silently skewed the watchdog's
rate-limit-block detection window by the UTC offset.
configure_logging() now forces UTC via Formatter.converter, independent
of whatever the OS clock is set to.
"""
import logging
import logging.handlers
import os
import tempfile
import time
import unittest

from app.logutil import LOG_FILE_BACKUP_COUNT, LOG_FILE_MAX_BYTES, configure_logging


class ConfigureLoggingUtcTest(unittest.TestCase):
    def tearDown(self):
        # configure_logging() attaches handlers to the root logger --
        # clear them so this test can't leak state into any other test
        # in the same process (unittest runs all test modules in one
        # process).
        logging.getLogger().handlers.clear()

    def test_asctime_formatter_uses_utc_not_local_system_time(self):
        configure_logging()
        handlers = logging.getLogger().handlers
        self.assertTrue(handlers, "configure_logging() should attach at least one handler")
        for h in handlers:
            self.assertIsNotNone(h.formatter)
            self.assertIs(
                h.formatter.converter, time.gmtime,
                "log timestamps must use time.gmtime (UTC), not the default "
                "time.localtime -- see app/watchdog.py::_parse_app_log_timestamp, "
                "which assumes this format is already UTC",
            )

    def test_log_file_handler_is_size_capped_rotating(self):
        # 2026-07-16: file logging became ON by default (see
        # config.DEFAULT_LOG_FILE), which makes unbounded growth a real
        # risk -- no logrotate config ships with this project, so the
        # handler itself must rotate. A plain FileHandler here would
        # silently reintroduce the forever-growing file.
        with tempfile.TemporaryDirectory() as tmp:
            configure_logging(log_file=os.path.join(tmp, "app.log"))
            file_handlers = [
                h for h in logging.getLogger().handlers
                if isinstance(h, logging.FileHandler)
            ]
            self.assertEqual(len(file_handlers), 1)
            h = file_handlers[0]
            self.assertIsInstance(h, logging.handlers.RotatingFileHandler)
            self.assertEqual(h.maxBytes, LOG_FILE_MAX_BYTES)
            self.assertEqual(h.backupCount, LOG_FILE_BACKUP_COUNT)
            # TemporaryDirectory cleanup on Windows-ish filesystems (and
            # tidiness anywhere) needs the handle released first.
            h.close()
