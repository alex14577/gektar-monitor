"""Unit tests for BackfillService.

Coverage:
  1. Single-flight: second start() returns False while first is running.
  2. Status snapshot reflects correct progress counters.
  3. cancel() aborts a running backfill.
  4. Lots are upserted with notify=False.
  5. MonitorCycleService.mark/clear_region_in_backfill called correctly.
  6. Regions are processed in order.
  7. backfill.delta_triggered INFO log emitted with correct payload when maybe_start returns True.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fis_monitor.domain.models import (
    LotUpsertResult,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.backfill import BackfillService
from tests.fakes.clock import FakeClock
from tests.fakes.state_repository import FakeStateRepository

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
        self.iterate_kwargs: list[dict] = []

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 2.0,
        per_page: int | None = None,
        max_pages: int | None = None,
        page_start: int = 1,
        page_callback: Callable[[int, int], None] | None = None,
        total_callback: Callable[[int], None] | None = None,
        raise_on_network_error: bool = False,
        raise_on_parse_error: bool = False,
    ) -> Iterator[ParsedListRow]:
        self.iterate_calls.append(region)
        self.iterate_kwargs.append(
            {"per_page": per_page, "max_pages": max_pages, "page_start": page_start}
        )
        rows = self._rows_by_region.get(region, [])
        if rows and page_callback is not None:
            page_callback(1, len(rows))
        if total_callback is not None:
            total_callback(len(rows))
        for row in rows:
            if stop_event.is_set():
                return
            yield row


class FakeLotRepository:
    """Minimal LotRepository fake tracking upsert calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[dict] = []

    def upsert(self, lot: Any, *, tracked: Any, is_backfill: bool = False) -> LotUpsertResult:
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

    def count_active(self, region_ids: tuple[int, ...] = ()) -> int:
        return 0


class FakeConfigSource:
    def __init__(self, regions: list[int] | None = None) -> None:
        self._settings = Settings(regions=regions or [_REGION_A])

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeMonitorCycleService:
    """Tracks mark/clear_region_in_backfill and request_run_now calls."""

    def __init__(self) -> None:
        self.mark_calls: list[int] = []
        self.clear_calls: list[int] = []
        self.run_now_calls: int = 0

    def mark_region_in_backfill(self, region: int) -> None:
        self.mark_calls.append(region)

    def clear_region_in_backfill(self, region: int) -> None:
        self.clear_calls.append(region)

    def request_run_now(self) -> None:
        self.run_now_calls += 1


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event: object) -> None:
        self.published.append(event)

    def subscribe(self) -> object:
        raise NotImplementedError





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
    event_bus: FakeEventBus | None = None,
    state_repo: FakeStateRepository | None = None,
    clock: FakeClock | None = None,
) -> tuple[BackfillService, FakeLotRepository, FakeMonitorCycleService, FakePaginatedListFetcher]:
    lot_repo = lot_repo or FakeLotRepository()
    mc = monitor_cycle or FakeMonitorCycleService()
    fetcher = fetcher or FakePaginatedListFetcher(rows_by_region=rows_by_region)
    config = FakeConfigSource(regions=regions or [_REGION_A])
    bus = event_bus or FakeEventBus()
    state_repo = state_repo or FakeStateRepository()

    svc = BackfillService(
        fetcher=fetcher,
        lot_repo=lot_repo,
        config_source=config,
        monitor_cycle=mc,
        event_bus=bus,
        clock=clock or FakeClock(),
        state_repo=state_repo,
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
    page_cb_calls: list[tuple[int, int]] = []

    def _cb(p: int, n: int) -> None:
        page_cb_calls.append((p, n))

    rows = list(fetcher.iterate(_REGION_A, stop, sleep_between_pages=0.0, page_callback=_cb))
    assert len(rows) == 1
    assert page_cb_calls == [(1, 1)]

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
    assert mc.run_now_calls == 1

    state_repo = FakeStateRepository()
    state_repo.set("k", "v")
    assert state_repo.get("k") == "v"
    state_repo.delete("k")
    assert state_repo.get("k") is None


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

    def test_single_region_pass_adr064(self) -> None:
        """ADR-064: single-region pass — only regions[0] is fetched (region= is no-op)."""
        svc, lot_repo, _mc, fetcher = _make_service(
            rows_by_region={
                _REGION_A: [_make_row(1), _make_row(2)],
                _REGION_B: [_make_row(10)],
            },
            regions=[_REGION_A, _REGION_B],
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        # Only regions[0]=REGION_A fetched; REGION_B not iterated (ADR-064 no-op)
        assert len(lot_repo.upsert_calls) == 2
        assert set(c["lot_id"] for c in lot_repo.upsert_calls) == {1, 2}
        assert fetcher.iterate_calls == [_REGION_A]

    def test_iterate_called_with_per_page_20(self) -> None:
        """BackfillService passes per_page=20 (ADR-036 updated 2026-05-16: reduced from 50)."""
        svc, _lot_repo, _mc, fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert fetcher.iterate_kwargs[0]["per_page"] == 20
        assert fetcher.iterate_kwargs[0]["max_pages"] == 5


# ---------------------------------------------------------------------------
# Test: page_callback updates _progress.current_page (replaces parallel counter)
# ---------------------------------------------------------------------------

class TestPageCallbackUpdatesProgress:
    def test_current_page_updated_via_callback(self) -> None:
        """BackfillService uses page_callback to update current_page, not a parallel counter.

        The fake fetcher invokes page_callback(1, n) for its single batch of rows.
        After the run, _progress.current_page has been set to 1 via the callback
        and then cleared to None by _run() region bookkeeping — this confirms the
        callback path is wired and not silently discarded.
        """
        # Use a fetcher that records which page_callback it received.
        recorded_callbacks: list[Callable[[int, int], None]] = []

        class CallbackCapturingFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: Callable[[int, int], None] | None = None,
                total_callback: Callable[[int], None] | None = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
            ) -> Iterator[ParsedListRow]:
                if page_callback is not None:
                    recorded_callbacks.append(page_callback)
                    page_callback(1, 2)
                yield _make_row(1)
                yield _make_row(2)

        from fis_monitor.services.backfill import BackfillService

        lot_repo = FakeLotRepository()
        mc = FakeMonitorCycleService()
        config = FakeConfigSource(regions=[_REGION_A])
        svc = BackfillService(
            fetcher=CallbackCapturingFetcher(),  # type: ignore[arg-type]
            lot_repo=lot_repo,
            config_source=config,
            monitor_cycle=mc,
            event_bus=FakeEventBus(),
            clock=FakeClock(),
            state_repo=FakeStateRepository(),
            sleep_between_pages=0.0,
        )

        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        # Callback was passed and invoked: recorded_callbacks must be non-empty.
        assert len(recorded_callbacks) == 1, "page_callback not passed to iterate()"
        # All lots upserted.
        assert len(lot_repo.upsert_calls) == 2


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
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: Callable[[int, int], None] | None = None,
                total_callback: Callable[[int], None] | None = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
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
        assert snap.regions_total == 1  # ADR-064: single-region pass
        assert snap.started_at is not None


# ---------------------------------------------------------------------------
# Test 4: cancel()
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_while_running_stops_backfill(self) -> None:
        """cancel() sets the stop event; the backfill stops mid-iteration.

        Single-region pass (ADR-064): uses a blocking fetcher on the single
        region to test cancel semantics.
        """
        barrier = threading.Barrier(2)
        cancel_issued = threading.Event()

        class BlockingFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: Callable[[int, int], None] | None = None,
                total_callback: Callable[[int], None] | None = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
            ) -> Iterator[ParsedListRow]:
                # Signal the main thread that iteration started
                barrier.wait()
                cancel_issued.wait(timeout=5.0)
                return iter([])

        svc, _lot_repo, *_ = _make_service(
            fetcher=BlockingFetcher(),  # type: ignore[call-overload]
            regions=[_REGION_A],
        )

        stop = threading.Event()
        svc.start(stop)
        assert svc.is_running()

        barrier.wait()  # wait until worker thread is inside iterate
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


# ---------------------------------------------------------------------------
# Test: start(regions=[1,2]) processes only those regions
# ---------------------------------------------------------------------------

class TestSubsetRegions:
    def test_start_subset_uses_first_region_adr064(self) -> None:
        """ADR-064: single-region pass — start(regions=[A,B]) fetches only regions[0]=A."""
        svc, lot_repo, _mc, fetcher = _make_service(
            rows_by_region={
                _REGION_A: [_make_row(1)],
                _REGION_B: [_make_row(10)],
                99: [_make_row(99)],
            },
            regions=[_REGION_A, _REGION_B, 99],
        )

        stop = threading.Event()
        svc.start(stop, regions=[_REGION_A, _REGION_B])
        _wait_until_done(svc)

        # Only regions[0]=REGION_A fetched (ADR-064 single-region pass)
        assert fetcher.iterate_calls == [_REGION_A]
        assert len(lot_repo.upsert_calls) == 1


# ---------------------------------------------------------------------------
# Test: maybe_start gate
# ---------------------------------------------------------------------------

class TestMaybeStart:
    def test_site_total_none_returns_false(self) -> None:
        svc, *_ = _make_service()
        stop = threading.Event()
        result = svc.maybe_start(_REGION_A, site_total=None, db_count=0, stop_event=stop)
        assert result is False
        assert not svc.is_running()

    def test_already_running_returns_false(self) -> None:
        barrier = threading.Barrier(2)
        done_event = threading.Event()

        class SlowFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: object = None,
                total_callback: object = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
            ):
                barrier.wait()
                done_event.wait(timeout=5.0)
                return iter([])

        svc, *_ = _make_service(fetcher=SlowFetcher())  # type: ignore[call-overload]
        stop = threading.Event()
        svc.start(stop)
        barrier.wait()

        result = svc.maybe_start(_REGION_A, site_total=100, db_count=0, stop_event=stop)
        assert result is False

        done_event.set()
        _wait_until_done(svc)

    def test_delta_below_threshold_returns_false(self) -> None:
        # delta=2, hint=4, threshold=3 → delta(2) <= hint(4)+threshold(3) → False
        svc, *_ = _make_service()
        stop = threading.Event()
        result = svc.maybe_start(
            _REGION_A, site_total=102, db_count=100, stop_event=stop,
            len_parsed_hint=4,
        )
        assert result is False
        assert not svc.is_running()

    def test_delta_above_threshold_returns_true_and_starts(self) -> None:
        # delta=10, hint=0, threshold=3 → delta(10) > hint(0)+threshold(3) → True
        svc, _lot_repo, _mc, fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )
        stop = threading.Event()
        result = svc.maybe_start(
            _REGION_A, site_total=110, db_count=100, stop_event=stop,
            len_parsed_hint=0,
        )
        assert result is True
        _wait_until_done(svc)
        # Only the given region was iterated
        assert fetcher.iterate_calls == [_REGION_A]

    def test_negative_delta_returns_false_skip_negative(self) -> None:
        # delta < 0 → False, decision=skip_negative
        svc, *_ = _make_service()
        stop = threading.Event()
        result = svc.maybe_start(_REGION_A, site_total=50, db_count=100, stop_event=stop)
        assert result is False
        assert not svc.is_running()

    def test_concurrent_maybe_start_single_flight(self) -> None:
        """Concurrent maybe_start for 4 regions — only the first one starts."""
        regions = [10, 20, 30, 40]
        rows_by_region = {r: [_make_row(r)] for r in regions}
        svc, _lot_repo, _mc, _fetcher = _make_service(
            rows_by_region=rows_by_region,
            regions=regions,
        )

        # Two-party barrier: main thread + exactly 1 winning iterate call.
        in_iterate = threading.Barrier(2)
        done_event = threading.Event()

        class TrackingSlowFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: object = None,
                total_callback: object = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
            ):
                in_iterate.wait(timeout=5.0)  # signal: one winner inside iterate
                done_event.wait(timeout=5.0)
                return iter([])

        svc._fetcher = TrackingSlowFetcher()  # type: ignore[assignment]

        stop = threading.Event()
        results: list[bool] = []
        result_lock = threading.Lock()

        def _try(r: int) -> None:
            res = svc.maybe_start(r, site_total=200, db_count=10, stop_event=stop)
            with result_lock:
                results.append(res)

        threads = [threading.Thread(target=_try, args=(r,)) for r in regions]
        for t in threads:
            t.start()

        # Wait until the winning backfill is inside the slow fetcher.
        # The barrier times out (raises BrokenBarrierError) if no winner enters,
        # which would make the assertion below fail clearly.
        with contextlib.suppress(threading.BrokenBarrierError):
            in_iterate.wait(timeout=5.0)

        # At this point 1 winner is running; the other 3 threads have already
        # called maybe_start and got False (they checked _running=True under lock).
        for t in threads:
            t.join(timeout=5.0)

        done_event.set()
        _wait_until_done(svc)

        assert results.count(True) == 1
        assert results.count(False) == len(regions) - 1


# ---------------------------------------------------------------------------
# Test: request_run_now called after success, NOT after cancel
# ---------------------------------------------------------------------------

class TestRequestRunNow:
    def test_request_run_now_called_after_success(self) -> None:
        svc, _lot_repo, mc, _fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)
        assert mc.run_now_calls == 1

    def test_request_run_now_not_called_after_cancel(self) -> None:
        barrier = threading.Barrier(2)
        cancel_issued = threading.Event()

        class SlowFetcher:
            def iterate(
                self,
                region: int,
                stop_event: threading.Event,
                *,
                sleep_between_pages: float = 2.0,
                per_page: int | None = None,
                max_pages: int | None = None,
                page_start: int = 1,
                page_callback: object = None,
                total_callback: object = None,
                raise_on_network_error: bool = False,
                raise_on_parse_error: bool = False,
            ):
                barrier.wait()
                cancel_issued.wait(timeout=5.0)
                return iter([])

        svc, _lot_repo, mc, _ = _make_service(
            fetcher=SlowFetcher(),  # type: ignore[call-overload]
            regions=[_REGION_A],
        )
        stop = threading.Event()
        svc.start(stop)
        barrier.wait()
        svc.cancel()
        cancel_issued.set()
        _wait_until_done(svc)
        assert mc.run_now_calls == 0


# ---------------------------------------------------------------------------
# Test: backfill.delta_triggered structured log (hf77)
# ---------------------------------------------------------------------------


class TestDeltaTriggeredLog:
    """Invariant: backfill.delta_triggered INFO log on trigger with correct payload."""

    def test_delta_triggered_log_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # delta=10, hint=0, threshold=3 → delta(10) > hint(0)+3 → True → log emitted.
        svc, _lot_repo, _mc, _fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )
        stop = threading.Event()

        with caplog.at_level(logging.INFO):
            result = svc.maybe_start(
                _REGION_A, site_total=110, db_count=100, stop_event=stop,
                len_parsed_hint=0,
            )

        _wait_until_done(svc)
        assert result is True

        triggered = [
            r for r in caplog.records if r.message == "backfill.delta_triggered"
        ]
        assert triggered, "Expected backfill.delta_triggered INFO log"
        rec = triggered[0]
        assert rec.levelno == logging.INFO
        assert rec.region_id == _REGION_A  # type: ignore[attr-defined]
        assert rec.delta == 10  # site_total(110) - db_count(100)  # type: ignore[attr-defined]
        assert rec.threshold == 3  # hint(0) + _DELTA_THRESHOLD(3)  # type: ignore[attr-defined]

    def test_delta_triggered_log_not_emitted_when_below_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # delta=2, hint=0, threshold=3 → not triggered → no log.
        svc, *_ = _make_service()
        stop = threading.Event()

        with caplog.at_level(logging.INFO):
            result = svc.maybe_start(
                _REGION_A, site_total=102, db_count=100, stop_event=stop,
                len_parsed_hint=0,
            )

        assert result is False
        triggered = [
            r for r in caplog.records if r.message == "backfill.delta_triggered"
        ]
        assert not triggered, "backfill.delta_triggered must not be logged when not triggered"


# ---------------------------------------------------------------------------
# Test: structured observability logs (gektar_monitor-su21)
# ---------------------------------------------------------------------------

_LOGGER_NAME = "fis_monitor.services.backfill"


class TestMaybeStartEntryLog:
    """Invariant 1: backfill.maybe_start.entry logged on every maybe_start call."""

    def test_entry_logged_with_payload(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, *_ = _make_service()
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(
                _REGION_A, site_total=50, db_count=40, stop_event=stop, len_parsed_hint=2
            )

        entries = [r for r in caplog.records if r.message == "backfill.maybe_start.entry"]
        assert entries, "backfill.maybe_start.entry must be logged"
        rec = entries[0]
        assert rec.region_id == _REGION_A  # type: ignore[attr-defined]
        assert rec.site_total == 50  # type: ignore[attr-defined]
        assert rec.db_count == 40  # type: ignore[attr-defined]
        assert rec.len_hint == 2  # type: ignore[attr-defined]
        # hint(2) + _DELTA_THRESHOLD(3)
        assert rec.threshold_computed == 5  # type: ignore[attr-defined]
        assert rec.currently_running is False  # type: ignore[attr-defined]


class TestMaybeStartDecisionLog:
    """Invariant 2: backfill.maybe_start.decision logged with correct decision field."""

    def _decisions(self, caplog: pytest.LogCaptureFixture) -> list:
        return [r for r in caplog.records if r.message == "backfill.maybe_start.decision"]

    def test_decision_skip_none(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, *_ = _make_service()
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(_REGION_A, site_total=None, db_count=0, stop_event=stop)
        recs = self._decisions(caplog)
        assert recs and recs[0].decision == "skip_none"  # type: ignore[attr-defined]

    def test_decision_skip_negative(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, *_ = _make_service()
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(_REGION_A, site_total=50, db_count=100, stop_event=stop)
        recs = self._decisions(caplog)
        assert recs and recs[0].decision == "skip_negative"  # type: ignore[attr-defined]

    def test_decision_skip_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        # delta=2, hint=0, threshold=3 → below threshold
        svc, *_ = _make_service()
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(
                _REGION_A, site_total=102, db_count=100, stop_event=stop, len_parsed_hint=0
            )
        recs = self._decisions(caplog)
        assert recs and recs[0].decision == "skip_threshold"  # type: ignore[attr-defined]

    def test_decision_trigger(self, caplog: pytest.LogCaptureFixture) -> None:
        # delta=10, hint=0, threshold=3 → trigger
        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            regions=[_REGION_A],
        )
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(_REGION_A, site_total=110, db_count=100, stop_event=stop)
        _wait_until_done(svc)
        recs = self._decisions(caplog)
        assert recs and recs[0].decision == "trigger"  # type: ignore[attr-defined]
        assert recs[0].delta == 10  # type: ignore[attr-defined]

    def test_decision_skip_running(self, caplog: pytest.LogCaptureFixture) -> None:
        barrier = threading.Barrier(2)
        done_event = threading.Event()

        class SlowFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                barrier.wait()
                done_event.wait(timeout=5.0)
                return iter([])

        svc, *_ = _make_service(fetcher=SlowFetcher())  # type: ignore[call-overload]
        stop = threading.Event()
        svc.start(stop)
        barrier.wait()

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.maybe_start(_REGION_A, site_total=100, db_count=0, stop_event=stop)

        done_event.set()
        _wait_until_done(svc)
        recs = [r for r in caplog.records if r.message == "backfill.maybe_start.decision"]
        assert recs and recs[0].decision == "skip_running"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Regression: hs9c — dead counter fields must not be present on BackfillStatus
# ---------------------------------------------------------------------------


def test_progress_snapshot_has_no_dead_counter_fields() -> None:
    """hs9c: lots_seen/regions_done/total_pages_seen removed — must not reappear.

    On prod these counters reported 652 lots instead of 412 because
    _progress.lots_seen was incremented on every upsert, including
    re-seen lots from pagination drift on torgi.gov.ru.  After hiq3 the
    UI reads only ``status``; the counters were dead code.  This test
    catches accidental re-introduction of any of the three fields.
    """
    svc, *_ = _make_service(rows_by_region={_REGION_A: [_make_row(1)]})
    stop = threading.Event()
    svc.start(stop)
    _wait_until_done(svc)

    snap = svc.status()
    assert not hasattr(snap, "lots_seen"), (
        "lots_seen re-introduced on BackfillStatus — hs9c regression"
    )
    assert not hasattr(snap, "regions_done"), (
        "regions_done re-introduced on BackfillStatus — hs9c regression"
    )
    assert not hasattr(snap, "total_pages_seen"), (
        "total_pages_seen re-introduced on BackfillStatus — hs9c regression"
    )


class TestRegionStartFinishLog:
    """Invariant 3: region.start + region.finish bracket each region; duration_ms >= 0."""

    def test_region_start_and_finish_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(1), _make_row(2)]},
            regions=[_REGION_A],
        )
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.start(stop)
            _wait_until_done(svc)

        starts = [r for r in caplog.records if r.message == "backfill.region.start"]
        finishes = [r for r in caplog.records if r.message == "backfill.region.finish"]
        assert len(starts) == 1
        assert starts[0].region_id == _REGION_A  # type: ignore[attr-defined]
        assert len(finishes) == 1
        rec = finishes[0]
        assert rec.region_id == _REGION_A  # type: ignore[attr-defined]
        assert rec.total_rows == 2  # type: ignore[attr-defined]
        assert rec.duration_ms >= 0  # type: ignore[attr-defined]
        assert rec.cancelled is False  # type: ignore[attr-defined]

    def test_single_region_gets_start_finish_adr064(self, caplog: pytest.LogCaptureFixture) -> None:
        """ADR-064: single-region pass — only regions[0] gets start+finish log."""
        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)], _REGION_B: [_make_row(10)]},
            regions=[_REGION_A, _REGION_B],
        )
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.start(stop)
            _wait_until_done(svc)

        start_regions = {
            r.region_id for r in caplog.records  # type: ignore[attr-defined]
            if r.message == "backfill.region.start"
        }
        finish_regions = {
            r.region_id for r in caplog.records  # type: ignore[attr-defined]
            if r.message == "backfill.region.finish"
        }
        assert start_regions == {_REGION_A}
        assert finish_regions == {_REGION_A}


class TestRegionPageLog:
    """Invariant 4: backfill.region.page logged per-page."""

    def test_page_logged_per_page(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fake fetcher that triggers page_callback 3 times → 3 page events."""
        page_rows = [[_make_row(1)], [_make_row(2)], [_make_row(3)]]

        class MultiPageFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                for page_num, rows in enumerate(page_rows, start=1):
                    if page_callback is not None:
                        page_callback(page_num, len(rows))
                    yield from rows

        svc = BackfillService(
            fetcher=MultiPageFetcher(),  # type: ignore[arg-type]
            lot_repo=FakeLotRepository(),
            config_source=FakeConfigSource(regions=[_REGION_A]),
            monitor_cycle=FakeMonitorCycleService(),
            event_bus=FakeEventBus(),
            clock=FakeClock(),
            state_repo=FakeStateRepository(),
            sleep_between_pages=0.0,
        )
        stop = threading.Event()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.start(stop)
            _wait_until_done(svc)

        page_logs = [r for r in caplog.records if r.message == "backfill.region.page"]
        assert len(page_logs) == 3
        page_nums = [r.page_num for r in page_logs]  # type: ignore[attr-defined]
        assert page_nums == [1, 2, 3]
        for rec in page_logs:
            assert rec.region_id == _REGION_A  # type: ignore[attr-defined]
            assert rec.rows_fetched == 1  # type: ignore[attr-defined]


class TestCancelLog:
    """Invariant 5: backfill.cancel.called logged on cancel()."""

    def test_cancel_logs_called(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, *_ = _make_service()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            svc.cancel()

        cancel_logs = [r for r in caplog.records if r.message == "backfill.cancel.called"]
        assert cancel_logs, "backfill.cancel.called must be logged"
        rec = cancel_logs[0]
        assert rec.was_running is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# fsm / k31 / 1iw — new invariant tests (spec 2026-06-04)
# ---------------------------------------------------------------------------

class TestMaxPagesInvariant:
    """Invariant 1: backfill делает ≤5 запросов страниц за прогон."""

    def test_max_pages_5_passed_to_iterate(self) -> None:
        """BackfillService передаёт max_pages=_BACKFILL_MAX_PAGES=5 в iterate."""
        svc, _lr, _mc, fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert fetcher.iterate_kwargs[0]["max_pages"] == 5

    def test_single_region_fetch_not_per_region(self) -> None:
        """Pocket не качается повторно для каждого региона (ADR-064 single-region pass)."""
        svc, _lr, _mc, fetcher = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)], _REGION_B: [_make_row(2)]},
            regions=[_REGION_A, _REGION_B],
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        # Only ONE iterate call — not one per region
        assert len(fetcher.iterate_calls) == 1


class TestGalkaInvariant:
    """Invariant 2: галка ставится только при полном успехе."""

    def test_galka_set_after_success(self) -> None:
        state = FakeStateRepository()
        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            state_repo=state,
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert state.get("backfill.done") is not None
        assert svc.is_done() is True

    def test_galka_not_set_after_cancel(self) -> None:
        barrier = threading.Barrier(2)
        cancel_issued = threading.Event()

        class SlowFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                barrier.wait()
                cancel_issued.wait(timeout=5.0)
                return iter([])

        state = FakeStateRepository()
        svc, *_ = _make_service(fetcher=SlowFetcher(), state_repo=state)  # type: ignore[call-overload]
        stop = threading.Event()
        svc.start(stop)
        barrier.wait()
        svc.cancel()
        cancel_issued.set()
        _wait_until_done(svc)

        assert state.get("backfill.done") is None
        assert svc.is_done() is False

    def test_galka_not_set_after_session_expired(self) -> None:
        from fis_monitor.domain.errors import SessionExpiredError as _SE

        class SessionExpiredFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                yield _make_row(1)
                raise _SE("expired")

        state = FakeStateRepository()
        svc, *_ = _make_service(fetcher=SessionExpiredFetcher(), state_repo=state)  # type: ignore[call-overload]
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert state.get("backfill.done") is None

    def test_galka_not_set_after_network_error(self) -> None:
        class NetworkErrorFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                if raise_on_network_error:
                    raise OSError("network unreachable")
                return iter([])

        state = FakeStateRepository()
        svc, *_ = _make_service(fetcher=NetworkErrorFetcher(), state_repo=state)  # type: ignore[call-overload]
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert state.get("backfill.done") is None


class TestDeltaTriggerPersisted:
    """Invariant 5: delta-trigger сравнивает с запомненным total, не с count_active."""

    def test_maybe_start_uses_persisted_total_not_db_count(self) -> None:
        """Если есть total_last=100, delta = site_total - 100 (не db_count)."""
        state = FakeStateRepository()
        state.set("backfill.total_last", "100")

        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(1)]},
            state_repo=state,
        )
        stop = threading.Event()

        # db_count=90 → old delta = site_total-db_count = 200-90=110 → trigger
        # total_last=100 → new delta = 200-100=100 → trigger (>3)
        # Both trigger here, but next test checks the threshold direction

        # With total_last=200, site_total=200 → delta=0 ≤ 3 → NO trigger
        state.set("backfill.total_last", "200")
        result = svc.maybe_start(_REGION_A, site_total=200, db_count=90, stop_event=stop)
        assert result is False, "delta=0 ≤ threshold — should not trigger"

    def test_negative_delta_updates_persisted_total(self) -> None:
        """Отрицательная дельта не триггерит, но обновляет запомненное значение."""
        state = FakeStateRepository()
        state.set("backfill.total_last", "100")

        svc, *_ = _make_service(state_repo=state)
        stop = threading.Event()

        result = svc.maybe_start(_REGION_A, site_total=80, db_count=100, stop_event=stop)
        assert result is False  # отрицательная дельта — нет триггера
        assert state.get("backfill.total_last") == "80"  # обновлено


class TestResumeInvariant:
    """Инвариант 4: start_resume всегда начинает со страницы 1.

    Формула count//20+1 отменена (k31).
    """

    def test_resume_always_starts_from_page_1(self) -> None:
        """start_resume() always starts from page 1 (no count//20+1 formula)."""
        fetcher = FakePaginatedListFetcher(rows_by_region={_REGION_A: [_make_row(1)]})
        state = FakeStateRepository()
        svc, *_ = _make_service(fetcher=fetcher, state_repo=state)
        stop = threading.Event()

        svc.start_resume(stop)
        _wait_until_done(svc)

        assert fetcher.iterate_kwargs[0]["page_start"] == 1
        assert fetcher.iterate_kwargs[0]["max_pages"] == 5


class TestNetworkErrorInvariant:
    """Инвариант 7: таймаут fetcher — _running=False, ошибка опубликована."""

    def test_network_error_sets_running_false(self) -> None:
        """Сетевая ошибка → backfill завершается, _running=False."""
        from fis_monitor.domain.models import SseCycleError

        class NetworkErrorFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                if raise_on_network_error:
                    raise OSError("timeout")
                return iter([])

        bus = FakeEventBus()
        state = FakeStateRepository()
        svc, *_ = _make_service(
            fetcher=NetworkErrorFetcher(),  # type: ignore[call-overload]
            event_bus=bus,
            state_repo=state,
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert not svc.is_running()
        assert state.get("backfill.done") is None

        error_events = [e for e in bus.published if isinstance(e, SseCycleError)]
        assert len(error_events) >= 1, "SseCycleError must be published on network error"


# ---------------------------------------------------------------------------
# Monthly window invariant (fsm spec 2026-06-04)
# ---------------------------------------------------------------------------

class TestMonthlyWindowInvariant:
    """Invariant 1: backfill saves only lots <=30 days old; early-stops on first older lot."""

    def _make_row_with_date(self, lot_id: int, date_create: datetime) -> ParsedListRow:
        return ParsedListRow(
            id=lot_id,
            cadastral_no=f"77:01:{lot_id:06d}:1",
            area_sqm=500,
            region=str(_REGION_A),
            municipality="Тест",
            land_category="Земли населённых пунктов",
            permitted_use="ИЖС",
            ogv="ДГИ",
            status="PUBLISHED",
            date_create=date_create,
            date_update=None,
        )

    def test_early_stop_on_old_lot_sets_galka(self) -> None:
        """First lot older than cutoff → early-stop → galka is set (success)."""
        clock_now = datetime(2026, 1, 1, tzinfo=UTC)

        new_row = self._make_row_with_date(1, clock_now)
        old_row = self._make_row_with_date(2, datetime(2025, 11, 1, tzinfo=UTC))

        class OrderedFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                yield new_row
                yield old_row

        state = FakeStateRepository()
        mc = FakeMonitorCycleService()
        lot_repo = FakeLotRepository()
        config = FakeConfigSource(regions=[_REGION_A])
        svc = BackfillService(
            fetcher=OrderedFetcher(),  # type: ignore[arg-type]
            lot_repo=lot_repo,
            config_source=config,
            monitor_cycle=mc,
            event_bus=FakeEventBus(),
            clock=FakeClock(now=clock_now),
            state_repo=state,
            sleep_between_pages=0.0,
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        # Only the new lot saved (old lot not processed)
        saved_ids = {c["lot_id"] for c in lot_repo.upsert_calls}
        assert 1 in saved_ids
        assert 2 not in saved_ids

        # Galka set despite early-stop (early-stop = success)
        assert state.get("backfill.done") is not None
        assert svc.is_done() is True

    def test_all_lots_within_window_all_saved(self) -> None:
        """All lots within 30-day window → all saved, galka set."""
        clock_now = datetime(2026, 1, 1, tzinfo=UTC)

        rows = [
            self._make_row_with_date(i, clock_now - timedelta(days=i))
            for i in range(5)
        ]

        class AllNewFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                yield from rows

        state = FakeStateRepository()
        svc, lot_repo, _mc, _f = _make_service(
            fetcher=AllNewFetcher(),  # type: ignore[call-overload]
            state_repo=state,
            clock=FakeClock(now=clock_now),
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert len(lot_repo.upsert_calls) == 5
        assert state.get("backfill.done") is not None


    def test_naive_date_create_treated_as_utc(self) -> None:
        """F6: naive date_create does not raise TypeError; assumed UTC for comparison."""
        clock_now = datetime(2026, 1, 1, tzinfo=UTC)
        # naive date well within 30-day window — should be saved
        naive_recent = self._make_row_with_date(10, datetime(2026, 1, 1, 0, 0, 0))
        # naive date older than 30 days — should trigger early-stop
        naive_old = self._make_row_with_date(11, datetime(2025, 11, 1, 0, 0, 0))

        class NaiveDateFetcher:
            def iterate(self, region, stop_event, *, sleep_between_pages=2.0,
                        per_page=None, max_pages=None, page_start=1, page_callback=None,
                        total_callback=None, raise_on_network_error=False,
                        raise_on_parse_error=False):
                yield naive_recent
                yield naive_old

        state = FakeStateRepository()
        lot_repo = FakeLotRepository()
        svc = BackfillService(
            fetcher=NaiveDateFetcher(),  # type: ignore[arg-type]
            lot_repo=lot_repo,
            config_source=FakeConfigSource(regions=[_REGION_A]),
            monitor_cycle=FakeMonitorCycleService(),
            event_bus=FakeEventBus(),
            clock=FakeClock(now=clock_now),
            state_repo=state,
            sleep_between_pages=0.0,
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        # naive_recent saved (within window), naive_old triggers early-stop
        saved_ids = {c["lot_id"] for c in lot_repo.upsert_calls}
        assert 10 in saved_ids, "recent naive lot must be saved"
        assert 11 not in saved_ids, "old naive lot must trigger early-stop (not saved)"
        # run completes successfully (galka set)
        assert state.get("backfill.done") is not None
    def test_galka_set_when_cap_reached_without_old_lot(self) -> None:
        """Safety cap (max_pages=5) reached without date-stop → also success (galka set)."""
        # This is covered by TestGalkaInvariant.test_galka_set_after_success;
        # here we verify the cap path explicitly reaches galka.
        state = FakeStateRepository()
        svc, *_ = _make_service(
            rows_by_region={_REGION_A: [_make_row(i) for i in range(10)]},
            state_repo=state,
        )
        stop = threading.Event()
        svc.start(stop)
        _wait_until_done(svc)

        assert state.get("backfill.done") is not None
