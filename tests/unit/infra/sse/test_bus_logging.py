"""Logging tests for ThreadEventBus DEBUG events (gektar_monitor-b9wq).

Covers:
- sse.event.queued (DEBUG — on every publish())
- sse.queue.drop (WARNING — on normal-event queue overflow)
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import (
    SseCycleError,
    SseLotStatus,
    SseSessionExpired,
)
from fis_monitor.infra.sse.bus import ThreadEventBus

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_LOGGER = "fis_monitor.infra.sse.bus"


def _make_cycle_error() -> SseCycleError:
    return SseCycleError(timestamp=_TS, cycle_id=1, error_category="network")


def _make_session_expired() -> SseSessionExpired:
    return SseSessionExpired(timestamp=_TS)


def _make_lot_status() -> SseLotStatus:
    """Normal-priority event (priority="normal") for overflow tests."""
    return SseLotStatus(lot_id=1, new_status="gone", event_type="gone")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publish_emits_event_queued_debug(caplog: pytest.LogCaptureFixture) -> None:
    """sse.event.queued emitted with event_type + subscriber_count on every publish()."""
    bus = ThreadEventBus()
    sub = bus.subscribe()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        bus.publish(_make_session_expired())

    sub.unsubscribe()

    records = [r for r in caplog.records if r.getMessage() == "sse.event.queued"]
    assert records, "expected sse.event.queued debug event"
    rec = records[0]
    assert rec.__dict__.get("event_type") == "session.expired"
    assert "subscriber_count" in rec.__dict__
    assert rec.__dict__.get("subscriber_count") == 1


def test_publish_queued_no_subscribers(caplog: pytest.LogCaptureFixture) -> None:
    """sse.event.queued still emitted when there are no subscribers."""
    bus = ThreadEventBus()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        bus.publish(_make_session_expired())

    records = [r for r in caplog.records if r.getMessage() == "sse.event.queued"]
    assert records
    assert records[0].__dict__.get("subscriber_count") == 0


def test_publish_normal_overflow_emits_queue_drop_warning(caplog: pytest.LogCaptureFixture) -> None:
    """sse.queue.drop WARNING emitted when a normal-priority queue overflows."""
    bus = ThreadEventBus()
    sub = bus.subscribe()
    # Fill the subscriber queue (maxsize=100) with normal-priority events.
    for _ in range(100):
        try:
            sub._q.put_nowait(_make_lot_status())
        except Exception:
            break

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        bus.publish(_make_lot_status())  # should trigger drop-from-tail

    sub.unsubscribe()

    drop_records = [r for r in caplog.records if r.getMessage() == "sse.queue.drop"]
    assert drop_records, "expected sse.queue.drop warning on overflow"
    assert drop_records[0].__dict__.get("drop_reason") == "overflow"
