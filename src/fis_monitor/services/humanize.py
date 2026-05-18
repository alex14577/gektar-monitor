"""Russian relative-time string formatter (bd 47uh).

Single source of truth for the ``"5 мин назад"`` style chips that show up
in the header-status widget and in SSE status events. Lives in the
``services`` layer so both ``services/monitor_cycle.py`` (publishes
``SseStatus``) and ``web/monitor_vm.py`` (initial render) can call it
without a layer-inversion (services → web would violate the layering).

The function is pure — no clock, no I/O — so the caller injects the
current time and the diff. This keeps unit tests trivial and avoids a
``time.now()`` global dependency.
"""

from __future__ import annotations

from datetime import timedelta


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
