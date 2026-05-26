"""Human-readable time formatters for the header-status widget (bd 47uh).

Single source of truth for header-status time strings. Lives in the
``services`` layer so both ``services/monitor_cycle.py`` (publishes
``SseStatus``) and ``web/monitor_vm.py`` (initial render) can call it
without a layer-inversion (services → web would violate the layering).

All functions are pure — no clock, no I/O — so callers inject time
values. This keeps unit tests trivial and avoids ``time.now()`` globals.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def humanize_relative_age(age: timedelta) -> str:
    """Compact Russian relative-time string suitable for a header chip.

    Granularity is deliberately coarse (minute / hour / day buckets) — the
    widget is glanceable, not a stopwatch. Negative ages (clock skew between
    request handler and lot insert) collapse to ``"только что"`` so the UI
    never shows a misleading future timestamp.
    """
    seconds = int(age.total_seconds())
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def format_local_time(dt_utc: datetime, tz: ZoneInfo, now_local: datetime) -> str:
    """Format a UTC datetime as a local absolute time string for the header chip.

    Args:
        dt_utc: UTC-aware datetime to format (the lot's ``first_seen``).
        tz: Target timezone (built from ``Settings.timezone``).
        now_local: Current local datetime in the same ``tz`` (injected so
            the function stays pure and testable without a live clock).

    Returns:
        ``"HH:MM"`` when ``dt_utc`` falls on today's local date, otherwise
        ``"DD.MM HH:MM"``. Keeps the chip compact for the common case
        (same-day event) while disambiguating cross-day events.
    """
    local = dt_utc.astimezone(tz)
    if local.date() == now_local.date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")
