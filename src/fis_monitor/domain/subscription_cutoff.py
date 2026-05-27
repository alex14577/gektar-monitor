"""Shared predicate for the subscribed_at region cutoff (ADR-039, day-precision rule).

This module is the **single source of truth** for the lot-vs-subscription
calendar comparison.  It is imported by:

- ``services.notifier_dispatcher.SubscribedAtFilteredNotifier.should_suppress``
  (email-channel pre-reserve suppression hook).
- ``services.lot_query._build_query`` mirrors this rule at SQL level via
  ``LEFT JOIN region_subscriptions ... WHERE date(date_create) >= date(subscribed_at)``
  when ``LotFilters.apply_subscription_cutoff`` is True.

The SQL expression is kept in sync with the Python predicate by the Layer-3
equivalence test in ``tests/integration/services/test_lot_query_cutoff.py``.

ADR-039 day-precision rationale
--------------------------------
``date_create`` is parsed from upstream "DD.MM.YYYY" and is always a UTC
midnight value (``datetime(Y, M, D, 0, 0, 0, tzinfo=UTC)``).  ``subscribed_at``
is the wallclock moment of subscription (``Clock.now()``).  Full-timestamp
comparison would suppress every same-day lot because ``00:00:00 < HH:MM:SS``
is always true within a day.  Calendar-date comparison prevents this false
positive while still blocking historical lots (the backfill-spam use case).

See ``docs/decisions/ADR-039-subscribed-at-region-cutoff.md``.
"""

from __future__ import annotations

from datetime import datetime


def passes_subscription_cutoff(
    date_create: datetime,
    subscribed_at: datetime | None,
    *,
    region_id: int | None = None,
) -> bool:
    """Return True when a lot should appear / be delivered given the subscription cutoff.

    Suppression rule (ADR-039, day-precision, amendment gn89):
    - A lot is **suppressed** (returns False) when its calendar publication date
      strictly precedes the subscription calendar date:
      ``date_create.date() < subscribed_at.date()``.
    - Same-day lots pass (``>=``).

    Fail-open cases — always returns True:
    - ``region_id is None``: lot has no region; no subscription to consult.
    - ``subscribed_at is None``: no subscription record for this region.

    Args:
        date_create:   Lot publication datetime (tz-aware UTC, day precision).
        subscribed_at: Region subscription moment (tz-aware UTC), or None.
        region_id:     Region identifier, or None for region-less lots.

    Returns:
        True  — lot passes the cutoff (show / deliver).
        False — lot is older than the subscription date (suppress).
    """
    if region_id is None or subscribed_at is None:
        return True
    return date_create.date() >= subscribed_at.date()
