"""Unit tests for make_login_success_callback (composition module).

Layer 2/3 — callback is extracted via make_login_success_callback factory;
all dependencies are faked (no I/O, no real DI graph).

Covered invariants (fplb):
1. Callback publishes SseStatus(state="active") on any successful headed login.
2. Callback publishes SseLoginSucceeded on any successful headed login.
3. Both SSE events are published EVEN WHEN onboarding is not COMPLETED
   (auth-chip recovery is independent of the backfill guard).
4. Both SSE events are published EVEN WHEN supervisor_cell is None
   (backfill is blocked, but UI recovery must not be).
5. If event_bus.publish raises, the exception is swallowed and does NOT
   propagate to the caller (defensive: UI recovery failure must not break
   the login flow).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fis_monitor.composition import make_login_success_callback
from fis_monitor.domain.models import OnboardingState, Settings, SseLoginSucceeded, SseStatus
from fis_monitor.infra.sse.bus import ThreadEventBus
from tests.fakes.clock import FakeClock
from tests.fakes.event_bus import FakeEventBus
from tests.fakes.lot_repository import FakeLotRepository

_BASE_NOW = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Minimal fakes for config_source, onboarding, backfill
# ---------------------------------------------------------------------------


class _FakeConfigSource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def current(self) -> Settings:
        return self._settings


class _FakeOnboarding:
    def __init__(self, state: OnboardingState = OnboardingState.COMPLETED) -> None:
        self._state = state

    def current(self) -> OnboardingState:
        return self._state


class _FakeBackfill:
    """Records calls to start()."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, stop: Any) -> None:
        self.started.append("started")


class _FakeSupervisor:
    """Records calls to supervisor.start()."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def start(self, name: str, task: Any) -> None:
        self.scheduled.append(name)


def _default_settings(*, regions: list[int] | None = None) -> Settings:
    """Build a minimal Settings snapshot suitable for callback tests."""
    return Settings(
        interval_minutes=5,
        regions=regions if regions is not None else [1],
    )


def _make_callback(
    *,
    clock: FakeClock | None = None,
    config_source: _FakeConfigSource | None = None,
    lot_repo: FakeLotRepository | None = None,
    event_bus: FakeEventBus | None = None,
    onboarding: _FakeOnboarding | None = None,
    backfill: _FakeBackfill | None = None,
    supervisor_cell: list[object] | None = None,
) -> Callable[[object], None]:
    """Factory helper — returns callback with convenient defaults."""
    return make_login_success_callback(  # type: ignore[return-value]
        clock=clock or FakeClock(_BASE_NOW),
        config_source=config_source or _FakeConfigSource(_default_settings()),
        lot_repo=lot_repo or FakeLotRepository(),
        event_bus=event_bus or FakeEventBus(),
        onboarding=onboarding or _FakeOnboarding(OnboardingState.COMPLETED),
        backfill=backfill or _FakeBackfill(),
        supervisor_cell=supervisor_cell if supervisor_cell is not None else [None],
    )


# ---------------------------------------------------------------------------
# Invariant 1 — SseStatus(state="active") is published on login success
# ---------------------------------------------------------------------------


def test_login_callback_publishes_sse_status_active() -> None:
    """Callback must publish SseStatus(state='active') on any headed-login success."""
    bus = FakeEventBus()
    cb = _make_callback(event_bus=bus)

    cb(None)

    status_events = [e for e in bus.published if isinstance(e, SseStatus)]
    assert len(status_events) == 1, f"Expected exactly one SseStatus, got {bus.published}"
    assert status_events[0].state == "active"


# ---------------------------------------------------------------------------
# Invariant 2 — SseLoginSucceeded is published on login success
# ---------------------------------------------------------------------------


def test_login_callback_publishes_login_succeeded() -> None:
    """Callback must publish SseLoginSucceeded on any headed-login success."""
    bus = FakeEventBus()
    cb = _make_callback(event_bus=bus)

    cb(None)

    login_events = [e for e in bus.published if isinstance(e, SseLoginSucceeded)]
    assert len(login_events) == 1, f"Expected exactly one SseLoginSucceeded, got {bus.published}"


# ---------------------------------------------------------------------------
# Invariant 3 — SSE published BEFORE onboarding guard (onboarding != COMPLETED)
# ---------------------------------------------------------------------------


def test_login_callback_publishes_before_onboarding_guard() -> None:
    """SSE events are published even when onboarding is not COMPLETED.

    The onboarding guard only blocks the backfill trigger — UI auth-chip
    recovery must not be gated by it.
    """
    bus = FakeEventBus()
    cb = _make_callback(
        event_bus=bus,
        onboarding=_FakeOnboarding(OnboardingState.REGIONS_SET),  # not COMPLETED
    )

    cb(None)

    assert any(isinstance(e, SseStatus) for e in bus.published), "SseStatus missing"
    assert any(isinstance(e, SseLoginSucceeded) for e in bus.published), "SseLoginSucceeded missing"


# ---------------------------------------------------------------------------
# Invariant 4 — SSE published when supervisor_cell is None (backfill blocked)
# ---------------------------------------------------------------------------


def test_login_callback_publishes_when_supervisor_unbound() -> None:
    """SSE events are published even when supervisor_cell[0] is None.

    supervisor=None means backfill won't start, but UI recovery is orthogonal.
    """
    bus = FakeEventBus()
    supervisor_cell: list[object] = [None]
    cb = _make_callback(
        event_bus=bus,
        supervisor_cell=supervisor_cell,
        onboarding=_FakeOnboarding(OnboardingState.COMPLETED),
        config_source=_FakeConfigSource(_default_settings(regions=[1])),
    )

    cb(None)

    assert any(isinstance(e, SseStatus) for e in bus.published), "SseStatus missing"
    assert any(isinstance(e, SseLoginSucceeded) for e in bus.published), "SseLoginSucceeded missing"


# ---------------------------------------------------------------------------
# Invariant 5 — publish failure is swallowed; caller never sees exception
# ---------------------------------------------------------------------------


def test_login_callback_publish_failure_does_not_break() -> None:
    """If event_bus.publish raises, the exception is swallowed (logged at WARNING).

    The backfill guard and supervisor.start() below the try-block must still
    execute — UI recovery failure must not abort the whole login callback.
    """

    class _BoomBus:
        def publish(self, event: object) -> None:
            raise RuntimeError("bus exploded")

    supervisor = _FakeSupervisor()
    supervisor_cell: list[object] = [supervisor]
    backfill = _FakeBackfill()

    cb = make_login_success_callback(  # type: ignore[assignment]
        clock=FakeClock(_BASE_NOW),
        config_source=_FakeConfigSource(_default_settings(regions=[1])),
        lot_repo=FakeLotRepository(),
        event_bus=_BoomBus(),  # type: ignore[arg-type]
        onboarding=_FakeOnboarding(OnboardingState.COMPLETED),
        backfill=backfill,
        supervisor_cell=supervisor_cell,
    )

    # Must NOT raise
    cb(None)

    # Backfill was still scheduled (guard code below the try-block ran)
    assert "backfill-auto" in supervisor.scheduled


# ---------------------------------------------------------------------------
# Invariant 6 — login.succeeded replay slot is evicted after publish (fplb)
# ---------------------------------------------------------------------------


def test_login_callback_evicts_login_succeeded_replay_slot() -> None:
    """After callback fires, the login.succeeded slot is absent from _last_normal.

    Uses a real ThreadEventBus so evict_normal_replay() (extension method,
    guarded by isinstance check) is actually called and the slot state can
    be inspected directly on _last_normal.
    """
    bus = ThreadEventBus()
    cb = make_login_success_callback(
        clock=FakeClock(_BASE_NOW),
        config_source=_FakeConfigSource(_default_settings()),
        lot_repo=FakeLotRepository(),
        event_bus=bus,
        onboarding=_FakeOnboarding(OnboardingState.COMPLETED),
        backfill=_FakeBackfill(),
        supervisor_cell=[None],
    )

    cb(None)

    # The slot must have been evicted — reconnecting SSE clients won't replay
    # the OOB-wipe fragment and stale-overwrite a fresh cycle.done result.
    assert "login.succeeded" not in bus._last_normal, (
        "login.succeeded slot should be evicted after publish to prevent "
        "stale-overwrite race on SSE reconnect"
    )
