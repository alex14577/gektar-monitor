"""Unit tests for ``web.filters.format_date_ru``.

Layer 4 (Web) — pure function, no infrastructure.
Covers invariants from spec: None, date, datetime, boundary months.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from fis_monitor.web.filters import format_date_ru


@pytest.mark.parametrize("value, expected", [
    (None, "—"),
    (date(2026, 3, 17), "17 марта 2026"),
    (date(2026, 1, 1), "1 января 2026"),
    (date(2026, 12, 31), "31 декабря 2026"),
    (date(2026, 5, 7), "7 мая 2026"),
    # datetime: time must be stripped
    (datetime(2026, 3, 17, 14, 23, 0, tzinfo=UTC), "17 марта 2026"),
    (datetime(2026, 1, 1, 0, 0, 0), "1 января 2026"),
])
def test_format_date_ru(value: date | datetime | None, expected: str) -> None:
    assert format_date_ru(value) == expected


def test_all_months_covered() -> None:
    """Every month 1..12 produces a non-empty Russian genitive string."""
    from fis_monitor.web.filters import _MONTHS_GEN
    for month in range(1, 13):
        assert month in _MONTHS_GEN
        assert _MONTHS_GEN[month]  # non-empty
