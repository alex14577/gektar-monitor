"""Integration tests for MonitorCycleService + FilterMatcher.

Verifies that the filter gate correctly suppresses dispatcher.dispatch for
lots that do not match the configured rf_subjects filter, while passing
through lots that do match (or when no filter is set).

Note: SseLotNew is no longer published directly from MonitorCycleService.
It flows exclusively via BrowserSseNotifier.send() → EventBus (Dispatcher
SSOT, ADR-030). These tests use a FakeNotifierDispatcher, so bus.published
will contain zero SseLotNew events; correctness is verified via
dispatcher.dispatch_calls.

These tests exercise the full Step-5 upsert+notify pipeline via the
``MonitorCycleService`` using fake collaborators.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import (
    CycleResult,
    FiltersConfig,
    HttpResponse,
    LotPublicDTO,
    LotUpsertResult,
    ParsedListPage,
    ParsedListRow,
    Settings,
    SseLotNew,
    TrackedField,
)
from fis_monitor.services.filter_matcher import AllFiltersMatcher, RfSubjectFilterMatcher
from fis_monitor.services.monitor_cycle import MonitorCycleService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION = 27  # Хабаровский край


# ---------------------------------------------------------------------------
# Fakes — minimal re-implementations (not importing from test_monitor_cycle
# to keep this file self-contained and the test readable in isolation)
# ---------------------------------------------------------------------------

class FakeHttpClient:
    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)


class FakeListParser:
    def __init__(self, rows: list[ParsedListRow]) -> None:
        self._rows = rows

    def parse(self, html: str) -> ParsedListPage:
        return ParsedListPage(rows=self._rows, total_count=len(self._rows))


class FakeEnrichmentService:
    def __init__(self, lots: list[Lot]) -> None:
        self._lots = lots

    def enrich_lots(self, lots: Sequence[Lot], *, max_workers: int) -> list[Lot]:
        return self._lots


class FakeLotRepository:
    def __init__(self, was_new_for: set[int]) -> None:
        self._was_new_for = was_new_for
        self.upsert_calls: list[Lot] = []

    def upsert(self, lot: Lot, *, tracked: Sequence[TrackedField]) -> LotUpsertResult:
        self.upsert_calls.append(lot)
        return LotUpsertResult(was_new=lot.id in self._was_new_for, changes=[])

    def get(self, lot_id: int) -> Lot | None: return None
    def list_active(self, *, limit: int, offset: int) -> list[Lot]: return []
    def get_last_known_id(self, region: int) -> int | None: return None
    def set_last_known_id(self, region: int, value: int) -> None: pass
    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None: pass
    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None: pass
    def needing_enrichment(self, limit: int) -> list[int]: return []


class FakeCyclesRepository:
    def __init__(self) -> None:
        self._next_id = 1
        self.close_calls: list[CycleResult] = []

    def open(self, region: int, at: datetime) -> int:
        cid = self._next_id
        self._next_id += 1
        return cid

    def close(self, cycle_id: int, result: CycleResult) -> None:
        self.close_calls.append(result)

    def list_recent(self, limit: int) -> list[CycleResult]: return []


class FakeNotifierDispatcher:
    def __init__(self) -> None:
        self.dispatch_calls: list[LotPublicDTO] = []

    def dispatch(self, lot: LotPublicDTO) -> None:
        self.dispatch_calls.append(lot)


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self) -> Any:
        raise NotImplementedError


class FakeConfigSource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeClock:
    def now(self) -> datetime: return _NOW
    def monotonic(self) -> float: return 0.0


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def _make_lot(lot_id: int, region_name: str) -> Lot:
    return Lot(
        id=lot_id,
        cadastral_no=f"{_REGION}:01:000{lot_id:04d}:1",
        area_sqm=1000,
        region=region_name,
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


def _make_parsed_row(lot_id: int, region_name: str) -> ParsedListRow:
    return ParsedListRow(
        id=lot_id,
        cadastral_no=f"{_REGION}:01:000{lot_id:04d}:1",
        area_sqm=1000,
        region=region_name,
        municipality="Тест",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
    )


def _build_service(
    lots: list[Lot],
    *,
    was_new_for: set[int],
    settings: Settings,
) -> tuple[MonitorCycleService, FakeNotifierDispatcher, FakeEventBus]:
    rows = [_make_parsed_row(lot.id, lot.region) for lot in lots]
    dispatcher = FakeNotifierDispatcher()
    event_bus = FakeEventBus()
    filter_matcher = AllFiltersMatcher([RfSubjectFilterMatcher()])

    svc = MonitorCycleService(
        http=FakeHttpClient(),
        list_parser=FakeListParser(rows),
        enrichment=FakeEnrichmentService(lots),
        lot_repo=FakeLotRepository(was_new_for=was_new_for),
        cycles_repo=FakeCyclesRepository(),
        notifier_dispatcher=dispatcher,
        event_bus=event_bus,
        config_source=FakeConfigSource(settings),
        clock=FakeClock(),
        cycle_progress_signal=threading.Event(),
        filter_matcher=filter_matcher,
    )
    return svc, dispatcher, event_bus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMonitorCycleWithFilter:
    """FilterMatcher integration: gate between upsert and emit/dispatch."""

    def test_new_lot_outside_filter_suppressed(self) -> None:
        """was_new=True lot from a filtered-out region → no publish, no dispatch."""
        # Хабаровский край = site-id 89; filter allows only Приморский (88)
        lot = _make_lot(1, "Хабаровский край")
        settings = Settings(
            filters=FiltersConfig(rf_subjects=[88]),
        )
        svc, dispatcher, event_bus = _build_service(
            [lot], was_new_for={1}, settings=settings
        )

        svc.run_cycle(_REGION)

        assert dispatcher.dispatch_calls == [], "dispatch must NOT be called for filtered-out lot"
        # No SseLotNew on bus either — Dispatcher SSOT means no direct publish
        sse_new_events = [e for e in event_bus.published if isinstance(e, SseLotNew)]
        assert sse_new_events == [], "SseLotNew must NOT be published for filtered-out lot"

    def test_new_lot_inside_filter_passes(self) -> None:
        """was_new=True lot whose region IS in rf_subjects → both called."""
        # Хабаровский край = site-id 89; filter includes 89
        lot = _make_lot(1, "Хабаровский край")
        settings = Settings(
            filters=FiltersConfig(rf_subjects=[89, 88]),
        )
        svc, dispatcher, event_bus = _build_service(
            [lot], was_new_for={1}, settings=settings
        )

        svc.run_cycle(_REGION)

        assert len(dispatcher.dispatch_calls) == 1, "dispatch must be called for matching lot"
        # SseLotNew is published by BrowserSseNotifier (inside real dispatcher),
        # not directly from monitor_cycle — FakeNotifierDispatcher won't produce it.
        sse_new_events = [e for e in event_bus.published if isinstance(e, SseLotNew)]
        assert len(sse_new_events) == 0, (
            "monitor_cycle must NOT publish SseLotNew directly (ADR-030)"
        )

    def test_new_lot_empty_filter_passes_through(self) -> None:
        """was_new=True with empty rf_subjects → both called (pass-through default)."""
        lot = _make_lot(1, "Хабаровский край")
        settings = Settings(
            filters=FiltersConfig(rf_subjects=[]),  # no filter = pass all
        )
        svc, dispatcher, event_bus = _build_service(
            [lot], was_new_for={1}, settings=settings
        )

        svc.run_cycle(_REGION)

        assert len(dispatcher.dispatch_calls) == 1
        # SseLotNew flows via BrowserSseNotifier (Dispatcher SSOT, ADR-030)
        sse_new_events = [e for e in event_bus.published if isinstance(e, SseLotNew)]
        assert len(sse_new_events) == 0

    def test_multiple_lots_only_matching_region_emits(self) -> None:
        """Two new lots from different regions; filter allows only one."""
        lot_khabarovsk = _make_lot(1, "Хабаровский край")  # site-id=89, filtered out
        lot_primorsky = _make_lot(2, "Приморский край")    # site-id=88, allowed

        settings = Settings(filters=FiltersConfig(rf_subjects=[88]))
        svc, dispatcher, event_bus = _build_service(
            [lot_khabarovsk, lot_primorsky],
            was_new_for={1, 2},
            settings=settings,
        )

        svc.run_cycle(_REGION)

        # Only Primorsky lot dispatched
        assert len(dispatcher.dispatch_calls) == 1
        assert dispatcher.dispatch_calls[0].id == 2

        # SseLotNew flows via BrowserSseNotifier (Dispatcher SSOT, ADR-030);
        # FakeNotifierDispatcher does not publish to the bus.
        sse_new_events = [e for e in event_bus.published if isinstance(e, SseLotNew)]
        assert len(sse_new_events) == 0

    def test_not_new_lots_not_dispatched_regardless(self) -> None:
        """was_new=False (existing lot, no changes) → dispatch never called regardless of filter."""
        lot = _make_lot(1, "Приморский край")
        settings = Settings(filters=FiltersConfig(rf_subjects=[]))  # pass-through
        svc, dispatcher, event_bus = _build_service(
            [lot], was_new_for=set(), settings=settings  # not new
        )

        svc.run_cycle(_REGION)

        assert dispatcher.dispatch_calls == []
        sse_new_events = [e for e in event_bus.published if isinstance(e, SseLotNew)]
        assert sse_new_events == []
