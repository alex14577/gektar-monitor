"""Unit tests for FullScanService with PaginatedListFetcher.

Tests the new paginated path in _fetch_region_ids_paginated: when a
PaginatedListFetcher is supplied, all pages are iterated and their ids
are collected. The existing single-page tests in test_full_scan_service.py
remain unchanged (backward-compat path).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import (
    CycleResult,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.full_scan import FullScanService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_REGION_A = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lot(lot_id: int, region: int = _REGION_A) -> Lot:
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


def _make_row(lot_id: int, region: int = _REGION_A) -> ParsedListRow:
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
# Fakes
# ---------------------------------------------------------------------------

class FakePaginatedListFetcher:
    """PaginatedListFetcher fake that yields rows by region across multiple pages."""

    def __init__(self, rows_by_region: dict[int, list[ParsedListRow]]) -> None:
        self._rows_by_region = rows_by_region
        self.iterate_calls: list[int] = []
        self.iterate_kwargs: list[dict] = []

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 0.0,
        per_page: int | None = None,
        max_pages: int | None = None,
    ) -> Iterator[ParsedListRow]:
        self.iterate_calls.append(region)
        self.iterate_kwargs.append({"per_page": per_page, "max_pages": max_pages})
        for row in self._rows_by_region.get(region, []):
            if stop_event.is_set():
                return
            yield row


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self, url: str, *, params: Any = None, headers: Any = None, timeout: float | None = None
    ) -> Any:
        self.calls.append(url)
        from fis_monitor.domain.models import HttpResponse
        return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)


class FakeListParser:
    def __init__(self, rows: list[ParsedListRow] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[str] = []

    def parse(self, html: str) -> list[ParsedListRow]:
        self.calls.append(html)
        return self._rows


class FakeLotRepository:
    def __init__(self, pages: dict[int, list[Lot]] | None = None) -> None:
        self.pages: dict[int, list[Lot]] = pages or {}
        self.mark_seen_calls: list[tuple] = []
        self.mark_inactive_calls: list[tuple] = []

    def upsert(self, lot: Any, *, tracked: Any) -> Any:
        raise NotImplementedError

    def get(self, lot_id: int) -> None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return self.pages.get(offset, [])

    def get_last_known_id(self, region: int) -> None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: Any, at: datetime) -> None:
        self.mark_seen_calls.append((list(lot_ids), at))

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        self.mark_inactive_calls.append((lot_id, reason, at))

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


class FakeConfigSource:
    def __init__(self, regions: list[int] | None = None) -> None:
        self._settings = Settings(regions=regions or [_REGION_A])

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self) -> Any:
        raise NotImplementedError


class FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _make_service(
    *,
    paginated_fetcher: FakePaginatedListFetcher | None = None,
    lot_repo: FakeLotRepository | None = None,
    config_source: FakeConfigSource | None = None,
    single_page_rows: list[ParsedListRow] | None = None,
    batch_size: int = 50,
) -> tuple[FullScanService, FakeHttpClient, FakeLotRepository]:
    http = FakeHttpClient()
    list_parser = FakeListParser(rows=single_page_rows or [])
    lot_repo = lot_repo or FakeLotRepository()
    cycles_repo = FakeCyclesRepository()
    config_source = config_source or FakeConfigSource()

    svc = FullScanService(
        http=http,
        list_parser=list_parser,
        lot_repo=lot_repo,
        cycles_repo=cycles_repo,
        config_source=config_source,
        clock=FakeClock(),
        event_bus=FakeEventBus(),
        cycle_progress_signal=threading.Event(),
        batch_size=batch_size,
        inter_batch_sleep_sec=0.0,
        paginated_fetcher=paginated_fetcher,  # type: ignore[arg-type]
    )
    return svc, http, lot_repo


# ---------------------------------------------------------------------------
# Test 1: paginated fetcher is used when supplied
# ---------------------------------------------------------------------------

class TestPaginatedFetcherIsUsed:
    def test_fetcher_iterate_called_per_region(self) -> None:
        """When paginated_fetcher is injected, iterate() is called for each region."""
        paginated = FakePaginatedListFetcher(
            rows_by_region={
                _REGION_A: [_make_row(1), _make_row(2)],
                _REGION_B: [_make_row(3)],
            }
        )
        config = FakeConfigSource(regions=[_REGION_A, _REGION_B])
        lot_repo = FakeLotRepository(pages={0: [_make_lot(1), _make_lot(2), _make_lot(3)]})

        svc, http, _ = _make_service(
            paginated_fetcher=paginated,
            lot_repo=lot_repo,
            config_source=config,
        )

        svc.run_once()

        assert set(paginated.iterate_calls) == {_REGION_A, _REGION_B}
        # HTTP client is NOT called when paginated fetcher is in use
        assert http.calls == []

    def test_iterate_called_with_per_page_50(self) -> None:
        """FullScanService passes per_page=50 (ADR-036: full walk with explicit page size)."""
        paginated = FakePaginatedListFetcher(
            rows_by_region={_REGION_A: [_make_row(1)]},
        )
        config = FakeConfigSource(regions=[_REGION_A])
        lot_repo = FakeLotRepository(pages={0: [_make_lot(1)]})

        svc, _, _ = _make_service(
            paginated_fetcher=paginated,
            lot_repo=lot_repo,
            config_source=config,
        )

        svc.run_once()

        assert paginated.iterate_kwargs[0]["per_page"] == 50
        assert paginated.iterate_kwargs[0]["max_pages"] is None

    def test_all_paginated_ids_collected(self) -> None:
        """IDs from all pages across all regions are collected for comparison."""
        paginated = FakePaginatedListFetcher(
            rows_by_region={
                _REGION_A: [_make_row(1), _make_row(2), _make_row(3)],
            }
        )
        config = FakeConfigSource(regions=[_REGION_A])
        # Active lot 4 is NOT in the paginated results → should be marked inactive
        lot_repo = FakeLotRepository(
            pages={0: [_make_lot(1), _make_lot(4)]}
        )

        svc, _, _ = _make_service(
            paginated_fetcher=paginated,
            lot_repo=lot_repo,
            config_source=config,
        )

        svc.run_once()

        inactive_ids = [call[0] for call in lot_repo.mark_inactive_calls]
        assert 4 in inactive_ids, f"Lot 4 should be inactive, got {inactive_ids}"
        assert 1 not in inactive_ids

    def test_fallback_to_single_page_when_no_fetcher(self) -> None:
        """Without paginated_fetcher, HTTP + ListParser is used (single page)."""
        single_rows = [_make_row(10)]
        lot_repo = FakeLotRepository(pages={0: [_make_lot(10), _make_lot(99)]})
        config = FakeConfigSource(regions=[_REGION_A])

        svc, http, repo = _make_service(
            paginated_fetcher=None,
            lot_repo=lot_repo,
            config_source=config,
            single_page_rows=single_rows,
        )

        svc.run_once()

        # HTTP WAS called (single-page path)
        assert len(http.calls) == 1
        # Lot 99 not in seen_ids → marked inactive
        inactive_ids = [c[0] for c in repo.mark_inactive_calls]
        assert 99 in inactive_ids


# ---------------------------------------------------------------------------
# Test 2: paginated ids cover mass-deactivation guard
# ---------------------------------------------------------------------------

class TestPaginatedMassDeactivationGuard:
    def test_empty_paginated_ids_aborts_scan(self) -> None:
        """If all paginated pages return empty, no lots are marked inactive."""
        paginated = FakePaginatedListFetcher(rows_by_region={})  # no rows for any region
        config = FakeConfigSource(regions=[_REGION_A])
        lot_repo = FakeLotRepository(pages={0: [_make_lot(1), _make_lot(2)]})

        svc, _, _ = _make_service(
            paginated_fetcher=paginated,
            lot_repo=lot_repo,
            config_source=config,
        )

        svc.run_once()

        # Abort guard: no mark_inactive calls
        assert lot_repo.mark_inactive_calls == []
