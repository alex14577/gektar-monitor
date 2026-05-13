"""Integration tests for BrowserSseNotifier.

Uses Fake collaborators to test EventBus integration without real threading.

Fake collaborators:
* ``FakeEventBus`` — in-memory queue, tracks published events.

All tests verify:
1. send() publishes SseLotNew with correct parameters.
2. EventBus exceptions are caught; NotifyResult.ok=True, detail="dropped (bus overflow)".
3. test() returns no-op; does NOT publish anything.
4. All ClassVars are complete.
5. Protocol compliance (runtime_checkable Notifier).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_core import ValidationError

from fis_monitor.domain.interfaces import Notifier
from fis_monitor.domain.models import (
    LotPublicDTO,
    SseLotNew,
)
from fis_monitor.infra.sse.browser_sse_notifier import (
    BrowserNotifierConfig,
    BrowserSseNotifier,
)

# ---------------------------------------------------------------------------
# Shared constants and factories
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_lot(**overrides) -> LotPublicDTO:
    """Factory for LotPublicDTO test data."""
    defaults = {
        "id": 42,
        "cadastral_no": "01:02:000000:1",
        "area_sqm": None,
        "region": "TestRegion",
        "municipality": None,
        "land_category": None,
        "permitted_use": None,
        "ogv": None,
        "status": "active",
        "date_create": _TS,
        "date_update": None,
        "lat": None,
        "lon": None,
        "has_boundaries": None,
        "raw_json": {},
        "first_seen": _TS,
        "last_seen": _TS,
        "detail_fetched_at": None,
        "enrichment_status": None,
        "last_seen_at": None,
        "age_seconds": 0,
        "tier": "match",
        "freshness": "hot",
    }
    defaults.update(overrides)
    return LotPublicDTO(**defaults)


# ---------------------------------------------------------------------------
# Fake EventBus for testing
# ---------------------------------------------------------------------------


class FakeEventBus:
    """Minimal EventBus for tests — tracks published events."""

    def __init__(self, raise_on_publish: Exception | None = None) -> None:
        self.published_events: list[SseLotNew] = []
        self._raise_on_publish = raise_on_publish

    def publish(self, event) -> None:
        """Simulate publish — optionally raise to test exception handling."""
        if self._raise_on_publish is not None:
            raise self._raise_on_publish
        # Accept only SseLotNew for this test
        if isinstance(event, SseLotNew):
            self.published_events.append(event)
        else:
            raise TypeError(f"Unexpected event type: {type(event)}")

    def subscribe(self):
        """Unused stub."""
        raise NotImplementedError("FakeEventBus doesn't implement subscribe")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrowserSseNotifierSend:
    """Tests for send() — publishes SseLotNew to EventBus."""

    def test_send_publishes_lot_new(self):
        """send() publishes a SseLotNew event with correct parameters."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot(id=99)
        recipient = "tab@browser"

        result = notifier.send(lot, recipient)

        # Verify result
        assert result.ok is True
        assert result.detail == "published"
        assert result.retryable is False

        # Verify bus received exactly one event
        assert len(bus.published_events) == 1
        event = bus.published_events[0]
        assert isinstance(event, SseLotNew)
        assert event.lot.id == 99
        assert event.fragment_template == "poster"

    def test_send_ignores_recipient(self):
        """send() ignores the recipient parameter (broadcast to all tabs)."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot()

        # Same lot, different recipients → same event
        notifier.send(lot, "tab-1")
        notifier.send(lot, "tab-2")

        # Both calls publish identical events
        assert len(bus.published_events) == 2
        assert bus.published_events[0].lot.id == bus.published_events[1].lot.id

    def test_send_catches_publish_exception(self):
        """send() catches EventBus.publish exceptions and returns graceful no-op."""
        bus = FakeEventBus(raise_on_publish=OverflowError("queue full"))
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot()

        result = notifier.send(lot, "recipient")

        # Verify result indicates safe failure
        assert result.ok is True
        assert result.detail == "dropped (bus overflow)"
        assert result.retryable is False

    def test_send_catches_generic_exception(self):
        """send() handles any Exception, not just queue.Full."""
        bus = FakeEventBus(raise_on_publish=RuntimeError("some bug"))
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot()

        result = notifier.send(lot, "recipient")

        # Still returns safe no-op
        assert result.ok is True
        assert result.detail == "dropped (bus overflow)"
        assert result.retryable is False


class TestBrowserSseNotifierTest:
    """Tests for test() — no-op, does NOT publish."""

    def test_test_returns_ok_no_publish(self):
        """test() returns success without publishing anything."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        result = notifier.test("recipient@example.com")

        # Verify result
        assert result.ok is True
        assert result.detail == "browser channel is push-only; no test send required"
        assert result.retryable is False

        # Verify nothing published
        assert len(bus.published_events) == 0

    def test_test_ignores_recipient(self):
        """test() does not use the recipient parameter."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        result1 = notifier.test("alice@example.com")
        result2 = notifier.test("bob@example.com")

        # Both return identical results
        assert result1 == result2
        # And publish nothing
        assert len(bus.published_events) == 0


class TestBrowserSseNotifierClassVars:
    """Tests for ClassVar metadata."""

    def test_classvars_complete(self):
        """All required ClassVars are set and have correct types."""
        assert BrowserSseNotifier.channel_id == "browser"
        assert isinstance(BrowserSseNotifier.display_name, str)
        assert len(BrowserSseNotifier.display_name) > 0
        assert isinstance(BrowserSseNotifier.description, str)
        assert len(BrowserSseNotifier.description) > 0
        assert BrowserSseNotifier.config_schema is BrowserNotifierConfig
        assert isinstance(BrowserSseNotifier.recipient_label, str)
        assert len(BrowserSseNotifier.recipient_label) > 0
        assert isinstance(BrowserSseNotifier.recipient_placeholder, str)
        assert len(BrowserSseNotifier.recipient_placeholder) > 0

    def test_config_schema_is_notifier_config(self):
        """config_schema is a NotifierConfig subclass."""
        from fis_monitor.domain.models import NotifierConfig

        assert issubclass(BrowserSseNotifier.config_schema, NotifierConfig)


class TestBrowserSseNotifierProtocol:
    """Tests for Protocol compliance."""

    def test_isinstance_notifier_protocol(self):
        """BrowserSseNotifier is recognized as a Notifier by isinstance."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        # runtime_checkable allows isinstance checks on Protocols
        assert isinstance(notifier, Notifier)

    def test_protocol_methods_present(self):
        """BrowserSseNotifier has required Notifier methods."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        # Must have both send and test
        assert callable(notifier.send)
        assert callable(notifier.test)

    def test_method_signatures(self):
        """send() and test() have correct parameter signatures."""
        import inspect

        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        # send(lot: LotPublicDTO, recipient: str) -> NotifyResult
        send_sig = inspect.signature(notifier.send)
        assert "lot" in send_sig.parameters
        assert "recipient" in send_sig.parameters

        # test(recipient: str) -> NotifyResult
        test_sig = inspect.signature(notifier.test)
        assert "recipient" in test_sig.parameters


class TestBrowserNotifierConfig:
    """Tests for BrowserNotifierConfig pydantic model."""

    def test_config_defaults(self):
        """BrowserNotifierConfig has correct defaults."""
        config = BrowserNotifierConfig()
        assert config.enabled is True

    def test_config_can_disable(self):
        """BrowserNotifierConfig.enabled can be set to False."""
        config = BrowserNotifierConfig(enabled=False)
        assert config.enabled is False

    def test_config_inherits_from_notifier_config(self):
        """BrowserNotifierConfig is a NotifierConfig subclass."""
        from fis_monitor.domain.models import NotifierConfig

        assert issubclass(BrowserNotifierConfig, NotifierConfig)

    def test_config_is_frozen(self):
        """NotifierConfig's frozen policy applies (no post-init edits)."""
        config = BrowserNotifierConfig(enabled=True)

        # Attempt to modify should raise (pydantic frozen)
        with pytest.raises(ValidationError):
            config.enabled = False


class TestBrowserSseNotifierIntegration:
    """Integration tests with real ThreadEventBus."""

    def test_integration_with_thread_event_bus(self):
        """send() works with real ThreadEventBus; test() publishes nothing."""
        from fis_monitor.infra.sse.bus import ThreadEventBus

        bus = ThreadEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot(id=123)

        # Subscribe to capture events
        with bus.subscribe() as sub:
            result = notifier.send(lot, "tab-1")

            # Result indicates success
            assert result.ok is True
            assert result.detail == "published"

            # Read event from subscription iterator
            events = list(sub.iter())
            assert len(events) == 1
            event = events[0]
            assert isinstance(event, SseLotNew)
            assert event.lot.id == 123

    def test_integration_graceful_when_no_subscribers(self):
        """send() succeeds even if no subscribers are connected."""
        from fis_monitor.infra.sse.bus import ThreadEventBus

        bus = ThreadEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot()

        # Publish without any subscribers
        result = notifier.send(lot, "recipient")

        # Still returns success (events dropped silently per ADR-008)
        assert result.ok is True
        assert result.detail == "published"
        assert result.retryable is False

    def test_integration_test_with_real_bus(self):
        """test() is a no-op even with real ThreadEventBus."""
        from fis_monitor.infra.sse.bus import ThreadEventBus

        bus = ThreadEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        # Subscribe to verify nothing is published
        with bus.subscribe() as sub:
            result = notifier.test("recipient")

            assert result.ok is True
            assert result.detail == "browser channel is push-only; no test send required"

            # No events published
            events = list(sub.iter())
            assert len(events) == 0


class TestBrowserSseNotifierEdgeCases:
    """Edge cases and corner scenarios."""

    def test_send_preserves_lot_details(self):
        """SseLotNew event preserves all lot details."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)
        lot = _make_lot(
            id=999,
            region="Special Region",
            status="complex",
            tier="silent",
            freshness="cold",
        )

        notifier.send(lot, "recipient")

        event = bus.published_events[0]
        assert event.lot.id == 999
        assert event.lot.region == "Special Region"
        assert event.lot.status == "complex"
        assert event.lot.tier == "silent"
        assert event.lot.freshness == "cold"

    def test_send_fragment_template_is_poster(self):
        """send() always uses fragment_template='poster'."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        notifier.send(_make_lot(), "recipient")

        event = bus.published_events[0]
        assert event.fragment_template == "poster"

    def test_multiple_sends_are_independent(self):
        """Multiple send() calls each publish independently."""
        bus = FakeEventBus()
        notifier = BrowserSseNotifier(event_bus=bus)

        notifier.send(_make_lot(id=1), "recipient")
        notifier.send(_make_lot(id=2), "recipient")
        notifier.send(_make_lot(id=3), "recipient")

        assert len(bus.published_events) == 3
        assert [e.lot.id for e in bus.published_events] == [1, 2, 3]
