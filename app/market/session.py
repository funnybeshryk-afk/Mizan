from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

# NYSE's regular session, Eastern local time. Deliberately not modeling
# early closes (day before Thanksgiving, etc.) or holidays - a bar simply
# never exists for a day/time the market wasn't open, so nothing here
# needs its own holiday calendar to stay correct.
EASTERN = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)

# Shared by OpeningRangeBreakoutStrategy (session-open-relative windowing)
# and app.automation.daemon's force_session_close (a fixed cutoff time) -
# one place for the UTC-bars-to-ET-session-time conversion, per the
# project's existing convention that Bar.timestamp is always tz-aware UTC
# (see AlpacaMarketDataProvider, app.market.cache._to_utc_naive).


def to_eastern(dt: datetime) -> datetime:
    """Converts a timestamp to US Eastern local time, correctly handling
    the EST/EDT switch via the IANA tz database rather than a fixed UTC
    offset. A naive datetime is assumed to already be UTC, matching how
    the rest of the codebase treats bar timestamps.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN)


def minutes_since_session_open(dt: datetime) -> float:
    """Minutes elapsed since 09:30 Eastern on dt's own Eastern calendar
    day. Negative before the open (e.g. pre-market bars)."""
    eastern_dt = to_eastern(dt)
    open_dt = eastern_dt.replace(
        hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0
    )
    return (eastern_dt - open_dt).total_seconds() / 60
