"""Shared test helpers -- build a minimal in-memory Settings without
touching the filesystem for secrets (unlike app.config.load_settings, which
reads real secret files by design)."""
from __future__ import annotations

import logging

from app.config import Course, Settings

# app/logutil.py's configure_logging() is only called by the real
# entrypoints (serve.py, retention.py, scripts/my-bt) -- tests call library
# functions like retention.run_purge()/erasure.erase_user_by_email()
# directly, without going through that. Some of those functions log at
# WARNING deliberately (see their own comments) so they're visible by
# default in production; without this, that same WARNING output would
# print to stderr during every test run via logging's "no handler
# configured" fallback. This file is imported by every test module, so one
# disable() here keeps test output clean without touching production
# behavior at all.
logging.disable(logging.CRITICAL)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        timezone="Europe/Berlin",
        admin_email="admin@example.org",
        base_url="https://example.org",
        caldav_url="https://dav.mailbox.org/",
        caldav_username="calendar@example.org",
        caldav_password="secret",
        booking_calendar="Bookings",
        conflict_calendars=("Calendar", "Bookings"),
        smtp_host="smtp.mailbox.org",
        smtp_port=465,
        smtp_username="calendar@example.org",
        smtp_password="secret",
        smtp_from="admin@example.org",
        admin_password_hash="deadbeef$deadbeef",
        show_next_slots=4,
        show_next_days=42,
        min_notice_hours=2,
        retention_months=24,
        canceled_retention_months=6,
        erasure_pepper=b"\x01" * 32,
        courses=(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_course(**overrides) -> Course:
    defaults = dict(
        shortname="lux-wed-yoga",
        title="Dynamic Ashtanga Vinyasa Yoga",
        location="Example Community Gym, Room 1",
        weekday="wed",
        start_time="17:15",
        duration_minutes=100,
        capacity=14,
        audience="private",
        language="en",
        description="test",
    )
    defaults.update(overrides)
    return Course(**defaults)
