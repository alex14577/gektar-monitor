"""Layer 2 — service: backfill SSE publishes is_backfill=True (dr21).

Invariant covered:
  (3) BackfillService publishes SseLotNew with is_backfill=True for new lots.

Live-path (BrowserSseNotifier) publishes without is_backfill (default=False) —
that invariant is already covered in test_backfill_sse.py (invariant 5) and by
browser_sse_notifier tests; no duplication here.

docs/architecture/09-test-strategy.md Layer 2:
  Unit, pure fakes — no network/DB.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.models import (
    LotUpsertResult,
    ParsedListRow,
    Settings,
    SseLotNew,
)
from fis_monitor.services.backfill import BackfillService

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_REGION_A = 77


# ---------------------------------------------------------------------------
# Fakes (minimal — re-use same pattern as test_backfill_sse.py)
# ---------------------------------------------------------------------------


def _make_row(lot_id: int) -> ParsedListRow:
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
        date_create=_NOW,
        date_update=_NOW,
    )


class _FakePaginatedListFetcher:
    def __init__(self, rows: list[ParsedListRow]) -> None:
        self._rows = rows

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 0.0,
        per_page: int | None = None,
        max_pages: int | None = None,
        page_callback: object = None,
    ) -> Iterator[ParsedListRow]:
        if page_callback is not None:
            page_callback(1, len(self._rows))  # type: ignore[operator]
        yield from self._rows


class _FakeLotRepository:
    def upsert(self, lot: Any, *, tracked: Any) -> LotUpsertResult:
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


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish(self, event: object) -> None:
        self.published.append(event)

    def subscribe(self) -> object:
        raise NotImplementedError


class _FakeMonitorCycle:
    def mark_region_in_backfill(self, region: int) -> None:
        pass

    def clear_region_in_backfill(self, region: int) -> None:
        pass

    def request_run_now(self) -> None:
        pass


class _FakeConfigSource:
    def current(self) -> Settings:
        return Settings(regions=[_REGION_A])

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run_backfill(rows: list[ParsedListRow]) -> list[SseLotNew]:
    bus = _FakeEventBus()
    svc = BackfillService(
        fetcher=_FakePaginatedListFetcher(rows),
        lot_repo=_FakeLotRepository(),
        config_source=_FakeConfigSource(),
        monitor_cycle=_FakeMonitorCycle(),
        event_bus=bus,
        sleep_between_pages=0.0,
    )
    stop = threading.Event()
    svc.start(stop, regions=[_REGION_A])
    deadline = time.monotonic() + 5.0
    while svc.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    return [e for e in bus.published if isinstance(e, SseLotNew)]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_backfill_publishes_sse_lot_new_with_is_backfill_true() -> None:
    """Invariant (3): BackfillService sets is_backfill=True on every SseLotNew."""
    events = _run_backfill([_make_row(1), _make_row(2)])

    assert len(events) == 2, f"expected 2 SseLotNew events, got {len(events)}"
    for ev in events:
        assert ev.is_backfill is True, (
            f"lot {ev.lot.id}: expected is_backfill=True, got {ev.is_backfill}"
        )
        assert ev.event == "lot.new", "event name must stay 'lot.new'"
