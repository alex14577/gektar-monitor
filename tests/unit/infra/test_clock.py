"""Unit tests for SystemClock."""
from __future__ import annotations

from datetime import datetime

from fis_monitor.infra.clock import SystemClock


def test_now_returns_aware_utc_datetime() -> None:
    """SystemClock.now() must return an aware datetime in UTC."""
    clock = SystemClock()
    result = clock.now()
    assert isinstance(result, datetime), "Expected a datetime instance"
    assert result.tzinfo is not None, "datetime must be timezone-aware"
    # Normalise to UTC offset for comparison (handles both UTC and timezone.utc aliases).
    assert result.utcoffset().total_seconds() == 0, "datetime must be in UTC (offset 0)"


def test_monotonic_is_increasing() -> None:
    """SystemClock.monotonic() must return an increasing float."""
    clock = SystemClock()
    t1 = clock.monotonic()
    t2 = clock.monotonic()
    assert isinstance(t1, float), "Expected float from monotonic()"
    assert t2 >= t1, "Second call must be >= first call (monotonically non-decreasing)"
