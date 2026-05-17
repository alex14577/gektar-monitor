"""Unit tests for MonitorCycleService.run_forever.

Covers the scheduled-loop behaviour: stop_event handling, per-region dispatch,
UpstreamError absorption, and unexpected-exception backoff.

All external dependencies are replaced with fully-callable fakes per the
project's fake-impl invariant (CLAUDE.md §6).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.errors import ParseBugError, ParserVersionMismatch, UpstreamError
from fis_monitor.domain.models import (
    CycleResult,
    Settings,
)
from fis_monitor.services.monitor_cycle import MonitorCycleService
from tests.fakes.lot_repository import FakeLotRepository
from tests.unit.services.conftest import (
    MinimalClock,
    MinimalConfigSource,
    MinimalCyclesRepository,
    MinimalEnrichmentService,
    MinimalEventBus,
    MinimalHttpClient,
    MinimalListParser,
    MinimalNotifierDispatcher,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION_A = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Spy — wraps run_cycle to record calls and optionally raise
# ---------------------------------------------------------------------------


class SpyMonitorCycleService(MonitorCycleService):
    """Subclass that replaces run_cycle with a spy.

    ``run_cycle_calls`` records each (region,) tuple.
    ``run_cycle_raises`` is an optional exception raised on every call.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_cycle_calls: list[int] = []
        self._run_cycle_raises: Exception | None = None

    def configure_raises(self, exc: Exception) -> None:
        self._run_cycle_raises = exc

    def run_cycle(self, region: int) -> CycleResult:  # type: ignore[override]
        self.run_cycle_calls.append(region)
        if self._run_cycle_raises is not None:
            raise self._run_cycle_raises
        # Return a minimal successful CycleResult without touching the real pipeline.
        return CycleResult(
            id=len(self.run_cycle_calls),
            region=region,
            started_at=_NOW,
            finished_at=_NOW,
            status="ok",
            lots_fetched=0,
            new_lots=0,
            error=None,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service(
    *,
    settings: Settings | None = None,
    run_cycle_raises: Exception | None = None,
) -> SpyMonitorCycleService:
    """Build a SpyMonitorCycleService with sensible fakes."""
    svc = SpyMonitorCycleService(
        http=MinimalHttpClient(),
        list_parser=MinimalListParser(),
        enrichment=MinimalEnrichmentService(),
        lot_repo=FakeLotRepository(),
        cycles_repo=MinimalCyclesRepository(),
        notifier_dispatcher=MinimalNotifierDispatcher(),
        event_bus=MinimalEventBus(),
        config_source=MinimalConfigSource(settings=settings or Settings()),
        clock=MinimalClock(),
        cycle_progress_signal=threading.Event(),
    )
    if run_cycle_raises is not None:
        svc.configure_raises(run_cycle_raises)
    return svc


# ---------------------------------------------------------------------------
# Test 1: stop_event set before call → exits immediately, no run_cycle calls
# ---------------------------------------------------------------------------


class TestRunForeverExitsImmediately:
    """run_forever exits without calling run_cycle when stop_event is pre-set."""

    def test_exits_when_stop_event_pre_set(self) -> None:
        svc = _make_service(settings=Settings(regions=[_REGION_A, _REGION_B]))
        stop_event = threading.Event()
        stop_event.set()  # pre-set before call

        svc.run_forever(stop_event)  # must return without blocking

        assert svc.run_cycle_calls == [], (
            f"Expected no run_cycle calls, got {svc.run_cycle_calls}"
        )


# ---------------------------------------------------------------------------
# Test 2: one pass — run_cycle called once per region
# ---------------------------------------------------------------------------


class TestRunForeverOnePassPerRegion:
    """run_forever calls run_cycle once per region in a single pass, then sleeps."""

    def test_run_cycle_called_for_each_region(self) -> None:
        regions = [_REGION_A, _REGION_B]
        settings = Settings(regions=regions, interval_minutes=15)
        svc = _make_service(settings=settings)

        stop_event = threading.Event()

        # After the first full pass completes, stop_event.wait(poll_interval)
        # is called with ~900 s.  We intercept by setting stop_event after the
        # first pass: we do this via a threading.Timer that fires quickly.
        # But since we can't predict exact timing, the cleanest approach is to
        # run in a thread, let the first pass complete (run_cycle * 2 calls),
        # then set stop_event.

        # Use a barrier: once run_cycle has been called for all regions, set stop.
        expected_calls = len(regions)
        barrier_event = threading.Event()
        original_run_cycle = svc.run_cycle

        call_count = 0

        def patched_run_cycle(region: int) -> CycleResult:
            nonlocal call_count
            result = original_run_cycle(region)
            call_count += 1
            if call_count >= expected_calls:
                barrier_event.set()
            return result

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        def runner() -> None:
            svc.run_forever(stop_event)

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        # Wait until both regions have been processed, then stop.
        assert barrier_event.wait(timeout=5.0), "run_cycle was not called for all regions in time"
        stop_event.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever did not exit after stop_event was set"

        assert svc.run_cycle_calls == regions, (
            f"Expected run_cycle calls {regions}, got {svc.run_cycle_calls}"
        )


# ---------------------------------------------------------------------------
# Test 3: stop_event honoured mid-pass
# ---------------------------------------------------------------------------


class TestRunForeverStopEventMidPass:
    """stop_event set after first pass → no second pass executes."""

    def test_no_second_pass_after_stop(self) -> None:
        regions = [_REGION_A, _REGION_B]
        settings = Settings(regions=regions, interval_minutes=15)
        svc = _make_service(settings=settings)

        stop_event = threading.Event()
        first_pass_done = threading.Event()

        original_run_cycle = svc.run_cycle
        calls: list[int] = []

        def patched_run_cycle(region: int) -> CycleResult:
            result = original_run_cycle(region)
            calls.append(region)
            if len(calls) >= len(regions):
                # Signal that first pass is complete.
                first_pass_done.set()
                # Stop immediately so the inter-pass wait returns fast.
                stop_event.set()
            return result

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
        t.start()

        assert first_pass_done.wait(timeout=5.0), "First pass did not complete in time"
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever did not stop after stop_event"

        # Only one pass (2 calls) — no second pass.
        assert calls == regions, f"Expected exactly one pass {regions}, got {calls}"


# ---------------------------------------------------------------------------
# Test 4: UpstreamError from run_cycle does NOT crash the loop
# ---------------------------------------------------------------------------


class TestRunForeverUpstreamErrorSurvival:
    """UpstreamError raised by run_cycle is absorbed; loop continues."""

    def test_upstream_error_does_not_crash(self) -> None:
        settings = Settings(regions=[_REGION_A], interval_minutes=15)

        upstream_exc = UpstreamError("timeout", category="timeout")
        svc = _make_service(settings=settings, run_cycle_raises=upstream_exc)

        stop_event = threading.Event()
        first_call_done = threading.Event()

        original_run_cycle = svc.run_cycle

        def patched_run_cycle(region: int) -> CycleResult:
            # Signal before calling original (which will raise) so the test
            # doesn't wait forever for a result that never returns.
            first_call_done.set()
            stop_event.set()  # stop after this attempt
            return original_run_cycle(region)  # raises UpstreamError

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
        t.start()

        assert first_call_done.wait(timeout=5.0), "run_cycle was not called"
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever hung after UpstreamError"

        # At least one call was made (loop did not crash before calling spy).
        assert len(svc.run_cycle_calls) >= 1


# ---------------------------------------------------------------------------
# Test 5: Unexpected Exception from run_cycle does NOT crash the loop
# ---------------------------------------------------------------------------


class TestRunForeverUnexpectedExceptionSurvival:
    """Unexpected Exception raised by run_cycle is logged + backoff; loop survives."""

    def test_unexpected_exception_does_not_crash(self) -> None:
        settings = Settings(regions=[_REGION_A], interval_minutes=15)

        boom = RuntimeError("disk full")
        svc = _make_service(settings=settings, run_cycle_raises=boom)
        # Shorten backoff so test is fast.
        svc._UNEXPECTED_BACKOFF_SEC = 0.01  # type: ignore[misc]

        stop_event = threading.Event()
        first_call_done = threading.Event()

        original_run_cycle = svc.run_cycle

        def patched_run_cycle(region: int) -> CycleResult:
            # Signal before calling original (which will raise) and set stop_event
            # so that after the short backoff sleep run_forever exits cleanly.
            first_call_done.set()
            stop_event.set()
            return original_run_cycle(region)  # raises RuntimeError

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
        t.start()

        assert first_call_done.wait(timeout=5.0), "run_cycle was not called"
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever hung after unexpected Exception"

        assert len(svc.run_cycle_calls) >= 1


# ---------------------------------------------------------------------------
# Test 6: ParseBugError from run_cycle does NOT crash the loop
# ---------------------------------------------------------------------------


class TestRunForeverParseBugErrorSurvival:
    """ParseBugError raised by run_cycle is absorbed at WARNING; loop survives."""

    def test_parse_bug_error_does_not_crash(self, caplog: Any) -> None:
        import logging

        settings = Settings(regions=[_REGION_A, _REGION_B], interval_minutes=15)

        parse_exc = ParseBugError(selector=".lot", context="test")
        svc = _make_service(settings=settings, run_cycle_raises=parse_exc)

        stop_event = threading.Event()
        call_count = 0
        barrier = threading.Event()

        original_run_cycle = svc.run_cycle

        def patched_run_cycle(region: int) -> CycleResult:
            nonlocal call_count
            call_count += 1
            if call_count >= len(settings.regions):
                barrier.set()
                stop_event.set()
            return original_run_cycle(region)  # raises ParseBugError

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="fis_monitor.services.monitor_cycle"):
            t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
            t.start()
            assert barrier.wait(timeout=5.0), "run_cycle was not called for all regions"
            t.join(timeout=5.0)

        assert not t.is_alive(), "run_forever hung after ParseBugError"
        # Both regions were processed — loop survived without crashing.
        assert len(svc.run_cycle_calls) == len(settings.regions)
        # No ERROR-level records for parse domain errors.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"Expected no ERROR logs for ParseBugError, got: {[r.message for r in error_records]}"
        )


# ---------------------------------------------------------------------------
# Test 7: ParserVersionMismatch from run_cycle does NOT crash the loop
# ---------------------------------------------------------------------------


class TestRunForeverParserVersionMismatchSurvival:
    """ParserVersionMismatch raised by run_cycle is absorbed at WARNING; loop survives."""

    def test_parser_version_mismatch_does_not_crash(self, caplog: Any) -> None:
        import logging

        settings = Settings(regions=[_REGION_A, _REGION_B], interval_minutes=15)

        mismatch_exc = ParserVersionMismatch("parser version 2 != expected 1")
        svc = _make_service(settings=settings, run_cycle_raises=mismatch_exc)

        stop_event = threading.Event()
        call_count = 0
        barrier = threading.Event()

        original_run_cycle = svc.run_cycle

        def patched_run_cycle(region: int) -> CycleResult:
            nonlocal call_count
            call_count += 1
            if call_count >= len(settings.regions):
                barrier.set()
                stop_event.set()
            return original_run_cycle(region)  # raises ParserVersionMismatch

        svc.run_cycle = patched_run_cycle  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="fis_monitor.services.monitor_cycle"):
            t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
            t.start()
            assert barrier.wait(timeout=5.0), "run_cycle was not called for all regions"
            t.join(timeout=5.0)

        assert not t.is_alive(), "run_forever hung after ParserVersionMismatch"
        # Both regions were processed — loop survived without crashing.
        assert len(svc.run_cycle_calls) == len(settings.regions)
        # No ERROR-level records for parse domain errors.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        messages = [r.message for r in error_records]
        assert not error_records, (
            f"Expected no ERROR logs for ParserVersionMismatch, got: {messages}"
        )
