"""Unit tests: monitor cycle galka gate + awaiting_backfill status (k31).

Invariants covered:
  (3a) Без галки монитор не выполняет ни одного цикла и не публикует SseLotNew.
  (3b) SseStatus(state='awaiting_backfill') публикуется при первом входе в ожидание.
  (3c) С галкой — run_forever вызывает run_cycle (тёплый старт работает).

docs/architecture/09-test-strategy.md Layer 2:
  Unit, fake dependencies. No SQLite.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from fis_monitor.domain.models import SseLotNew, SseStatus
from fis_monitor.services.monitor_cycle import MonitorCycleService
from tests.fakes.state_repository import FakeStateRepository as _FakeStateRepository

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------




class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self) -> Any:
        raise NotImplementedError


class _FakeLotRepo:
    def count_active(self, region_ids: tuple[int, ...] = ()) -> int:
        return 0

    def latest_new_first_seen(self) -> datetime | None:
        return None

    def upsert(self, lot: Any, *, tracked: Any) -> Any:
        raise NotImplementedError

    def get(self, lot_id: int) -> Any:
        return None

    def list_active(self, *, limit: int, offset: int) -> list:
        return []

    def mark_seen(self, lot_ids: Any, at: Any) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: Any) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list:
        return []

    def get_last_known_id(self, region: int) -> Any:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class _FakeConfigSource:
    def current(self) -> Any:
        from fis_monitor.domain.models import Settings
        return Settings(regions=[1])

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class _CountingRunCycle:
    """Fake MonitorCycleService that counts run_cycle calls and stops after N."""

    def __init__(self, stop_event: threading.Event, max_calls: int = 1) -> None:
        self.run_cycle_count = 0
        self._max = max_calls
        self._stop_event = stop_event

    def run_cycle(self, region: int) -> None:
        self.run_cycle_count += 1
        if self.run_cycle_count >= self._max:
            self._stop_event.set()


def _build_service(
    *,
    state_done: bool = False,
    run_cycle_impl: _CountingRunCycle | None = None,
) -> tuple[MonitorCycleService, _FakeEventBus, _FakeStateRepository]:
    """Construct a MonitorCycleService with minimal fake DI."""
    bus = _FakeEventBus()
    state = _FakeStateRepository(initial={"backfill.done": "1"} if state_done else None)

    svc = MonitorCycleService(
        http=MagicMock(),
        list_parser=MagicMock(),
        enrichment=MagicMock(),
        lot_repo=_FakeLotRepo(),
        cycles_repo=MagicMock(),
        notifier_dispatcher=MagicMock(),
        event_bus=bus,
        config_source=_FakeConfigSource(),
        clock=_FakeClock(),
        cycle_progress_signal=threading.Event(),
        state_repo=state,
    )

    if run_cycle_impl is not None:
        # Monkey-patch run_cycle for gate tests
        svc.run_cycle = run_cycle_impl.run_cycle  # type: ignore[method-assign]

    return svc, bus, state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_cycle_without_galka() -> None:
    """Invariant 3a: без галки run_forever пропускает все циклы."""
    stop = threading.Event()
    svc, bus, _state = _build_service(state_done=False)

    # Publish awaiting_backfill then stop
    def _set_stop_after_first_iteration() -> None:
        import time
        time.sleep(0.05)
        stop.set()

    import threading as th
    t = th.Thread(target=_set_stop_after_first_iteration, daemon=True)
    t.start()
    svc.run_forever(stop)
    t.join()

    # No SseLotNew published (no cycles ran)
    lot_new_events = [e for e in bus.published if isinstance(e, SseLotNew)]
    assert len(lot_new_events) == 0


def test_awaiting_backfill_published_without_galka() -> None:
    """Invariant 3b: SseStatus(state='awaiting_backfill') публикуется при ожидании."""
    stop = threading.Event()
    svc, bus, _state = _build_service(state_done=False)

    import threading as th
    import time

    def _stopper() -> None:
        time.sleep(0.05)
        stop.set()

    t = th.Thread(target=_stopper, daemon=True)
    t.start()
    svc.run_forever(stop)
    t.join()

    awaiting = [e for e in bus.published
                if isinstance(e, SseStatus) and e.state == "awaiting_backfill"]
    assert len(awaiting) >= 1, "SseStatus(state='awaiting_backfill') must be published"


def test_cycle_runs_with_galka() -> None:
    """Invariant 3c: с галкой монитор запускает циклы (тёплый старт)."""
    stop = threading.Event()
    counter = _CountingRunCycle(stop_event=stop, max_calls=1)
    svc, _bus, _state = _build_service(state_done=True)
    svc.run_cycle = counter.run_cycle  # type: ignore[method-assign]

    import threading as th
    import time

    def _stopper() -> None:
        time.sleep(0.5)
        stop.set()

    t = th.Thread(target=_stopper, daemon=True)
    t.start()
    svc.run_forever(stop)
    t.join(timeout=1.0)

    assert counter.run_cycle_count >= 1, "run_cycle must be called when galka is set"
