"""Unit tests for SseCycleStarted publish invariant (hiq3, ADR-050).

Layer 2 (Application services) — pure unit tests with FakeClock and
FakeEventBus per docs/architecture/09-test-strategy.md.

Context: Replaces the countdown (bd r82m, ADR-048) tests now that ADR-050
supersedes ADR-048. The countdown fields (next_fire_at, next_cycle_mmss)
are removed from SseStatus. Replaced by SseCycleStarted / SseCycleDone
binary signals (UI pulse-dot consumer was further removed in lw5s; the
events stay on the stream for telemetry and future consumers).

Covered invariants:
- ``SseCycleStarted`` is published at the start of ``run_cycle``.
- It carries ``cycle_id`` matching the opened cycle.
- It is published before any ``SseCycleDone`` event.
- On error paths (upstream error), ``SseCycleStarted`` is still published.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.errors import UpstreamError
from fis_monitor.domain.models import SseCycleDone, SseCycleStarted
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


def _started_events(published: list) -> list[SseCycleStarted]:
    return [e for e in published if isinstance(e, SseCycleStarted)]


def _done_events(published: list) -> list[SseCycleDone]:
    return [e for e in published if isinstance(e, SseCycleDone)]


class TestPublishCycleStarted:
    """run_cycle publishes SseCycleStarted before SseCycleDone."""

    def test_cycle_started_published_on_happy_path(self) -> None:
        """SseCycleStarted must be published on every successful cycle."""
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=FakeClock(fixed=_NOW),
        )

        svc.run_cycle(_REGION)

        started = _started_events(bus.published)
        assert started, "SseCycleStarted must be published on happy path"
        assert started[0].timestamp == _NOW

    def test_cycle_started_before_done(self) -> None:
        """SseCycleStarted must be published before any SseCycleDone."""
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=FakeClock(fixed=_NOW),
        )

        svc.run_cycle(_REGION)

        started = _started_events(bus.published)
        done = _done_events(bus.published)
        assert started, "SseCycleStarted must be in published events"
        assert done, "SseCycleDone must be in published events"

        # Both events have the same timestamp from FakeClock — so check
        # positional ordering in bus.published list instead
        first_started_idx = bus.published.index(started[0])
        first_done_idx = bus.published.index(done[0])
        assert first_started_idx < first_done_idx, (
            "SseCycleStarted must appear before SseCycleDone in the event stream"
        )

    def test_cycle_started_has_cycle_id(self) -> None:
        """SseCycleStarted.cycle_id must be a non-zero integer (assigned by cycles repo)."""
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=FakeClock(fixed=_NOW),
        )

        svc.run_cycle(_REGION)

        started = _started_events(bus.published)
        assert started[0].cycle_id > 0


class TestSseCycleStartedOnErrorPath:
    """On error paths, SseCycleStarted is still published before the cycle terminates."""

    def test_cycle_started_and_done_published_on_parser_upstream_error(self) -> None:
        """SseCycleStarted AND SseCycleDone are published when parse() raises UpstreamError.

        Invariant (y38m): every cycle.started MUST be paired with a cycle.done.
        Even if list_parser.parse raises UpstreamError (defensive path — current
        parser raises only ParseBugError/SessionExpiredError) — run_cycle must
        gracefully close the cycle with cycle.done, not re-raise.
        """
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(raises=UpstreamError("timeout", category="timeout")),
            clock=FakeClock(fixed=_NOW),
        )

        # Defensive catch — must NOT propagate UpstreamError from parse step.
        svc.run_cycle(_REGION)

        started = _started_events(bus.published)
        done = _done_events(bus.published)
        assert started, (
            "SseCycleStarted must be published on upstream error path so the "
            "cycle boundary is visible to telemetry / SSE consumers"
        )
        assert done, (
            "SseCycleDone must be published to close the cycle.started → "
            "cycle.done pair — bug y38m regression guard"
        )
