"""Layer 1 unit tests for ``domain.subscription_cutoff.passes_subscription_cutoff``.

Tests the pure predicate per ADR-039 day-precision rule:
- same-day → pass (True); historical (day before) → suppress (False)
- day after subscribed_at → pass
- subscribed_at None → pass (fail-open, for SQL LEFT-JOIN equivalence)
- region_id None → pass (fail-open, ADR-035 I2: unrecognised subject)

Note: the email-channel stricter path (suppress when region known but no subscription
record) lives in ``SubscribedAtFilteredNotifier.should_suppress``, which short-circuits
before calling this predicate. See ``test_notifier_dispatcher.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.subscription_cutoff import passes_subscription_cutoff

# Base reference day: 2026-05-15 00:00 UTC (midnight — day-precision style)
_DAY = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)
_DAY_BEFORE = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
_DAY_AFTER = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)

# subscribed_at is a wallclock moment (non-midnight), same calendar day as _DAY
_SUBSCRIBED_SAME_DAY = datetime(2026, 5, 15, 10, 30, 0, tzinfo=UTC)
_SUBSCRIBED_YESTERDAY = datetime(2026, 5, 14, 9, 0, 0, tzinfo=UTC)


def test_same_day_lot_passes() -> None:
    """date_create.date() == subscribed_at.date() → passes.

    gn89 amendment: same-day is not suppressed.
    """
    result = passes_subscription_cutoff(_DAY, _SUBSCRIBED_SAME_DAY, region_id=1)
    assert result is True


def test_historical_lot_suppressed() -> None:
    """date_create day strictly before subscribed_at day → suppressed."""
    result = passes_subscription_cutoff(_DAY_BEFORE, _SUBSCRIBED_SAME_DAY, region_id=1)
    assert result is False


def test_day_after_subscription_passes() -> None:
    """date_create day strictly after subscribed_at day → passes."""
    result = passes_subscription_cutoff(_DAY_AFTER, _SUBSCRIBED_SAME_DAY, region_id=1)
    assert result is True


def test_subscribed_at_none_passes() -> None:
    """No subscription record supplied → fail-open (always True).

    This predicate is fail-open for subscribed_at=None to mirror the SQL
    LEFT-JOIN expression (``WHERE rs.subscribed_at IS NULL OR ...``).
    The email-channel strictness (suppress when region known, no record)
    is enforced in ``SubscribedAtFilteredNotifier.should_suppress``.
    """
    result = passes_subscription_cutoff(_DAY_BEFORE, None, region_id=1)
    assert result is True


def test_region_id_none_passes() -> None:
    """Lot has no region → no subscription to consult → fail-open (always True)."""
    result = passes_subscription_cutoff(_DAY_BEFORE, _SUBSCRIBED_SAME_DAY, region_id=None)
    assert result is True
