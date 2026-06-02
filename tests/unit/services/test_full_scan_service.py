"""Unit tests for FullScanService.

Covers removal-detection logic: fetch → compare → mark_seen / mark_inactive.
All external dependencies are replaced with fully-callable fakes (not just
isinstance-checked stubs) per the project's fake-impl invariant.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.errors import UpstreamError
from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import (
    CycleResult,
    HttpResponse,
    ParsedListPage,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.full_scan import FullScanService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION_A = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lot(lot_id: int, region: int = _REGION_A) -> Lot:
    """Return a minimal active Lot with the given id."""
    return Lot(
        id=lot_id,
        cadastral_no=f"{region}:01:{lot_id:06d}:1",
        area_sqm=1000,
        region=str(region),
        municipality="Тест",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=_NOW,
        last_seen=_NOW,
        detail_fetched_at=None,
        enrichment_status="done",
        last_seen_at=_NOW,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
    )


def _make_parsed_row(lot_id: int, region: int = _REGION_A) -> ParsedListRow:
    return ParsedListRow(
        id=lot_id,
        cadastral_no=f"{region}:01:{lot_id:06d}:1",
        area_sqm=1000,
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
# Fakes — all methods are fully callable (not just stubs)
# ---------------------------------------------------------------------------

class FakeHttpClient:
    """HttpClient fake supporting per-url configurable responses or errors."""

    def __init__(
        self,
        response_text: str = "<html/>",
        raises: Exception | None = None,
        per_url_raises: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._response_text = response_text
        self._raises = raises
        self._per_url_raises: dict[str, Exception] = per_url_raises or {}

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        exc = self._per_url_raises.get(url, self._raises)
        if exc is not None:
            raise exc
        return HttpResponse(
            status=200,
            text=self._response_text,
            headers={},
            final_url=url,
        )


class FakeListParser:
    """ListParser fake returning configurable rows or raising."""

    def __init__(
        self,
        rows: list[ParsedListRow] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._rows = rows or []
        self._raises = raises

    def parse(self, html: str) -> ParsedListPage:
        self.calls.append(html)
        if self._raises is not None:
            raise self._raises
        return ParsedListPage(rows=self._rows, total_count=len(self._rows))


class FakeLotRepository:
    """LotRepository fake with configurable active lot pages.

    ``pages`` maps offset → list[Lot]. Missing offsets return [].
    All Protocol methods are callable.
    """

    def __init__(self, pages: dict[int, list[Lot]] | None = None) -> None:
        self.pages: dict[int, list[Lot]] = pages or {}
        self.mark_seen_calls: list[tuple[list[int], datetime]] = []
        self.mark_inactive_calls: list[tuple[int, str, datetime]] = []

    def upsert(self, lot: Lot, *, tracked: Any) -> Any:
        raise NotImplementedError("not used in full_scan tests")

    def get(self, lot_id: int) -> Lot | None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return self.pages.get(offset, [])

    def get_last_known_id(self, region: int) -> int | None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        self.mark_seen_calls.append((list(lot_ids), at))

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        self.mark_inactive_calls.append((lot_id, reason, at))

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


class FakeCyclesRepository:
    """CyclesRepository fake. All Protocol methods are callable."""

    def __init__(self) -> None:
        self._next_id = 1
        self.open_calls: list[tuple[int, datetime]] = []
        self.close_calls: list[tuple[int, CycleResult]] = []

    def open(self, region: int, at: datetime) -> int:
        self.open_calls.append((region, at))
        cycle_id = self._next_id
        self._next_id += 1
        return cycle_id

    def close(self, cycle_id: int, result: CycleResult) -> None:
        self.close_calls.append((cycle_id, result))

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


class FakeConfigSource:
    """ConfigSource fake returning a fixed Settings snapshot."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError("not used in full_scan tests")


class FakeEventBus:
    """EventBus fake tracking publish calls."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self) -> Any:
        raise NotImplementedError("not used in full_scan tests")


class FakeClock:
    """Clock fake returning a fixed timestamp."""

    def __init__(self, fixed: datetime = _NOW) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _make_service(
    *,
    http: FakeHttpClient | None = None,
    list_parser: FakeListParser | None = None,
    lot_repo: FakeLotRepository | None = None,
    cycles_repo: FakeCyclesRepository | None = None,
    config_source: FakeConfigSource | None = None,
    clock: FakeClock | None = None,
    event_bus: FakeEventBus | None = None,
    cycle_progress_signal: threading.Event | None = None,
    batch_size: int = 50,
    inter_batch_sleep_sec: float = 0.0,
) -> tuple[
    FullScanService,
    FakeHttpClient,
    FakeListParser,
    FakeLotRepository,
    FakeCyclesRepository,
    FakeConfigSource,
    FakeEventBus,
]:
    http = http or FakeHttpClient()
    list_parser = list_parser or FakeListParser()
    lot_repo = lot_repo or FakeLotRepository()
    cycles_repo = cycles_repo or FakeCyclesRepository()
    config_source = config_source or FakeConfigSource()
    clock = clock or FakeClock()
    event_bus = event_bus or FakeEventBus()
    signal = cycle_progress_signal or threading.Event()

    svc = FullScanService(
        http=http,
        list_parser=list_parser,
        lot_repo=lot_repo,
        cycles_repo=cycles_repo,
        config_source=config_source,
        clock=clock,
        event_bus=event_bus,
        cycle_progress_signal=signal,
        batch_size=batch_size,
        inter_batch_sleep_sec=inter_batch_sleep_sec,
    )
    return svc, http, list_parser, lot_repo, cycles_repo, config_source, event_bus


# ---------------------------------------------------------------------------
# Test 1: basic removal detection
# ---------------------------------------------------------------------------

class TestRunOnceMissingLot:
    """run_once with 1 region: seen=[1,2,3], active=[1,2,4] → mark_inactive(4)."""

    def test_mark_inactive_for_missing_lot(self) -> None:
        seen_rows = [
            _make_parsed_row(1),
            _make_parsed_row(2),
            _make_parsed_row(3),
        ]
        http = FakeHttpClient(response_text="<html/>")
        list_parser = FakeListParser(rows=seen_rows)

        # Active lots: 1, 2, 4 (lot 4 is absent from listing)
        active_lots = {0: [_make_lot(1), _make_lot(2), _make_lot(4)]}
        lot_repo = FakeLotRepository(pages=active_lots)

        settings = Settings(regions=[_REGION_A])
        config_source = FakeConfigSource(settings=settings)

        svc, *_ = _make_service(
            http=http,
            list_parser=list_parser,
            lot_repo=lot_repo,
            config_source=config_source,
        )

        # Act
        svc.run_once()

        # Assert — lot 4 marked inactive
        inactive_ids = [call[0] for call in lot_repo.mark_inactive_calls]
        assert inactive_ids == [4], f"Expected [4], got {inactive_ids}"

        # Assert — lots 1 and 2 marked seen (lot 4 absent, not in seen call)
        seen_id_sets = [
            set(call[0]) for call in lot_repo.mark_seen_calls
        ]
        all_seen = set().union(*seen_id_sets) if seen_id_sets else set()
        assert 1 in all_seen
        assert 2 in all_seen
        assert 4 not in all_seen

    def test_http_called_once_for_first_region(self) -> None:
        settings = Settings(regions=[_REGION_A, _REGION_B])
        config_source = FakeConfigSource(settings=settings)
        http = FakeHttpClient()
        list_parser = FakeListParser(rows=[_make_parsed_row(1)])
        lot_repo = FakeLotRepository(pages={0: [_make_lot(1)]})

        svc, *_ = _make_service(
            http=http,
            list_parser=list_parser,
            lot_repo=lot_repo,
            config_source=config_source,
        )
        svc.run_once()

        assert len(http.calls) == 1  # single fetch (ADR-064)


# ---------------------------------------------------------------------------
# Test 2: all HTTP errors → no mark_inactive (mass-deactivation guard)
# ---------------------------------------------------------------------------

class TestRunOnceAllRegionsFail:
    """When all regions fail (empty seen_ids) → run_once aborts, no mark_inactive."""

    def test_no_deactivation_on_total_failure(self) -> None:
        http = FakeHttpClient(raises=UpstreamError("timeout", category="timeout"))
        list_parser = FakeListParser()

        # Active lots exist; should NOT be deactivated
        lot_repo = FakeLotRepository(pages={0: [_make_lot(1), _make_lot(2)]})

        settings = Settings(regions=[_REGION_A, _REGION_B])
        config_source = FakeConfigSource(settings=settings)

        svc, *_ = _make_service(
            http=http,
            list_parser=list_parser,
            lot_repo=lot_repo,
            config_source=config_source,
        )

        svc.run_once()

        assert lot_repo.mark_inactive_calls == [], (
            "mark_inactive must not be called when all regions fail"
        )
        assert lot_repo.mark_seen_calls == [], (
            "mark_seen must not be called when scan aborted"
        )


# ---------------------------------------------------------------------------
# Test 3: run_forever exits immediately when stop_event is pre-set
# ---------------------------------------------------------------------------

class TestRunForeverExitsImmediately:
    """run_forever with a pre-set stop_event exits without calling run_once."""

    def test_exits_without_run_once(self) -> None:
        svc, http, _list_parser, lot_repo, *_ = _make_service()

        stop_event = threading.Event()
        stop_event.set()

        svc.run_forever(stop_event)

        # HTTP was never called → run_once was never called
        assert http.calls == []
        assert lot_repo.mark_inactive_calls == []


# ---------------------------------------------------------------------------
# Test 4: batching — offset increments correctly across pages
# ---------------------------------------------------------------------------

class TestBatching:
    """list_active is called with incrementing offsets; mark_inactive covers all missing."""

    def test_multiple_batches(self) -> None:
        batch_size = 2

        # 5 active lots across 3 pages (last page has 1 lot)
        pages = {
            0: [_make_lot(1), _make_lot(2)],
            2: [_make_lot(3), _make_lot(4)],
            4: [_make_lot(5)],
        }
        lot_repo = FakeLotRepository(pages=pages)

        # seen_ids: 1, 3, 5 — lots 2, 4 are missing
        seen_rows = [
            _make_parsed_row(1),
            _make_parsed_row(3),
            _make_parsed_row(5),
        ]
        list_parser = FakeListParser(rows=seen_rows)

        settings = Settings(regions=[_REGION_A])
        config_source = FakeConfigSource(settings=settings)

        svc, *_ = _make_service(
            list_parser=list_parser,
            lot_repo=lot_repo,
            config_source=config_source,
            batch_size=batch_size,
        )

        svc.run_once()

        inactive_ids = sorted(call[0] for call in lot_repo.mark_inactive_calls)
        assert inactive_ids == [2, 4], f"Expected [2, 4], got {inactive_ids}"

    def test_offset_increments_correctly(self) -> None:
        """list_active is called at offsets 0, batch_size, 2*batch_size."""
        batch_size = 3
        list_active_offsets: list[int] = []

        class TrackingLotRepo(FakeLotRepository):
            def list_active(self, *, limit: int, offset: int) -> list[Lot]:
                list_active_offsets.append(offset)
                return super().list_active(limit=limit, offset=offset)

        pages = {
            0: [_make_lot(1), _make_lot(2), _make_lot(3)],
            3: [_make_lot(4)],
        }
        lot_repo = TrackingLotRepo(pages=pages)

        seen_rows = [_make_parsed_row(i) for i in range(1, 5)]
        list_parser = FakeListParser(rows=seen_rows)

        svc, *_ = _make_service(
            list_parser=list_parser,
            lot_repo=lot_repo,
            batch_size=batch_size,
        )

        svc.run_once()

        # Should have called list_active at offsets 0, 3, 6 (last returns [])
        assert list_active_offsets == [0, 3, 6], (
            f"Expected offsets [0, 3, 6], got {list_active_offsets}"
        )




# ---------------------------------------------------------------------------
# Test 6: mid-scan shutdown — stop_event aborts batch loop early (B1/B2)
# ---------------------------------------------------------------------------

class TestMidScanShutdown:
    """If stop_event is set after the first batch is processed, remaining
    batches are skipped and no further mark_inactive calls are made."""

    def test_run_once_aborts_mid_scan_when_stop_event_set(self) -> None:
        """stop_event set on 2nd list_active call → 3rd batch never processed."""
        batch_size = 2
        stop_event = threading.Event()

        class StopOnSecondCall(FakeLotRepository):
            """Sets stop_event on the second call to list_active."""

            def __init__(self, pages: dict[int, list[Lot]]) -> None:
                super().__init__(pages=pages)
                self.list_active_call_count = 0

            def list_active(self, *, limit: int, offset: int) -> list[Lot]:
                self.list_active_call_count += 1
                if self.list_active_call_count == 2:
                    stop_event.set()
                return super().list_active(limit=limit, offset=offset)

        # 3 batches worth of active lots (6 lots, batch_size=2).
        # All lots are absent from seen_ids so each would produce mark_inactive
        # if processed — makes it easy to verify early termination by count.
        pages = {
            0: [_make_lot(1), _make_lot(2)],
            2: [_make_lot(3), _make_lot(4)],
            4: [_make_lot(5), _make_lot(6)],
        }
        repo = StopOnSecondCall(pages=pages)

        # seen_ids: only lot 99 — all active lots are "missing", so each batch
        # would call mark_inactive for every lot in that batch.
        seen_rows = [_make_parsed_row(99)]
        list_parser = FakeListParser(rows=seen_rows)
        settings = Settings(regions=[_REGION_A])
        config_source = FakeConfigSource(settings=settings)

        svc, *_ = _make_service(
            list_parser=list_parser,
            lot_repo=repo,
            config_source=config_source,
            batch_size=batch_size,
            inter_batch_sleep_sec=0.0,
        )

        # Act
        svc.run_once(stop_event=stop_event)

        # Assert: stop_event was set on the 2nd list_active call.
        # The top-of-loop check fires before fetching batch 3 → only 2 calls.
        assert repo.list_active_call_count == 2, (
            f"Expected exactly 2 list_active calls, got {repo.list_active_call_count}"
        )

        # Only the first batch's lots (1, 2) could have been marked inactive.
        # Batch 2 was fetched but stop_event was set during that call, so the
        # top-of-loop guard on the *next* iteration prevents batch 3.
        # The second batch itself is still processed (stop is checked at top
        # of the loop, before fetching — not after setting the event).
        inactive_ids = sorted(call[0] for call in repo.mark_inactive_calls)
        assert 5 not in inactive_ids, (
            f"Batch 3 should not have been processed; inactive_ids={inactive_ids}"
        )
        assert 6 not in inactive_ids, (
            f"Batch 3 should not have been processed; inactive_ids={inactive_ids}"
        )
