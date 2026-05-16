"""Logging tests for FullScanService DEBUG events (gektar_monitor-b9wq).

Covers:
- full_scan.region.start
- full_scan.region.finish (region_id, ids_count, pagination_completed)
- full_scan.removal_candidates.detected (total_seen_ids)
- full_scan.mark_inactive (lot_id, reason)
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import (
    CycleResult,
    HttpResponse,
    ParsedListPage,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.full_scan import FullScanService

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION = 42
_LOGGER = "fis_monitor.services.full_scan"


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _FakeHttp:
    def __init__(self, rows: list[ParsedListRow] | None = None) -> None:
        self._rows = rows or []

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)


class _FakeListParser:
    def __init__(self, rows: list[ParsedListRow] | None = None) -> None:
        self._rows = rows or []

    def parse(self, html: str) -> ParsedListPage:
        return ParsedListPage(rows=self._rows, total_count=len(self._rows))


class _FakeLotRepo:
    def __init__(self, active_lots: list[Lot] | None = None) -> None:
        self._active = active_lots or []
        self.marked_inactive: list[tuple[int, str]] = []

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return self._active[offset: offset + limit]

    def mark_seen(self, lot_ids: list[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        self.marked_inactive.append((lot_id, reason))

    def upsert(self, lot: Any, *, tracked: Any) -> Any:
        raise NotImplementedError

    def get(self, lot_id: int) -> None:
        return None

    def get_last_known_id(self, region: int) -> None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


class _FakeCyclesRepo:
    def open(self, region: int, at: datetime) -> int:
        return 1

    def close(self, cycle_id: int, result: CycleResult) -> None:
        pass

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


class _FakeConfigSource:
    def __init__(self, regions: list[int] | None = None) -> None:
        self._settings = Settings(regions=regions or [_REGION])

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class _FakeEventBus:
    def publish(self, event: Any) -> None:
        pass


def _make_svc(
    *,
    rows: list[ParsedListRow] | None = None,
    active_lots: list[Lot] | None = None,
    regions: list[int] | None = None,
) -> tuple[FullScanService, _FakeLotRepo]:
    lot_repo = _FakeLotRepo(active_lots=active_lots)
    svc = FullScanService(
        http=_FakeHttp(),
        list_parser=_FakeListParser(rows=rows or []),
        lot_repo=lot_repo,
        cycles_repo=_FakeCyclesRepo(),
        config_source=_FakeConfigSource(regions=regions),
        clock=_FakeClock(),
        event_bus=_FakeEventBus(),
        cycle_progress_signal=threading.Event(),
    )
    return svc, lot_repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_once_emits_region_start_debug(caplog: pytest.LogCaptureFixture) -> None:
    """full_scan.region.start emitted with region_id when scan starts per-region."""
    row = ParsedListRow(
        id=1001,
        cadastral_no="42:01:000001:1",
        area_sqm=500,
        region="42",
        municipality="Кемерово",
        land_category="Земли населённых пунктов",
        permitted_use="ЛПХ",
        ogv="ОМС",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )
    svc, _ = _make_svc(rows=[row])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_once()

    records = [r for r in caplog.records if r.getMessage() == "full_scan.region.start"]
    assert records, "expected full_scan.region.start"
    assert records[0].__dict__.get("region_id") == _REGION


def test_run_once_emits_region_finish_debug(caplog: pytest.LogCaptureFixture) -> None:
    """full_scan.region.finish emitted with ids_count + pagination_completed."""
    row = ParsedListRow(
        id=1002,
        cadastral_no="42:01:000002:1",
        area_sqm=500,
        region="42",
        municipality="Кемерово",
        land_category="Земли населённых пунктов",
        permitted_use="ЛПХ",
        ogv="ОМС",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )
    svc, _ = _make_svc(rows=[row])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_once()

    records = [r for r in caplog.records if r.getMessage() == "full_scan.region.finish"]
    assert records, "expected full_scan.region.finish"
    rec = records[0]
    assert "ids_count" in rec.__dict__
    assert "pagination_completed" in rec.__dict__
    assert rec.__dict__.get("region_id") == _REGION


def test_run_once_emits_removal_candidates_detected(caplog: pytest.LogCaptureFixture) -> None:
    """full_scan.removal_candidates.detected emitted with total_seen_ids > 0."""
    row = ParsedListRow(
        id=2001,
        cadastral_no="42:01:002001:1",
        area_sqm=500,
        region="42",
        municipality="Кемерово",
        land_category="Земли населённых пунктов",
        permitted_use="ЛПХ",
        ogv="ОМС",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )
    svc, _ = _make_svc(rows=[row])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_once()

    event_name = "full_scan.removal_candidates.detected"
    records = [r for r in caplog.records if r.getMessage() == event_name]
    assert records, "expected full_scan.removal_candidates.detected"
    assert records[0].__dict__.get("total_seen_ids") == 1


def test_run_once_emits_mark_inactive_debug(caplog: pytest.LogCaptureFixture) -> None:
    """full_scan.mark_inactive emitted with lot_id + reason for each lot absent from seen_ids."""
    from tests.factories import make_lot

    # One lot in DB, NOT in the seen-ids from the scan → should be deactivated.
    absent_lot = make_lot(id=9999, region_id=_REGION)
    # Provide a different lot on the page so seen_ids is non-empty (avoids abort).
    page_row = ParsedListRow(
        id=8888,
        cadastral_no="42:01:008888:1",
        area_sqm=500,
        region="42",
        municipality="Кемерово",
        land_category="Земли населённых пунктов",
        permitted_use="ЛПХ",
        ogv="ОМС",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )
    svc, _lot_repo = _make_svc(rows=[page_row], active_lots=[absent_lot])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_once()

    # mark_inactive is logged at INFO level in the implementation
    records = [r for r in caplog.records if r.getMessage() == "full_scan.mark_inactive"]
    assert records, "expected full_scan.mark_inactive event for absent lot"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 9999
    assert rec.__dict__.get("reason") == "full_scan_missing"
