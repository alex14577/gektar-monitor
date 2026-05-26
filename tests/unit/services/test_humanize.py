"""Unit tests for ``humanize_relative_age`` and ``format_local_time`` (bd 47uh)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fis_monitor.services.humanize import format_local_time, humanize_relative_age

_MSK = ZoneInfo("Europe/Moscow")  # UTC+3 (no DST)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=-5), "только что"),  # negative (clock skew)
        (timedelta(seconds=0), "только что"),
        (timedelta(seconds=59), "только что"),
        (timedelta(seconds=60), "1 мин назад"),
        (timedelta(minutes=30), "30 мин назад"),
        (timedelta(minutes=59), "59 мин назад"),
        (timedelta(minutes=60), "1 ч назад"),
        (timedelta(hours=5), "5 ч назад"),
        (timedelta(hours=23, minutes=59), "23 ч назад"),
        (timedelta(hours=24), "1 дн назад"),
        (timedelta(days=7), "7 дн назад"),
    ],
)
def test_humanize_relative_age_buckets(age, expected) -> None:
    assert humanize_relative_age(age) == expected


# ---------------------------------------------------------------------------
# format_local_time
# ---------------------------------------------------------------------------


def test_format_local_time_same_day_shows_hhmm() -> None:
    """UTC input converts to local time; same-day event → 'HH:MM'."""
    # 14:35 UTC = 17:35 Moscow (UTC+3)
    dt_utc = datetime(2026, 5, 18, 14, 35, 0, tzinfo=UTC)
    # now is same calendar day in Moscow: 12:00 UTC = 15:00 Moscow
    now_local = datetime(2026, 5, 18, 15, 0, 0, tzinfo=_MSK)
    assert format_local_time(dt_utc, _MSK, now_local) == "17:35"


def test_format_local_time_other_day_shows_ddmm_hhmm() -> None:
    """Event from a different calendar day → 'DD.MM HH:MM'."""
    # 14:35 UTC on 2026-05-17 = 17:35 Moscow on 2026-05-17
    dt_utc = datetime(2026, 5, 17, 14, 35, 0, tzinfo=UTC)
    # now is 2026-05-18 in Moscow
    now_local = datetime(2026, 5, 18, 15, 0, 0, tzinfo=_MSK)
    assert format_local_time(dt_utc, _MSK, now_local) == "17.05 17:35"


def test_format_local_time_utc_offset_applied() -> None:
    """UTC midnight maps to 03:00 Moscow — verifies offset is applied, not stripped."""
    dt_utc = datetime(2026, 1, 10, 0, 0, 0, tzinfo=UTC)
    now_local = datetime(2026, 1, 10, 6, 0, 0, tzinfo=_MSK)
    assert format_local_time(dt_utc, _MSK, now_local) == "03:00"
