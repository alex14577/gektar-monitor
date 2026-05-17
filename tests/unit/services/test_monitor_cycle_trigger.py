"""Unit tests for MonitorCycleService manual-trigger behaviour.

Covers ``request_run_now()`` + ``_wait_for_next_pass()`` interaction with the
``run_forever`` scheduler loop.

Tests spawn real threads and use real queue.Queue — no mocking of the trigger
mechanism itself.  This follows the project's fake-impl invariant: tests must
exercise the real code path, not just verify isinstance() or attribute access.

Coverage:
  (a) request_run_now() after a pass wakes the scheduler before poll_interval.
  (b) Multiple request_run_now() calls collapse into one extra pass.
  (c) Without a trigger, the scheduler sleeps the full poll_interval.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.models import (
    CycleResult,
    Settings,
)
from fis_monitor.services.filter_matcher import AllFiltersMatcher
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

# Very long poll interval so tests that rely on early wakeup are unambiguous.
_LONG_POLL_SEC = 30.0


# ---------------------------------------------------------------------------
# Spy subclass — intercepts run_cycle without hitting real pipeline
# ---------------------------------------------------------------------------


class SpyCycleService(MonitorCycleService):
    """Subclass that replaces run_cycle with a no-op spy."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pass_count = 0
        self._pass_started = threading.Event()

    def run_cycle(self, region: int) -> CycleResult:  # type: ignore[override]
        self.pass_count += 1
        self._pass_started.set()
        return CycleResult(
            id=self.pass_count,
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


def _make_service(poll_interval_minutes: int = 0) -> SpyCycleService:
    """Build a SpyCycleService with a very long poll interval by default.

    ``interval_minutes=0`` means "continuous" which maps to
    ``_DEFAULT_POLL_INTERVAL_SEC`` (60 s).  For trigger tests we override
    ``_DEFAULT_POLL_INTERVAL_SEC`` to a large value so the test can assert
    on early wakeup.
    """
    settings = Settings(regions=[77], interval_minutes=poll_interval_minutes)
    svc = SpyCycleService(
        http=MinimalHttpClient(),
        list_parser=MinimalListParser(),
        enrichment=MinimalEnrichmentService(),
        lot_repo=FakeLotRepository(),
        cycles_repo=MinimalCyclesRepository(),
        notifier_dispatcher=MinimalNotifierDispatcher(),
        event_bus=MinimalEventBus(),
        config_source=MinimalConfigSource(settings),
        clock=MinimalClock(),
        cycle_progress_signal=threading.Event(),
        filter_matcher=AllFiltersMatcher([]),
    )
    # Override poll interval to a large value so early-wakeup tests are unambiguous.
    svc._DEFAULT_POLL_INTERVAL_SEC = _LONG_POLL_SEC  # type: ignore[misc]
    return svc


# ---------------------------------------------------------------------------
# Helper: run scheduler in background, return thread + stop event
# ---------------------------------------------------------------------------


def _start_scheduler(svc: SpyCycleService) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
    t.start()
    return t, stop_event


# ---------------------------------------------------------------------------
# Test (a): request_run_now() wakes scheduler early
# ---------------------------------------------------------------------------


class TestRequestRunNowWakesEarly:
    """request_run_now() causes run_forever to start a new pass before poll_interval expires."""

    def test_scheduler_wakes_before_poll_interval(self) -> None:
        svc = _make_service()
        t, stop_event = _start_scheduler(svc)

        # Wait until the first pass executes (loop startup).
        assert svc._pass_started.wait(timeout=5.0), "First pass did not execute in time"
        first_pass_count = svc.pass_count

        # Reset the pass_started event so we can detect the next pass.
        svc._pass_started.clear()

        # Record time before trigger — poll_interval is _LONG_POLL_SEC.
        trigger_at = time.monotonic()
        svc.request_run_now()

        # The scheduler should wake well before _LONG_POLL_SEC (5s tolerance).
        woke = svc._pass_started.wait(timeout=5.0)
        elapsed = time.monotonic() - trigger_at

        stop_event.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever did not exit after stop_event"

        assert woke, "Scheduler did not wake up after request_run_now()"
        assert elapsed < _LONG_POLL_SEC, (
            f"Scheduler took {elapsed:.2f}s — not woken early (poll_interval={_LONG_POLL_SEC}s)"
        )
        assert svc.pass_count > first_pass_count, "No extra pass was executed after trigger"


# ---------------------------------------------------------------------------
# Test (b): multiple request_run_now() calls collapse into one extra pass
# ---------------------------------------------------------------------------


class TestRequestRunNowCollapse:
    """Multiple request_run_now() calls while scheduler sleeps = exactly one extra pass."""

    def test_burst_triggers_one_pass(self) -> None:
        svc = _make_service()
        t, stop_event = _start_scheduler(svc)

        # Wait for the first pass to complete.
        assert svc._pass_started.wait(timeout=5.0), "First pass did not execute in time"
        svc._pass_started.clear()

        # Fire 5 trigger requests in rapid succession.
        for _ in range(5):
            svc.request_run_now()

        # Wait for the extra pass to start.
        assert svc._pass_started.wait(timeout=5.0), "Extra pass did not start after burst trigger"
        pass_after_burst = svc.pass_count

        # Give a moment for any spurious extra passes to fire.
        svc._pass_started.clear()
        # If the queue is properly drained, no second extra pass should start before
        # the next natural poll_interval (which is _LONG_POLL_SEC = 30s, >> 0.2s).
        extra = svc._pass_started.wait(timeout=0.2)

        stop_event.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever did not exit after stop_event"

        # First pass = 1, burst pass = 2; no third pass within 0.2s.
        assert pass_after_burst == 2, (
            f"Expected exactly 2 passes after burst, got {pass_after_burst}"
        )
        assert not extra, (
            "An unexpected third pass started within 0.2s — triggers were not drained"
        )


# ---------------------------------------------------------------------------
# Test (c): without a trigger, scheduler sleeps the full poll_interval
# ---------------------------------------------------------------------------


class TestNoTriggerSleepsFull:
    """Without request_run_now(), no second pass starts before poll_interval expires."""

    def test_no_early_wakeup_without_trigger(self) -> None:
        svc = _make_service()
        t, stop_event = _start_scheduler(svc)

        # Wait for the first pass.
        assert svc._pass_started.wait(timeout=5.0), "First pass did not execute in time"
        svc._pass_started.clear()

        # No trigger — second pass should NOT start within 0.3s.
        early_wakeup = svc._pass_started.wait(timeout=0.3)

        stop_event.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_forever did not exit after stop_event"

        assert not early_wakeup, (
            "Scheduler woke up early without a trigger — unexpected second pass"
        )


# ---------------------------------------------------------------------------
# Tests for _wait_for_next_pass directly
# ---------------------------------------------------------------------------


class TestWaitForNextPassDirect:
    """Direct unit tests for _wait_for_next_pass() using real stop_event and queue."""

    def test_wait_for_next_pass_exits_immediately_when_stop_event_already_set(
        self,
    ) -> None:
        """_wait_for_next_pass must return quickly when stop_event is already set.

        The implementation polls stop_event at the top of each slice loop
        (``while not stop_event.is_set()``), so a pre-set event exits before
        the first get() call.
        """
        svc = _make_service()
        stop_event = threading.Event()
        stop_event.set()  # already set before call

        start = time.monotonic()
        svc._wait_for_next_pass(stop_event, timeout=30.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"_wait_for_next_pass did not exit quickly with stop_event set "
            f"(elapsed={elapsed:.3f}s)"
        )

    def test_wait_for_next_pass_zero_timeout_returns_immediately(self) -> None:
        """_wait_for_next_pass with timeout=0.0 must return without blocking."""
        svc = _make_service()
        stop_event = threading.Event()  # not set

        start = time.monotonic()
        svc._wait_for_next_pass(stop_event, timeout=0.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"_wait_for_next_pass(timeout=0.0) did not return immediately "
            f"(elapsed={elapsed:.3f}s)"
        )

    def test_wait_for_next_pass_returns_after_timeout_without_trigger(self) -> None:
        """_wait_for_next_pass must return after ~timeout seconds with no trigger or stop."""
        svc = _make_service()
        stop_event = threading.Event()  # not set
        timeout_sec = 0.3

        start = time.monotonic()
        svc._wait_for_next_pass(stop_event, timeout=timeout_sec)
        elapsed = time.monotonic() - start

        # Allow a generous 2x window for slow CI — the important invariant is
        # that it returned *after* the timeout (not prematurely) and *before*
        # a runaway block (e.g. 10x timeout).
        assert elapsed >= timeout_sec * 0.8, (
            f"_wait_for_next_pass returned too early: elapsed={elapsed:.3f}s "
            f"but timeout={timeout_sec}s"
        )
        assert elapsed < timeout_sec * 10, (
            f"_wait_for_next_pass blocked far too long: elapsed={elapsed:.3f}s"
        )
