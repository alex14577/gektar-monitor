"""Jinja2 custom filters for the web layer.

All functions are pure (no side effects, no globals mutation).
No locale.setlocale — month names are embedded as a static dict.
No babel — eliminates the ~10MB CLDR bundle (ADR-026 bundle size budget).
"""
from __future__ import annotations

from datetime import date, datetime

# Russian month names in genitive case (родительный падеж).
# E.g. "17 марта 2026" — the correct form for dates in Russian prose.
_MONTHS_GEN: dict[int, str] = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_date_ru(value: date | datetime | None) -> str:
    """Format a date as «D месяца YYYY» (Russian genitive month).

    Examples:
        format_date_ru(date(2026, 3, 17)) → "17 марта 2026"
        format_date_ru(datetime(2026, 3, 17, 14, 0)) → "17 марта 2026"  # time stripped
        format_date_ru(None) → "—"

    Args:
        value: A ``date``, ``datetime``, or ``None``.

    Returns:
        Formatted string "D месяца YYYY" or "—" for None/invalid input.
    """
    if value is None:
        return "—"
    d: date = value.date() if isinstance(value, datetime) else value
    return f"{d.day} {_MONTHS_GEN[d.month]} {d.year}"
