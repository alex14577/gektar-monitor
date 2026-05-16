"""Logging tests for MonitorCycleService DEBUG events (gektar_monitor-b9wq).

Covers structured DEBUG events emitted during run_cycle / _run_cycle_inner:
- monitor_cycle.cycle.start
- monitor_cycle.region.fetch.start / finish
- monitor_cycle.region.upsert
- monitor_cycle.cycle.finish
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.models import (
    CycleResult,
    HttpResponse,
    LotUpsertResult,
    ParsedListPage,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.filter_matcher import AllFiltersMatcher
from fis_monitor.services.monitor_cycle import MonitorCycleService

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION = 77
_LOGGER = "fis_monitor.services.monitor_cycle"


# ---------------------------------------------------------------------------
# Minimal fakes (only what run_cycle needs)
# ---------------------------------------------------------------------------


class _FakeHttp:
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


class _FakeEnrichment:
    def enrich_lots(self, lots: list[Any], *, max_workers: int) -> list[Any]:
        return list(lots)


class _FakeLotRepo:
    def upsert(self, lot: Any, *, tracked: Any) -> LotUpsertResult:
        return LotUpsertResult(was_new=False, changes=[])

    def get(self, lot_id: int) -> None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Any]:
        return []

    def get_last_known_id(self, region: int) -> None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: list[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


class _FakeCyclesRepo:
    def __init__(self) -> None:
        self._id = 1

    def open(self, region: int, at: datetime) -> int:
        cid = self._id
        self._id += 1
        return cid

    def close(self, cycle_id: int, result: CycleResult) -> None:
        pass

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


class _FakeDispatcher:
    def dispatch(self, lot: Any) -> None:
        pass


class _FakeEventBus:
    def publish(self, event: Any) -> None:
        pass

    def subscribe(self) -> Any:
        raise NotImplementedError


class _FakeConfigSource:
    def current(self) -> Settings:
        return Settings()

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


def _make_svc(**overrides: Any) -> MonitorCycleService:
    return MonitorCycleService(
        http=overrides.get("http", _FakeHttp()),
        list_parser=overrides.get("list_parser", _FakeListParser()),
        enrichment=overrides.get("enrichment", _FakeEnrichment()),
        lot_repo=overrides.get("lot_repo", _FakeLotRepo()),
        cycles_repo=overrides.get("cycles_repo", _FakeCyclesRepo()),
        notifier_dispatcher=overrides.get("notifier_dispatcher", _FakeDispatcher()),
        event_bus=overrides.get("event_bus", _FakeEventBus()),
        config_source=overrides.get("config_source", _FakeConfigSource()),
        clock=overrides.get("clock", _FakeClock()),
        cycle_progress_signal=overrides.get("cycle_progress_signal", threading.Event()),
        filter_matcher=AllFiltersMatcher([]),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_cycle_emits_cycle_start_debug(caplog: pytest.LogCaptureFixture) -> None:
    """monitor_cycle.cycle.start emitted with region_id extra."""
    svc = _make_svc()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == "monitor_cycle.cycle.start"]
    assert records, "expected monitor_cycle.cycle.start debug event"
    assert records[0].__dict__.get("region_id") == _REGION


def test_run_cycle_emits_fetch_start_debug(caplog: pytest.LogCaptureFixture) -> None:
    """monitor_cycle.region.fetch.start emitted with region_id + cycle_id."""
    svc = _make_svc()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == "monitor_cycle.region.fetch.start"]
    assert records, "expected monitor_cycle.region.fetch.start debug event"
    rec = records[0]
    assert rec.__dict__.get("region_id") == _REGION
    assert rec.__dict__.get("cycle_id") is not None


def test_run_cycle_emits_fetch_finish_debug(caplog: pytest.LogCaptureFixture) -> None:
    """monitor_cycle.region.fetch.finish emitted with http_status + duration_ms."""
    svc = _make_svc()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == "monitor_cycle.region.fetch.finish"]
    assert records, "expected monitor_cycle.region.fetch.finish debug event"
    rec = records[0]
    assert rec.__dict__.get("http_status") == 200
    assert "duration_ms" in rec.__dict__


def test_run_cycle_emits_cycle_finish_debug(caplog: pytest.LogCaptureFixture) -> None:
    """monitor_cycle.cycle.finish emitted with status + lots_fetched + new_lots."""
    svc = _make_svc()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == "monitor_cycle.cycle.finish"]
    assert records, "expected monitor_cycle.cycle.finish debug event"
    rec = records[0]
    assert rec.__dict__.get("status") == "ok"
    assert "lots_fetched" in rec.__dict__
    assert "new_lots" in rec.__dict__


def test_run_cycle_emits_upsert_debug_per_lot(caplog: pytest.LogCaptureFixture) -> None:
    """monitor_cycle.region.upsert emitted once per lot with region_id + lot_id + was_new."""
    row = ParsedListRow(
        id=101,
        cadastral_no="77:01:00000001:1",
        area_sqm=1000,
        region="77",
        municipality="Москва",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )
    svc = _make_svc(list_parser=_FakeListParser(rows=[row]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == "monitor_cycle.region.upsert"]
    assert len(records) == 1, "expected one upsert event per lot"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 101
    assert rec.__dict__.get("region_id") == _REGION
    assert "was_new" in rec.__dict__
