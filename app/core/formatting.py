from __future__ import annotations

from datetime import timedelta
from decimal import Decimal


def format_quantity(value: Decimal) -> str:
    """Fixed-point (never scientific notation) with trailing zeros trimmed,
    so fractional share counts stay readable at any capital size - "0.5"
    rather than "0.500000", but also "1234567" rather than "1.23457e+6"."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_uptime(delta: timedelta) -> str:
    """Short human-readable duration ("45с", "12м", "3ч 20м") - shared by
    the GUI's "Автоматизация" panel and the Telegram bot's /status reply,
    so "how long has the daemon been up" reads identically in both."""
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return f"{total_seconds}с"
    minutes, _ = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}м"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м"
