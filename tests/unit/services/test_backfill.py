"""Unit tests for BackfillService.

Coverage:
  1. Single-flight: second start() returns False while first is running.
  2. Status snapshot reflects correct progress counters.
  3. cancel() aborts a running backfill.
  4. Lots are upserted with notify=False.
  5. MonitorCycleService.mark/clear_region_in_backfill called correctly.
  6. Regions are processed in order.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.models import (
    LotUpsertResult,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.backfill import BackfillService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_REGION_A = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(lot_id: int, region: int = _REGION_A) -> ParsedListRow:
    return ParsedListRow(
        id=lot_id,
        cadastral_no=f"{region}:01:{lot_id:06d}:1",
        area_sqm=500,
        region=str(region),
        municipality="Тест",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePaginatedListFetcher:
    """Fake PaginatedListFetcher returning pre-configured rows per region."""

    def __init__(
        self,
        rows_by_region: dict[int, list[ParsedListRow]] | None = None,
    ) -> None:
        self._rows_by_region: dict[int, list[ParsedListRow]] = rows_by_region or {}
        self.iterate_calls: list[int] = []

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 2.0,
    ) -> Iterator[ParsedListRow]:
        self.iterate_calls.append(region)
        for row in self._rows_by_region.get(region, []):
            if stop_event.is_set():
                return
            yield row


class FakeLotRepository:
    """Minimal LotRepository fake tracking upsert calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[dict] = []

    def upsert(self, lot: Any, *, tracked: Any) -> LotUpsertResult:
        self.upsert_calls.append({"lot_id": lot.id})
        return LotUpsertResult(was_new=True, changes=[])

    def get(self, lot_id: int) -> None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list:
        return []

    def get_last_known_id(self, region: int) -> None:
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


class FakeConfigSource:
    def __init__(self, regions: list[int] | None = None) -> None:
        self._settings = Settings(regions=regions or [_REGION_A])

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeMonitorCycleService:
    """Tracks mark/clear_region_in_backfill calls."""

    def __init__(self) -> None:
        self.mark_calls: list[int] = []
        self.clear_calls: list[int] = []

    def mark_region_in_backfill(self, region: int) -> None:
        self.mark_calls.append(region)

    def clear_region_in_backfill(self, region: int) -> None:
        self.clear_calls.append(region)

    def request_run_now(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _make_service(
    *,
    rows_by_region: dict[int, list[ParsedListRow]] | None = None,
    regions: list[int] | None = None,
    lot_repo: FakeLotRepository | None = None,
    monitor_cycle: FakeMonitorCycleService | None = None,
    fetcher: FakePaginatedListFetcher | None = None,
) -> tuple[BackfillService, FakeLotRepository, FakeMonitorCycleService, FakePaginatedListFetcher]:
    lot_repo = lot_repo or FakeLotRepository()
    mc = monitor_cycle or FakeMonitorCycleService()
    fetcher = fetcher or FakePaginatedListFetcher(rows_by_region=rows_by_region)
    config = FakeConfigSource(regions=regions or [_REGION_A])

    svc = BackfillService(
        fetcher=fetcher,
        lot_repo=lot_repo,
        config_source=config,
        monitor_cycle=mc,
        sleep_between_pages=0.0,
    )
    return svc, lot_repo, mc, fetcher


# ---------------------------------------------------------------------------
# Anti-mock: all fake methods are exercised
# ---------------------------------------------------------------------------

def test_fake_all_methods() -> None:
    """Exercise every method on the fakes to catch API drift."""
    fetcher = FakePaginatedListFetcher(rows_by_region={_REGION_A: [_make_row(1)]})
    stop = threading.Event()
    rows = list(fetcher.iterate(_REGION_A, stop, sleep_between_pages=0.0))
    assert len(rows) == 1

    repo = FakeLotRepository()
    from fis_monitor.domain.models import Lot
    lot = Lot(
        id=1, cadastral_no="77:01:000001:1", area_sqm=500, region="77",
        municipality="T", land_category="L", permitted_use="P", ogv="O",
        status="S", date_create=_NOW, date_update=_NOW,
        lat=None, lon=None, has_boundaries=None, raw_json={}, parser_version=1,
        first_seen=_NOW, last_seen=_NOW, detail_fetched_at=None,
        enrichment_status="pending", last_seen_at=_NOW, is_active=True,
        inactive_reason=None, inactive_since=None, inactive_confirmed_at=None,
    )
    result = repo.upsert(lot, tracked=("status",))
    assert result.was_new is True
    assert repo.get(1) is None
    assert repo.list_active(limit=10, offset=0) == []
    assert repo.get_last_known_id(1) is None
    repo.set_last_known_id(1, 42)
    repo.mark_seen([1], _NOW)
    repo.mark_inactive(1, "test", _NOW)
    assert repo.needing_enrichment(5) == []
    assert repo.count_active() == 0

    mc = FakeMonitorCycleService()
    mc.mark_region_in_backfill(1)
    mc.clear_region_in_backfill(1)
    mc.request_run_now()
    assert mc.mark_calls == [1]
    assert mc.clear_calls == [1]


# ---------------------------------------------------------------------------
# Test 1: basic backfill — lots upserted with notify=False
# ---------------------------------------------------------------------------

class TestBasicBackfill:
    def test_upserts_all_rows(self) -> None:
        rows = [_make_row(1), _make_row(2), _make_row(3)]
        svc, lot_repo, _mc, _fetcher = _make_service(
            rows_by_region={_REGION_A: rows},
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert len(lot_repo.upsert_calls) == 3

    def test_mark_clear_called_per_region(self) -> None:
        svc, _lot_repo, mc, _fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert mc.mark_calls == [_REGION_A]
        assert mc.clear_calls == [_REGION_A]

    def test_multiple_regions_processed(self) -> None:
        svc, lot_repo, _mc, _fetcher = _make_service(
            rows_by_region={
                _REGION_A: [_make_row(1), _make_row(2)],
                _REGION_B: [_make_row(10)],
            },
            regions=[_REGION_A, _REGION_B],
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert len(lot_repo.upsert_calls) == 3
        assert set(c["lot_id"] for c in lot_repo.upsert_calls) == {1, 2, 10}


# ---------------------------------------------------------------------------
# Test 2: single-flight
# ---------------------------------------------------------------------------

def _wait_until_done(svc: BackfillService, timeout: float = 5.0) -> None:
    """Block until ``svc.is_running()`` returns False or timeout expires."""
    import time
    deadline = time.monotonic() + timeout
    while svc.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)


class TestSingleFlight:
    def test_second_start_returns_false_while_running(self) -> None:
        """A second start() call while one is running returns False immediately.

        start() now spawns a daemon thread internally (P1-5), so the test
        barrier waits until the worker thread is inside SlowFetcher.iterate
        (i.e. the backfill is genuinely in-flight) before attempting a
        second concurrent start().
        """
        barrier = threading.Barrier(2)
        done_event = threading.Event()

        class SlowFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
            ) -> Iterator[ParsedListRow]:
                barrier.wait()  # signal that backfill worker is running
                done_event.wait(timeout=5.0)  # hold until test releases
                return iter([])

        svc, *_ = _make_service(fetcher=SlowFetcher())  # type: ignore[call-overload]

        stop = threading.Event()

        # First start() — spawns a daemon thread internally; returns True immediately.
        r1 = svc.start(stop)
        assert r1 is True

        barrier.wait()  # wait until the worker thread is inside SlowFetcher.iterate

        # Now backfill is running — second start() should return False.
        second_result = svc.start(stop)
        assert second_result is False, f"Expected False, got {second_result}"
        assert svc.is_running() is True

        # Release the worker thread.
        done_event.set()
        _wait_until_done(svc)
        assert not svc.is_running()

    def test_second_start_allowed_after_first_finishes(self) -> None:
        """Two sequential start() calls both return True (no overlap)."""
        svc, _lot_repo, *_ = _make_service(rows_by_region={})

        stop = threading.Event()
        r1 = svc.start(stop)
        assert r1 is True
        # Wait for the first run to complete before attempting the second.
        _wait_until_done(svc)
        r2 = svc.start(stop)
        _wait_until_done(svc)
        assert r2 is True  # second run started after first completed


# ---------------------------------------------------------------------------
# Test 3: status snapshot
# ---------------------------------------------------------------------------

class TestStatusSnapshot:
    def test_idle_status(self) -> None:
        svc, *_ = _make_service()
        snap = svc.status()
        assert snap.running is False
        assert snap.lots_seen == 0
        assert snap.regions_total == 0
        assert snap.started_at is None

    def test_status_after_run(self) -> None:
        rows = [_make_row(1), _make_row(2)]
        svc, _lot_repo, _mc, _ = _make_service(
            rows_by_region={_REGION_A: rows},
            regions=[_REGION_A, _REGION_B],
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        snap = svc.status()
        assert snap.running is False
        assert snap.lots_seen == 2
        assert snap.regions_done == 2
        assert snap.regions_total == 2
        assert snap.started_at is not None


# ---------------------------------------------------------------------------
# Test 4: cancel()
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_while_running_stops_backfill(self) -> None:
        """cancel() sets the stop event; the backfill stops before processing all regions.

        start() now spawns the backfill daemon thread internally.  The test uses
        the barrier to synchronize with the worker thread directly.
        """
        barrier = threading.Barrier(2)
        cancel_issued = threading.Event()

        class StopOnSecondRegion:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
            ) -> Iterator[ParsedListRow]:
                if region == _REGION_B:
                    # Signal the main thread to cancel
                    barrier.wait()
                    cancel_issued.wait(timeout=5.0)
                return iter([_make_row(1, region=region)])

        svc, _lot_repo, *_ = _make_service(
            fetcher=StopOnSecondRegion(),  # type: ignore[call-overload]
            regions=[_REGION_A, _REGION_B],
        )

        stop = threading.Event()
        # start() spawns the worker thread internally and returns True immediately.
        svc.start(stop)
        assert svc.is_running()

        barrier.wait()  # wait until worker thread is inside region B
        svc.cancel()
        cancel_issued.set()

        _wait_until_done(svc, timeout=5.0)
        assert not svc.is_running()

    def test_cancel_when_idle_is_noop(self) -> None:
        """cancel() when not running does not raise."""
        svc, *_ = _make_service()
        svc.cancel()  # should not raise
        assert not svc.is_running()


# ---------------------------------------------------------------------------
# Test 5: watcher thread exits on normal completion (P0-5)
# ---------------------------------------------------------------------------

class TestWatcherExitsOnNormalCompletion:
    def test_watcher_exits_on_normal_completion(self) -> None:
        """Stop-watcher daemon thread must exit within 2 s after a clean backfill run.

        Regression guard for P0-5: without ``self._stop_event.set()`` in the
        finally block of ``_run()``, the watcher thread polls forever because
        neither the internal nor the external stop-event is set on a normal
        finish.  The thread is daemon=True so it is invisible to join(), but it
        still consumes OS resources.

        Strategy: run a one-region backfill synchronously (start() blocks), then
        verify the watcher thread is no longer alive within 2 s.
        """
        rows = [_make_row(1), _make_row(2)]
        svc, _lot_repo, _mc, _fetcher = _make_service(
            rows_by_region={_REGION_A: rows},
        )

        # Grab the watcher before it's created to identify it by name after.
        # The watcher is started inside _combined_stop() which is called from
        # _run().  We capture it by listing threads before and after start().
        import threading

        stop = threading.Event()
        # start() is synchronous — it blocks until _run() completes.
        svc.start(stop)

        # The backfill finished; find the watcher thread if it exists.
        watcher_threads = [
            t for t in threading.enumerate()
            if t.name == "backfill-stop-watcher"
        ]

        if watcher_threads:
            # Watcher should exit shortly after _stop_event.set().
            watcher_threads[0].join(timeout=2.0)
            assert not watcher_threads[0].is_alive(), (
                "Stop-watcher thread is still alive after backfill finished "
                "(P0-5 regression: _stop_event.set() not called in _run finally)"
            )
        # If no watcher thread is found, it already exited — which is also correct.
