"""Unit tests for MonitorCycleService backfill skip-set.

Coverage:
  1. mark_region_in_backfill adds region to the skip set.
  2. clear_region_in_backfill removes region from the skip set.
  3. run_forever skips regions present in the backfill set.
  4. run_forever processes regions NOT in the backfill set.
  5. clear_region_in_backfill is idempotent (discard, not remove).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import (
    CycleResult,
    HttpResponse,
    LotPublicDTO,
    LotUpsertResult,
    ParsedListRow,
    Settings,
    TrackedField,
)
from fis_monitor.services.filter_matcher import AllFiltersMatcher
from fis_monitor.services.monitor_cycle import MonitorCycleService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_REGION_A = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class FakeHttpClient:
    def get(
        self, url: str, *, params: Any = None, headers: Any = None, timeout: float | None = None
    ) -> HttpResponse:
        return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)


class FakeListParser:
    def parse(self, html: str) -> list[ParsedListRow]:
        return []


class FakeEnrichmentService:
    def enrich_lots(self, lots: Sequence[Lot], *, max_workers: int) -> list[Lot]:
        return list(lots)


class FakeLotRepository:
    def upsert(self, lot: Lot, *, tracked: Sequence[TrackedField]) -> LotUpsertResult:
        return LotUpsertResult(was_new=False, changes=[])

    def get(self, lot_id: int) -> Lot | None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return []

    def get_last_known_id(self, region: int) -> int | None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []

    def count_active(self) -> int:
        return 0


class FakeCyclesRepository:
    def __init__(self) -> None:
        self._next = 1

    def open(self, region: int, at: datetime) -> int:
        idx = self._next
        self._next += 1
        return idx

    def close(self, cycle_id: int, result: CycleResult) -> None:
        pass

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


class FakeNotifierDispatcher:
    def dispatch(self, lot: LotPublicDTO) -> None:
        pass


class FakeEventBus:
    def publish(self, event: Any) -> None:
        pass

    def subscribe(self) -> Any:
        raise NotImplementedError


class FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class FakeConfigSource:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Spy subclass
# ---------------------------------------------------------------------------

class SpyMonitorCycleService(MonitorCycleService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_cycle_calls: list[int] = []

    def run_cycle(self, region: int) -> CycleResult:  # type: ignore[override]
        self.run_cycle_calls.append(region)
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


def _make_service(regions: list[int] | None = None) -> SpyMonitorCycleService:
    settings = Settings(regions=regions or [_REGION_A])
    return SpyMonitorCycleService(
        http=FakeHttpClient(),
        list_parser=FakeListParser(),
        enrichment=FakeEnrichmentService(),
        lot_repo=FakeLotRepository(),
        cycles_repo=FakeCyclesRepository(),
        notifier_dispatcher=FakeNotifierDispatcher(),
        event_bus=FakeEventBus(),
        config_source=FakeConfigSource(settings=settings),
        clock=FakeClock(),
        cycle_progress_signal=threading.Event(),
        filter_matcher=AllFiltersMatcher([]),
    )


# ---------------------------------------------------------------------------
# Test 1: mark / clear change the skip-set
# ---------------------------------------------------------------------------

class TestMarkClearRegionInBackfill:
    def test_mark_adds_region(self) -> None:
        svc = _make_service()
        svc.mark_region_in_backfill(_REGION_A)
        with svc._backfill_lock:
            assert _REGION_A in svc._regions_in_backfill

    def test_clear_removes_region(self) -> None:
        svc = _make_service()
        svc.mark_region_in_backfill(_REGION_A)
        svc.clear_region_in_backfill(_REGION_A)
        with svc._backfill_lock:
            assert _REGION_A not in svc._regions_in_backfill

    def test_clear_is_idempotent(self) -> None:
        """Clearing a region that was never marked does not raise."""
        svc = _make_service()
        svc.clear_region_in_backfill(_REGION_A)  # never marked — must not raise
        with svc._backfill_lock:
            assert _REGION_A not in svc._regions_in_backfill

    def test_mark_multiple_regions(self) -> None:
        svc = _make_service()
        svc.mark_region_in_backfill(_REGION_A)
        svc.mark_region_in_backfill(_REGION_B)
        with svc._backfill_lock:
            assert {_REGION_A, _REGION_B} == svc._regions_in_backfill


# ---------------------------------------------------------------------------
# Test 2: run_forever skips regions in backfill set
# ---------------------------------------------------------------------------

class TestRunForeverSkipsBackfilledRegions:
    def test_backfilled_region_is_skipped(self) -> None:
        """Region in backfill set → run_cycle is NOT called for it."""
        svc = _make_service(regions=[_REGION_A, _REGION_B])

        # Mark region A as in backfill before starting the loop
        svc.mark_region_in_backfill(_REGION_A)

        stop = threading.Event()

        # We need to stop after one pass. Patch _wait_for_next_pass to stop immediately.
        def _stop_after_first_pass(stop_ev: threading.Event, timeout: float) -> None:
            stop_ev.set()

        svc._wait_for_next_pass = _stop_after_first_pass  # type: ignore[method-assign]

        svc.run_forever(stop)

        # Region A was in backfill → skipped; Region B was processed.
        assert _REGION_A not in svc.run_cycle_calls, (
            f"Region A should have been skipped, got calls: {svc.run_cycle_calls}"
        )
        assert _REGION_B in svc.run_cycle_calls, (
            f"Region B should have been processed, got calls: {svc.run_cycle_calls}"
        )

    def test_region_processed_after_clear(self) -> None:
        """Once a region is cleared from backfill set, run_cycle is called for it."""
        svc = _make_service(regions=[_REGION_A])

        svc.mark_region_in_backfill(_REGION_A)
        svc.clear_region_in_backfill(_REGION_A)

        stop = threading.Event()

        def _stop_after_first_pass(stop_ev: threading.Event, timeout: float) -> None:
            stop_ev.set()

        svc._wait_for_next_pass = _stop_after_first_pass  # type: ignore[method-assign]

        svc.run_forever(stop)

        assert _REGION_A in svc.run_cycle_calls
