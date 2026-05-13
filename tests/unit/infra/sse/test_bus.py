"""Unit tests for ThreadEventBus — TDD RED phase first, then GREEN.

Covers:
  - Protocol structural compliance
  - Normal / critical event delivery
  - Per-type last_critical slots
  - Drop-from-tail for normal, force-unsubscribe for critical slow consumers
  - Context-manager subscription lifecycle
  - Thread safety (basic)
  - ADR-008 no-db-persistence invariant
"""
from __future__ import annotations

import inspect
import threading
from datetime import UTC, datetime

from fis_monitor.domain.models import (
    SseCycleError,
    SseLotNew,
    SseLotStatus,
    SseSessionExpired,
    SseSmtpFailed,
)
from fis_monitor.infra.sse.bus import ThreadEventBus

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_cycle_error(cycle_id: int = 1) -> SseCycleError:
    return SseCycleError(
        timestamp=_TS,
        cycle_id=cycle_id,
        error_category="network",
    )


def make_smtp_failed(attempt_no: int = 1) -> SseSmtpFailed:
    return SseSmtpFailed(
        timestamp=_TS,
        channel_id="ch-1",
        attempt_no=attempt_no,
        error_category="timeout",
    )


def make_session_expired() -> SseSessionExpired:
    return SseSessionExpired(timestamp=_TS)


def make_lot_new(lot_id: int = 42) -> SseLotNew:
    from fis_monitor.domain.models import Lot, LotPublicDTO

    lot = Lot(
        id=lot_id,
        cadastral_no="01:02:000000:1",
        area_sqm=None,
        region="TestRegion",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=_TS,
        date_update=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        first_seen=_TS,
        last_seen=_TS,
        detail_fetched_at=None,
        enrichment_status=None,
        last_seen_at=None,
    )
    lot_dto = LotPublicDTO(
        **lot.model_dump(),
        age_seconds=0,
        tier="match",
        freshness="hot",
    )
    return SseLotNew(lot=lot_dto, fragment_template="poster")


def make_lot_status(lot_id: int = 99) -> SseLotStatus:
    return SseLotStatus(lot_id=lot_id, new_status="gone", event_type="gone")


# ---------------------------------------------------------------------------
# 1. Protocol structural compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    def test_bus_implements_protocol(self):
        bus = ThreadEventBus()
        # Presence + callable checks (defence in depth)
        assert hasattr(bus, "publish"), "Missing publish method"
        assert hasattr(bus, "subscribe"), "Missing subscribe method"
        assert callable(bus.publish)
        assert callable(bus.subscribe)

        # Signature checks: guard against accidental Protocol drift.
        # bus.publish / bus.subscribe are bound methods — 'self' is absent.
        publish_params = list(inspect.signature(bus.publish).parameters)
        assert publish_params == ["event"], (
            f"publish() signature drift: expected ['event'], got {publish_params}"
        )

        subscribe_params = list(inspect.signature(bus.subscribe).parameters)
        assert subscribe_params == [], (
            f"subscribe() signature drift: expected [], got {subscribe_params}"
        )


# ---------------------------------------------------------------------------
# 2. Normal event delivery
# ---------------------------------------------------------------------------

class TestNormalDelivery:
    def test_publish_normal_delivers_to_subscriber(self):
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            event = make_lot_new()
            bus.publish(event)
            events = list(sub.iter())
        assert len(events) == 1
        assert events[0] == event

    def test_publish_normal_does_not_persist_in_critical_slot(self):
        bus = ThreadEventBus()
        event = make_lot_new()
        bus.publish(event)
        assert bus.last_critical(SseLotNew) is None

    def test_publish_lot_status_delivers_to_subscriber(self):
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            event = make_lot_status()
            bus.publish(event)
            events = list(sub.iter())
        assert events == [event]


# ---------------------------------------------------------------------------
# 3. Critical event delivery
# ---------------------------------------------------------------------------

class TestCriticalDelivery:
    def test_publish_critical_delivers_to_subscriber(self):
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            event = make_cycle_error()
            bus.publish(event)
            events = list(sub.iter())
        assert events == [event]

    def test_publish_critical_updates_last_critical_slot(self):
        bus = ThreadEventBus()
        event = make_cycle_error()
        bus.publish(event)
        assert bus.last_critical(SseCycleError) == event

    def test_per_type_slots_independent(self):
        bus = ThreadEventBus()
        cycle_evt = make_cycle_error()
        smtp_evt = make_smtp_failed()
        bus.publish(cycle_evt)
        bus.publish(smtp_evt)
        assert bus.last_critical(SseCycleError) == cycle_evt
        assert bus.last_critical(SseSmtpFailed) == smtp_evt

    def test_per_type_slot_overwrites_same_type(self):
        bus = ThreadEventBus()
        first = make_cycle_error(cycle_id=1)
        second = make_cycle_error(cycle_id=2)
        bus.publish(first)
        bus.publish(second)
        assert bus.last_critical(SseCycleError) == second

    def test_session_expired_slot(self):
        bus = ThreadEventBus()
        event = make_session_expired()
        bus.publish(event)
        assert bus.last_critical(SseSessionExpired) == event

    def test_normal_does_not_persist_in_critical_slot(self):
        bus = ThreadEventBus()
        bus.publish(make_lot_new())
        bus.publish(make_lot_status())
        assert bus.last_critical(SseLotNew) is None
        assert bus.last_critical(SseLotStatus) is None


# ---------------------------------------------------------------------------
# 4. Subscription context-manager / lifecycle
# ---------------------------------------------------------------------------

class TestSubscriptionLifecycle:
    def test_subscribe_returns_context_manager(self):
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            assert hasattr(sub, "iter")
            assert hasattr(sub, "unsubscribe")
            bus.publish(make_lot_new())
            events = list(sub.iter())
            assert len(events) == 1
        # after __exit__, subscriber is removed — further publishes don't raise
        bus.publish(make_lot_new())

    def test_unsubscribe_is_idempotent(self):
        bus = ThreadEventBus()
        sub = bus.subscribe()
        sub.unsubscribe()
        sub.unsubscribe()  # must not raise

    def test_context_manager_exit_unsubscribes(self):
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            pass
        # After exit, iter should return empty (no further deliveries)
        bus.publish(make_lot_new())
        assert list(sub.iter()) == []


# ---------------------------------------------------------------------------
# 5. Back-pressure: normal drop-from-tail
# ---------------------------------------------------------------------------

class TestNormalDropFromTail:
    def test_normal_drops_from_tail_when_queue_full(self):
        """Fill subscriber queue to maxsize=100, then publish one more.

        The new event should be at the tail; the oldest should be dropped.
        """
        bus = ThreadEventBus()
        with bus.subscribe() as sub:
            # Fill queue to capacity
            for i in range(100):
                bus.publish(make_lot_status(lot_id=i))

            # Publish one more (the 101st)
            newest = make_lot_status(lot_id=999)
            bus.publish(newest)

            events = list(sub.iter())

        assert len(events) == 100
        # The newest event should be the last in the queue
        assert events[-1] == newest
        # The very first event (lot_id=0) should have been dropped
        lot_ids = [e.lot_id for e in events]
        assert 0 not in lot_ids
        assert 999 in lot_ids


# ---------------------------------------------------------------------------
# 6. Back-pressure: critical force-unsubscribe slow consumer
# ---------------------------------------------------------------------------

class TestCriticalForceUnsubscribe:
    def test_critical_force_unsubscribes_slow_consumer(self):
        """A subscriber that never reads gets force-unsubscribed after queue full + timeout."""
        bus = ThreadEventBus(critical_timeout=0.05)  # short timeout for test speed
        slow_sub = bus.subscribe()
        # Don't read from slow_sub — let it fill up

        # Fill the queue beyond capacity with critical events; the bus should
        # eventually force-unsubscribe the slow consumer.
        # We publish enough to fill the queue and trigger the timeout path.
        for i in range(110):
            bus.publish(make_cycle_error(cycle_id=i))

        # After force-unsubscribe, further publishes must not raise
        bus.publish(make_cycle_error(cycle_id=999))

        # The slow subscriber should have been removed (unsubscribed)
        # The subscription's internal _alive flag is False
        assert not slow_sub._alive  # implementation detail — acceptable for unit test


# ---------------------------------------------------------------------------
# 7. Multiple subscribers fan-out
# ---------------------------------------------------------------------------

class TestMultipleSubscribers:
    def test_multiple_subscribers_receive_same_event(self):
        bus = ThreadEventBus()
        with bus.subscribe() as s1, bus.subscribe() as s2, bus.subscribe() as s3:
            event = make_lot_new()
            bus.publish(event)
            e1 = list(s1.iter())
            e2 = list(s2.iter())
            e3 = list(s3.iter())
        assert e1 == [event]
        assert e2 == [event]
        assert e3 == [event]


# ---------------------------------------------------------------------------
# 8. Thread-safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_publish_thread_safety_basic(self):
        """4 publisher threads x 100 normal events; 1 consumer.

        For normal events (best-effort) the delivery count may be < total
        due to drop-from-tail, but there should be no crashes, deadlocks,
        or data corruption.
        """
        bus = ThreadEventBus()
        received: list = []
        errors: list[Exception] = []

        with bus.subscribe() as sub:
            barrier = threading.Barrier(4)

            def publisher():
                barrier.wait()
                for i in range(100):
                    try:
                        bus.publish(make_lot_status(lot_id=i))
                    except Exception as exc:
                        errors.append(exc)

            threads = [threading.Thread(target=publisher) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            received.extend(sub.iter())

        assert not errors, f"Publisher threads raised: {errors}"
        # All received events must be valid SseLotStatus instances
        for evt in received:
            assert isinstance(evt, SseLotStatus)

    def test_critical_slot_thread_safety(self):
        """Multiple threads publishing critical events; last_critical must not corrupt."""
        bus = ThreadEventBus()
        errors: list[Exception] = []

        def publisher(cycle_id: int):
            for _ in range(20):
                try:
                    bus.publish(make_cycle_error(cycle_id=cycle_id))
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=publisher, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        # last_critical must return a valid SseCycleError (no corruption)
        result = bus.last_critical(SseCycleError)
        assert result is None or isinstance(result, SseCycleError)


# ---------------------------------------------------------------------------
# 9. ADR-008 invariant — no DB persistence
# ---------------------------------------------------------------------------

class TestNoPersistence:
    def test_no_db_persistence(self):
        """ADR-008: ThreadEventBus must NOT import sqlite3 or connection infra."""
        import ast
        from pathlib import Path

        bus_path = Path(__file__).parents[4] / "src" / "fis_monitor" / "infra" / "sse" / "bus.py"
        source = bus_path.read_text()
        tree = ast.parse(source)

        forbidden = {"sqlite3", "connection", "state.db", "ThreadLocalConnectionProvider"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    for bad in forbidden:
                        assert bad not in name, (
                            f"ADR-008 violation: bus.py imports '{name}' "
                            f"which contains forbidden term '{bad}'"
                        )
