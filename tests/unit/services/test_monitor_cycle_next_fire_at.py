"""Unit tests for SseStatus.next_fire_at publish invariant (bd r82m).

Layer 2 (Application services) — pure unit tests with FakeClock and
FakeEventBus per docs/architecture/09-test-strategy.md.

Root-cause (bd r82m): ``_publish_status`` published ``next_cycle_mmss="{interval}:00"``
unconditionally after every cycle — a static string reset to the full interval on
every swap/reconnect, so the visible countdown appeared frozen. Fix: publish the
absolute UTC ``next_fire_at = clock.now() + timedelta(minutes=interval)`` in the
``SseStatus`` payload so the JS can compute the real remaining time on every tick.

Covered invariants:
- ``SseStatus.next_fire_at`` is ``clock.now() + interval_minutes`` after a cycle.
- ``SseStatus.next_fire_at`` is ``None`` when ``interval_minutes == 0`` (continuous mode).
- ``SseStatus.next_fire_at_iso`` renders as ``Z``-suffixed UTC ISO-8601 string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fis_monitor.domain.models import Settings, SseStatus
from tests.unit.services.test_monitor_cycle import (
    _REGION,
    FakeClock,
    FakeEnrichmentService,
    FakeListParser,
    FakeLotRepository,
    _make_lot,
    _make_parsed_row,
    _make_service,
)

_NOW = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def _status_events(published: list) -> list[SseStatus]:
    return [e for e in published if isinstance(e, SseStatus)]


class TestPublishStatusNextFireAt:
    """_publish_status publishes SseStatus with correct next_fire_at."""

    def test_next_fire_at_equals_now_plus_interval(self) -> None:
        """next_fire_at must equal clock.now() + interval_minutes."""
        from tests.unit.services.test_monitor_cycle import FakeConfigSource

        interval = 5
        clock = FakeClock(fixed=_NOW)
        config = FakeConfigSource()
        config._settings = Settings(interval_minutes=interval)

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=clock,
            config_source=config,
        )

        svc.run_cycle(_REGION)

        statuses = _status_events(bus.published)
        assert statuses, "SseStatus must be published"
        evt = statuses[0]
        expected = _NOW + timedelta(minutes=interval)
        assert evt.next_fire_at == expected, (
            f"expected next_fire_at={expected!r}, got {evt.next_fire_at!r}"
        )

    def test_next_fire_at_none_when_interval_zero(self) -> None:
        """interval_minutes=0 (continuous mode) → next_fire_at must be None."""
        from tests.unit.services.test_monitor_cycle import FakeConfigSource

        clock = FakeClock(fixed=_NOW)
        config = FakeConfigSource()
        config._settings = Settings(interval_minutes=0)

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=clock,
            config_source=config,
        )

        svc.run_cycle(_REGION)

        statuses = _status_events(bus.published)
        assert statuses, "SseStatus must be published"
        assert statuses[0].next_fire_at is None


class TestSseStatusNextFireAtIso:
    """SseStatus.next_fire_at_iso produces correct ISO-8601 UTC format."""

    def test_iso_format_has_z_suffix(self) -> None:
        """Z-suffix ensures Date.parse() in all browsers treats value as UTC."""
        ts = datetime(2026, 5, 18, 10, 30, 0, tzinfo=UTC)
        evt = SseStatus(
            timestamp=_NOW,
            state="active",
            interval_minutes=5,
            next_fire_at=ts,
        )
        assert evt.next_fire_at_iso == "2026-05-18T10:30:00Z"

    def test_iso_empty_when_none(self) -> None:
        evt = SseStatus(
            timestamp=_NOW,
            state="active",
            interval_minutes=0,
            next_fire_at=None,
        )
        assert evt.next_fire_at_iso == ""

    def test_iso_utc_no_microseconds(self) -> None:
        """Microseconds must be stripped — Date.parse edge-case in some older browsers."""
        ts = datetime(2026, 5, 18, 10, 30, 45, 123456, tzinfo=UTC)
        evt = SseStatus(
            timestamp=_NOW,
            state="active",
            interval_minutes=5,
            next_fire_at=ts,
        )
        # Must not contain microseconds or fractional seconds.
        iso = evt.next_fire_at_iso
        assert "." not in iso
        assert iso.endswith("Z")
