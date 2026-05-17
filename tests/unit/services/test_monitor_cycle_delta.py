"""Unit tests for MonitorCycleService delta-trigger integration (gektar_monitor-69n5).

Layer 2 — Application services.  All deps are fakes; no network or DB.

Invariants covered:
  1. Cold-start: count_active=0, total_count=300, len(rows)=20 → maybe_start called.
  2. Steady-state: count_active=300, total_count=300 → maybe_start returns False (below threshold).
  3. total_count=None 5 times → ERROR log delta_check.parse_failure; counter=5.
  4. total_count restores after None → counter reset.
  5. Shutdown: stop_event passed to maybe_start is the same object from run_forever.
  6. delta_check.fired INFO log contains region_id, total_count, db_count, len_hint, decision.
  7. Multiple regions → per-region miss counter (not shared).
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.models import (
    ParsedListPage,
    ParsedListRow,
    Settings,
)
from fis_monitor.services.monitor_cycle import MonitorCycleService
from tests.fakes.lot_repository import FakeLotRepository
from tests.unit.services.conftest import (
    MinimalClock,
    MinimalCyclesRepository,
    MinimalEnrichmentService,
    MinimalEventBus,
    MinimalHttpClient,
    MinimalNotifierDispatcher,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION = 77
_REGION_B = 50


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_parsed_row(lot_id: int) -> ParsedListRow:
    return ParsedListRow(
        id=lot_id,
        cadastral_no=f"77:01:{lot_id:08d}:1",
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


class FakeListParser:
    """Configurable ListParser fake — returns a ParsedListPage per call."""

    def __init__(
        self,
        rows: list[ParsedListRow] | None = None,
        total_count: int | None = 0,
        total_count_sequence: list[int | None] | None = None,
    ) -> None:
        self._rows = rows or []
        self._total_count = total_count
        self._sequence = total_count_sequence
        self._call_index = 0

    def parse(self, html: str) -> ParsedListPage:
        if self._sequence is not None:
            idx = min(self._call_index, len(self._sequence) - 1)
            tc = self._sequence[idx]
            self._call_index += 1
        else:
            tc = self._total_count
        return ParsedListPage(rows=self._rows, total_count=tc)


class _LocalConfigSource:
    """ConfigSource with per-regions Settings — specific to delta tests."""

    def __init__(self, regions: list[int] | None = None) -> None:
        self._settings = Settings(regions=regions or [_REGION])

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeBackfillService:
    """Full BackfillHandle fake — implements ALL protocol methods."""

    def __init__(self, maybe_start_returns: bool = False) -> None:
        self._maybe_start_returns = maybe_start_returns
        self.maybe_start_calls: list[dict[str, Any]] = []
        self.start_calls: list[Any] = []
        self.cancel_calls: int = 0
        self.status_calls: int = 0

    # -- BackfillHandle (used by MonitorCycleService) --

    def maybe_start(
        self,
        region_id: int,
        site_total: int | None,
        db_count: int,
        stop_event: threading.Event,
        *,
        len_parsed_hint: int = 0,
    ) -> bool:
        self.maybe_start_calls.append(
            {
                "region_id": region_id,
                "site_total": site_total,
                "db_count": db_count,
                "stop_event": stop_event,
                "len_parsed_hint": len_parsed_hint,
            }
        )
        return self._maybe_start_returns

    # -- Full BackfillService API (all methods exercised to catch API bugs) --

    def start(
        self,
        stop_event_external: threading.Event,
        regions: list[int] | None = None,
    ) -> bool:
        self.start_calls.append((stop_event_external, regions))
        return True

    def cancel(self) -> None:
        self.cancel_calls += 1

    def status(self) -> object:
        self.status_calls += 1
        return None

    def is_running(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service(
    *,
    rows: list[ParsedListRow] | None = None,
    total_count: int | None = 0,
    total_count_sequence: list[int | None] | None = None,
    count_active_value: int = 0,
    backfill_returns: bool = False,
    regions: list[int] | None = None,
) -> tuple[MonitorCycleService, FakeLotRepository, FakeBackfillService, threading.Event]:
    lot_repo = FakeLotRepository(count_active_value=count_active_value)
    backfill = FakeBackfillService(maybe_start_returns=backfill_returns)
    stop_event = threading.Event()

    svc = MonitorCycleService(
        http=MinimalHttpClient(),
        list_parser=FakeListParser(
            rows=rows,
            total_count=total_count,
            total_count_sequence=total_count_sequence,
        ),
        enrichment=MinimalEnrichmentService(),
        lot_repo=lot_repo,
        cycles_repo=MinimalCyclesRepository(),
        notifier_dispatcher=MinimalNotifierDispatcher(),
        event_bus=MinimalEventBus(),
        config_source=_LocalConfigSource(regions=regions),
        clock=MinimalClock(),
        cycle_progress_signal=threading.Event(),
        backfill=backfill,
    )
    # Store so tests can pass the same event to run_forever / check it later.
    return svc, lot_repo, backfill, stop_event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeltaTriggerColdStart:
    """Invariant 1: count_active=0, total_count=300, len(rows)=20 → maybe_start called."""

    def test_maybe_start_called_with_correct_args(self) -> None:
        rows = [_make_parsed_row(i) for i in range(20)]
        svc, _, backfill, stop_event = _make_service(
            rows=rows,
            total_count=300,
            count_active_value=0,
        )

        svc._stop_event = stop_event
        svc.run_cycle(_REGION)

        assert len(backfill.maybe_start_calls) == 1
        call = backfill.maybe_start_calls[0]
        assert call["region_id"] == _REGION
        assert call["site_total"] == 300
        assert call["db_count"] == 0
        assert call["len_parsed_hint"] == 20
        assert call["stop_event"] is stop_event

    def test_count_active_called_with_region_id(self) -> None:
        rows = [_make_parsed_row(i) for i in range(5)]
        svc, lot_repo, _, stop_event = _make_service(
            rows=rows,
            total_count=300,
            count_active_value=0,
        )
        svc._stop_event = stop_event
        svc.run_cycle(_REGION)

        assert _REGION in lot_repo.count_active_calls


class TestDeltaTriggerSteadyState:
    """Invariant 2: count_active=300, total_count=300 → maybe_start called (gate decides)."""

    def test_maybe_start_called_returns_false_below_threshold(self) -> None:
        rows = [_make_parsed_row(i) for i in range(20)]
        svc, _, backfill, stop_event = _make_service(
            rows=rows,
            total_count=300,
            count_active_value=300,
            backfill_returns=False,
        )
        svc._stop_event = stop_event
        svc.run_cycle(_REGION)

        # maybe_start is called; it returns False (BackfillService gate decides internally).
        assert len(backfill.maybe_start_calls) == 1
        assert backfill.maybe_start_calls[0]["db_count"] == 300
        assert backfill.maybe_start_calls[0]["site_total"] == 300


class TestParseMissCounter:
    """Invariants 3 & 4: total_count=None → counter; >=5 → ERROR log; restore → reset."""

    def test_no_maybe_start_when_total_count_none(self) -> None:
        svc, _, backfill, stop_event = _make_service(total_count=None)
        svc._stop_event = stop_event
        svc.run_cycle(_REGION)
        assert len(backfill.maybe_start_calls) == 0

    def test_counter_increments_per_none(self) -> None:
        svc, _, _, stop_event = _make_service(total_count=None)
        svc._stop_event = stop_event
        for _ in range(3):
            svc.run_cycle(_REGION)
        assert svc._parse_miss_counter.get(_REGION) == 3

    def test_warning_log_below_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, _, _, stop_event = _make_service(total_count=None)
        svc._stop_event = stop_event
        with caplog.at_level(logging.WARNING):
            svc.run_cycle(_REGION)  # miss=1, below threshold
        warn_recs = [
            r for r in caplog.records
            if r.message == "delta_check.parse_failure" and r.levelno == logging.WARNING
        ]
        assert warn_recs, "Expected delta_check.parse_failure WARNING for miss=1"
        rec = warn_recs[0]
        assert rec.region_id == _REGION  # type: ignore[attr-defined]
        assert rec.consecutive_miss_count == 1  # type: ignore[attr-defined]

    def test_error_log_at_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, _, _, stop_event = _make_service(total_count=None)
        svc._stop_event = stop_event
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                svc.run_cycle(_REGION)
        error_recs = [
            r for r in caplog.records
            if r.message == "delta_check.parse_failure" and r.levelno == logging.ERROR
        ]
        assert error_recs, "Expected delta_check.parse_failure ERROR at miss=5"
        rec = error_recs[0]
        assert rec.region_id == _REGION  # type: ignore[attr-defined]
        assert rec.consecutive_miss_count == 5  # type: ignore[attr-defined]

    def test_error_log_contains_region_and_miss_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc, _, _, stop_event = _make_service(total_count=None)
        svc._stop_event = stop_event
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                svc.run_cycle(_REGION)
        parse_fail_recs = [
            r for r in caplog.records if r.message == "delta_check.parse_failure"
        ]
        assert parse_fail_recs, "Expected delta_check.parse_failure records"
        last = parse_fail_recs[-1]
        assert last.region_id == _REGION  # type: ignore[attr-defined]
        assert last.consecutive_miss_count == 5  # type: ignore[attr-defined]

    def test_counter_resets_after_restore(self) -> None:
        # 3 misses, then total_count restores → counter cleared.
        sequence = [None, None, None, 300]
        svc, _, _, stop_event = _make_service(
            total_count_sequence=sequence,
            count_active_value=0,
        )
        svc._stop_event = stop_event
        for _ in range(4):
            svc.run_cycle(_REGION)

        assert svc._parse_miss_counter.get(_REGION, 0) == 0

    def test_maybe_start_called_after_restore(self) -> None:
        sequence = [None, None, None, 300]
        svc, _, backfill, stop_event = _make_service(
            total_count_sequence=sequence,
            count_active_value=0,
        )
        svc._stop_event = stop_event
        for _ in range(4):
            svc.run_cycle(_REGION)

        # maybe_start should be called only on the 4th cycle (restore).
        assert len(backfill.maybe_start_calls) == 1
        assert backfill.maybe_start_calls[0]["site_total"] == 300


class TestStopEventPassThrough:
    """Invariant 5: stop_event passed to maybe_start is the same object as run_forever receives."""

    def test_stop_event_identity(self) -> None:
        rows = [_make_parsed_row(i) for i in range(20)]
        svc, _, backfill, _ = _make_service(
            rows=rows,
            total_count=300,
            count_active_value=0,
        )

        captured_stop: list[threading.Event] = []
        original_maybe_start = backfill.maybe_start

        def _spy(
            region_id: int,
            site_total: int | None,
            db_count: int,
            stop_event: threading.Event,
            *,
            len_parsed_hint: int = 0,
        ) -> bool:
            captured_stop.append(stop_event)
            return original_maybe_start(
                region_id, site_total, db_count, stop_event, len_parsed_hint=len_parsed_hint
            )

        backfill.maybe_start = _spy  # type: ignore[method-assign]

        stop = threading.Event()
        # Patch _wait_for_next_pass to stop after first pass.
        svc._wait_for_next_pass = lambda ev, t: ev.set()  # type: ignore[method-assign]
        svc.run_forever(stop)

        assert len(captured_stop) >= 1
        assert all(ev is stop for ev in captured_stop)


class TestDeltaCheckFiredLog:
    """Invariant 6: delta_check.fired INFO log — structured extra fields."""

    def test_fired_log_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        rows = [_make_parsed_row(i) for i in range(5)]
        svc, _, _, stop_event = _make_service(
            rows=rows,
            total_count=200,
            count_active_value=10,
            backfill_returns=True,
        )
        svc._stop_event = stop_event

        with caplog.at_level(logging.INFO):
            svc.run_cycle(_REGION)

        fired = [r for r in caplog.records if r.message == "delta_check.fired"]
        assert fired, "Expected delta_check.fired INFO log"
        rec = fired[0]
        assert rec.levelno == logging.INFO
        assert rec.region_id == _REGION  # type: ignore[attr-defined]
        assert rec.total_upstream == 200  # type: ignore[attr-defined]
        assert rec.count_active == 10  # type: ignore[attr-defined]
        assert rec.delta == 190  # 200 - 10  # type: ignore[attr-defined]
        assert rec.decision == "trigger"  # type: ignore[attr-defined]

    def test_fired_decision_skip_when_no_trigger(self, caplog: pytest.LogCaptureFixture) -> None:
        rows = [_make_parsed_row(i) for i in range(5)]
        svc, _, _, stop_event = _make_service(
            rows=rows,
            total_count=200,
            count_active_value=10,
            backfill_returns=False,
        )
        svc._stop_event = stop_event

        with caplog.at_level(logging.INFO):
            svc.run_cycle(_REGION)

        fired = [r for r in caplog.records if r.message == "delta_check.fired"]
        assert fired
        assert fired[0].decision == "skip"  # type: ignore[attr-defined]


class TestPerRegionMissCounter:
    """Invariant 7: miss counter is per-region, not global."""

    def test_miss_counter_isolated_per_region(self) -> None:
        # Two-region config: region 77 gets None, region 50 gets 300.
        # Use per-call sequence: we interleave calls manually.
        svc_a, _, _, stop_a = _make_service(total_count=None)
        svc_b, _, _, stop_b = _make_service(
            total_count=300,
            count_active_value=0,
            regions=[_REGION_B],
        )

        svc_a._stop_event = stop_a
        svc_b._stop_event = stop_b

        for _ in range(3):
            svc_a.run_cycle(_REGION)
        svc_b.run_cycle(_REGION_B)

        assert svc_a._parse_miss_counter.get(_REGION, 0) == 3
        assert svc_a._parse_miss_counter.get(_REGION_B, 0) == 0

    def test_two_regions_independent_counters(self) -> None:
        """Service instance with two regions: miss counter is keyed by region_id."""
        sequence_77 = [None, None, None]

        svc, _, _, stop = _make_service(
            total_count_sequence=sequence_77,
            regions=[_REGION],
        )
        svc._stop_event = stop
        for _ in range(3):
            svc.run_cycle(_REGION)

        assert svc._parse_miss_counter.get(_REGION, 0) == 3
        # _REGION_B has never been cycled — counter must be absent.
        assert svc._parse_miss_counter.get(_REGION_B, 0) == 0


class TestAllFakeMethods:
    """Ensure FakeBackfillService all methods are callable (catches API mismatch bugs)."""

    def test_all_methods_callable(self) -> None:
        fake = FakeBackfillService()
        ev = threading.Event()

        result = fake.maybe_start(1, 100, 0, ev, len_parsed_hint=5)
        assert isinstance(result, bool)

        result2 = fake.start(ev, regions=[1])
        assert isinstance(result2, bool)

        fake.cancel()
        assert fake.cancel_calls == 1

        fake.status()
        assert fake.status_calls == 1

        assert fake.is_running() is False
