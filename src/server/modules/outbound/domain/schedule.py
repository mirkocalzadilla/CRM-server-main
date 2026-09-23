"""Pure scheduling rules for business-initiated sends (no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.shared.timezone import to_business_time


def as_utc(moment: datetime) -> datetime:
    """Naive datetimes (SQLite, some drivers) are stored in UTC; make them explicit."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def is_within_send_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True when the business-local hour is inside [start_hour, end_hour)."""
    local_hour = to_business_time(now).hour
    return start_hour <= local_hour < end_hour


def due_reminder_window(starts_at: datetime, now: datetime, windows: tuple[int, ...]) -> int | None:
    """Which reminder (in hours before the event) is due right now.

    Windows are checked from the closest to the event outwards, so an entry issued
    late gets a single reminder (the closest one) instead of a burst. Past events
    never get reminders.
    """
    remaining = as_utc(starts_at) - as_utc(now)
    if remaining <= timedelta(0):
        return None
    for hours in sorted(windows):
        if remaining <= timedelta(hours=hours):
            return hours
    return None
