"""Unit tests for BackfillService SSE publishing (gektar_monitor-w8dr).

Invariants tested:
  5. BackfillService publishes SseLotNew ONLY for lots with was_new=True
     (bd-bi7i: backfill — это исторический догон, дубль не должен дёргать
     real-time эскалацию на фронте).
  6. Backfill does NOT call email Notifier at any point (no dispatcher/notifier dep).
  7. EventBus publish exception → backfill does not raise, logs warning, continues.
  8. Cancelled backfill (stop.is_set() True at publish time) → SseLotNew NOT published.
  9. Duplicate lot (was_new=False) → no SseLotNew published.
  10. SessionExpiredError from fetcher → SseSessionExpired published, remaining regions aborted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.errors import SessionExpiredError
from fis_monitor.domain.models import (
    LotUpsertResult,
    ParsedListRow,
    Settings,
    SseLotNew,
    SseSessionExpired,
)
from fis_monitor.services.backfill import BackfillService
from tests.fakes.clock import FakeClock
from tests.fakes.state_repository import FakeStateRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_REGION_A = 77


# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePaginatedListFetcher:
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
        page_start: int = 1,
        page_callback: object = None,
        total_callback: object = None,
        raise_on_network_error: bool = False,
        raise_on_parse_error: bool = False,
    ) -> Iterator[ParsedListRow]:
        if page_callback is not None:
            page_callback(1, len(self._rows))  # type: ignore[operator]
        yield from self._rows





class FakeLotRepository:
    def __init__(self, *, was_new_per_lot: dict[int, bool] | None = None) -> None:
        self.upsert_calls: list[int] = []
        # Mapping lot.id → was_new флаг. По умолчанию все upsert-ы считаются новыми
        # (исторически поведение фейка). Тесты на дедупликацию могут передать
        # явный словарь, чтобы эмулировать «лот уже есть в БД».
        self._was_new = was_new_per_lot or {}

    def upsert(self, lot: Any, *, tracked: Any) -> LotUpsertResult:
        self.upsert_calls.append(lot.id)
        was_new = self._was_new.get(lot.id, True)
        return LotUpsertResult(was_new=was_new, changes=[])

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


class FakeEventBus:
    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[object] = []
        self._raise = raise_on_publish
        self.publish_call_count: int = 0

    def publish(self, event: object) -> None:
        self.publish_call_count += 1
        if self._raise:
            raise RuntimeError("bus overflow")
        self.published.append(event)

    def subscribe(self) -> object:
        raise NotImplementedError


class FakeMonitorCycle:
    def mark_region_in_backfill(self, region: int) -> None:
        pass

    def clear_region_in_backfill(self, region: int) -> None:
        pass

    def request_run_now(self) -> None:
        pass


class FakeConfigSource:
    def current(self) -> Settings:
        return Settings(regions=[_REGION_A])

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory + run helper
# ---------------------------------------------------------------------------


def _make_service(
    rows: list[ParsedListRow],
    event_bus: FakeEventBus | None = None,
    lot_repo: FakeLotRepository | None = None,
) -> tuple[BackfillService, FakeLotRepository, FakeEventBus]:
    lot_repo = lot_repo or FakeLotRepository()
    bus = event_bus or FakeEventBus()
    svc = BackfillService(
        fetcher=FakePaginatedListFetcher(rows),
        lot_repo=lot_repo,
        config_source=FakeConfigSource(),
        monitor_cycle=FakeMonitorCycle(),
        event_bus=bus,
        clock=FakeClock(),
        state_repo=FakeStateRepository(),
        sleep_between_pages=0.0,
    )
    return svc, lot_repo, bus


def _run_sync(svc: BackfillService, regions: list[int] | None = None) -> None:
    stop = threading.Event()
    started = svc.start(stop, regions=regions)
    assert started
    deadline = time.monotonic() + 5.0
    while svc.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not svc.is_running(), "backfill did not finish in time"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_publishes_sse_lot_new_only_for_new_lots():
    """Invariant 5: SseLotNew published only for lots with was_new=True (bd-bi7i)."""
    rows = [_make_row(1), _make_row(2), _make_row(3)]
    svc, lot_repo, bus = _make_service(rows)

    _run_sync(svc)

    assert len(lot_repo.upsert_calls) == 3
    assert len(bus.published) == 3
    assert all(isinstance(e, SseLotNew) for e in bus.published)
    lot_ids = {e.lot.id for e in bus.published}  # type: ignore[union-attr]
    assert lot_ids == {1, 2, 3}


def test_backfill_skips_sse_for_duplicate_lots():
    """Invariant 9 (bd-bi7i): backfill upserts duplicate (was_new=False) → no SseLotNew.

    Раньше backfill публиковал SseLotNew безусловно — фронт получал «новый лот»
    на исторические записи и запускал escalationStart() (звук + чип «Громче
    через ...»). Теперь публикация фильтруется по upsert_result.was_new, как в
    monitor_cycle.py:597.
    """
    rows = [_make_row(1), _make_row(2), _make_row(3)]
    # Лот 2 — уже в БД (дубль из истории); лоты 1 и 3 — действительно новые.
    lot_repo = FakeLotRepository(was_new_per_lot={1: True, 2: False, 3: True})
    svc, lot_repo, bus = _make_service(rows, lot_repo=lot_repo)

    _run_sync(svc)

    assert len(lot_repo.upsert_calls) == 3, "все три лота upsert-нуты"
    published_ids = {e.lot.id for e in bus.published}  # type: ignore[union-attr]
    assert published_ids == {1, 3}, "SSE только для was_new=True лотов"


def test_backfill_does_not_depend_on_email_notifier():
    """Invariant 6: BackfillService has no notifier_dispatcher/email dependency."""
    rows = [_make_row(1)]
    svc, _, bus = _make_service(rows)

    _run_sync(svc)

    assert len(bus.published) == 1
    assert isinstance(bus.published[0], SseLotNew)
    assert not hasattr(svc, "_notifier_dispatcher")
    assert not hasattr(svc, "_dispatcher")
    assert not hasattr(svc, "_email_notifier")


def test_backfill_sse_publish_failure_does_not_raise(caplog):
    """Invariant 7: EventBus publish raises → backfill logs warning, continues."""
    rows = [_make_row(1), _make_row(2)]
    bus = FakeEventBus(raise_on_publish=True)
    svc, lot_repo, bus = _make_service(rows, event_bus=bus)

    with caplog.at_level(logging.WARNING, logger="fis_monitor.services.backfill"):
        _run_sync(svc)

    assert len(lot_repo.upsert_calls) == 2, "upserts must complete despite publish failure"
    assert "SseLotNew publish failed" in caplog.text
    assert bus.publish_call_count == 2
    assert len(bus.published) == 0


def test_backfill_cancelled_before_start_no_sse_published():
    """Invariant 8: stop_event already set before backfill processes rows → no SseLotNew."""
    stop_event = threading.Event()
    stop_event.set()  # pre-cancelled

    bus = FakeEventBus()
    rows = [_make_row(1), _make_row(2)]
    svc, _, bus = _make_service(rows, event_bus=bus)

    started = svc.start(stop_event, regions=[_REGION_A])
    assert started

    deadline = time.monotonic() + 5.0
    while svc.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)

    # Pre-cancelled backfill: row loop exits immediately after cancel check
    # so SseLotNew is never published (no upserts completed either)
    assert len(bus.published) == 0, "no SseLotNew should be published when pre-cancelled"


# ---------------------------------------------------------------------------
# Tests for SessionExpiredError handling (invariant 10, ADR-063)
# ---------------------------------------------------------------------------


class FakePaginatedListFetcherSessionExpired:
    """Yields rows for region A page 1, then raises SessionExpiredError on next call."""

    def __init__(self, rows_before_expiry: list[ParsedListRow]) -> None:
        self._rows = rows_before_expiry
        self._call_count = 0

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 0.0,
        per_page: int | None = None,
        max_pages: int | None = None,
        page_start: int = 1,
        page_callback: object = None,
        total_callback: object = None,
        raise_on_network_error: bool = False,
        raise_on_parse_error: bool = False,
    ) -> Iterator[ParsedListRow]:
        self._call_count += 1
        if self._call_count == 1:
            # First region: yield some rows, then raise SessionExpiredError
            yield from self._rows
            raise SessionExpiredError("session expired mid-iterate")
        # Second region: should never be reached
        raise AssertionError("second region should not be fetched after session expiry")


def test_backfill_session_expired_publishes_sse_session_expired() -> None:
    """Invariant 10a: SessionExpiredError → SseSessionExpired published once."""
    rows = [_make_row(1), _make_row(2)]
    bus = FakeEventBus()
    lot_repo = FakeLotRepository()
    svc = BackfillService(
        fetcher=FakePaginatedListFetcherSessionExpired(rows),
        lot_repo=lot_repo,
        config_source=FakeConfigSource(),
        monitor_cycle=FakeMonitorCycle(),
        event_bus=bus,
        clock=FakeClock(),
        state_repo=FakeStateRepository(),
        sleep_between_pages=0.0,
    )

    _run_sync(svc)

    session_expired_events = [e for e in bus.published if isinstance(e, SseSessionExpired)]
    assert len(session_expired_events) == 1


def test_backfill_session_expired_aborts_remaining_regions() -> None:
    """Invariant 10b: SessionExpiredError on region A → region B not processed."""
    rows = [_make_row(1)]
    bus = FakeEventBus()
    lot_repo = FakeLotRepository()

    class FakeConfigSourceTwoRegions:
        def current(self) -> Settings:
            return Settings(regions=[_REGION_A, 50])

        def subscribe(self, cb: Any) -> Any:
            raise NotImplementedError

    fetcher = FakePaginatedListFetcherSessionExpired(rows)
    svc = BackfillService(
        fetcher=fetcher,
        lot_repo=lot_repo,
        config_source=FakeConfigSourceTwoRegions(),
        monitor_cycle=FakeMonitorCycle(),
        event_bus=bus,
        clock=FakeClock(),
        state_repo=FakeStateRepository(),
        sleep_between_pages=0.0,
    )

    _run_sync(svc)

    # Only one iterate call (region A); region B never reached
    assert fetcher._call_count == 1
    session_expired_events = [e for e in bus.published if isinstance(e, SseSessionExpired)]
    assert len(session_expired_events) == 1
