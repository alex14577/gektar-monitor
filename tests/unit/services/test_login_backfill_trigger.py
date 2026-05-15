"""Unit tests for the login-driven backfill auto-trigger (f5u race-condition fix).

ADR-032 / f5u: backfill must start ONLY after successful headed-login, not at
onboarding-completion time.  The Playwright headed-login takes 10-60 s; triggering
backfill immediately after onboarding-completion (before login succeeds) caused
ParseBugErrors from missing session cookies → 0 rows → historical data lost
permanently.

Coverage (6 tests):
  1. Backfill starts when login.succeeded AND onboarding==COMPLETED AND count_active==0.
  2. Backfill does NOT start if onboarding state is not COMPLETED (even if login succeeded).
  3. Backfill does NOT start if count_active > 0 (catalogue already populated).
  4. Backfill does NOT start on start_refresh() success (only headed login triggers it).
  5. Re-trigger idempotency: second login.succeeded with backfill already running
     → BackfillService.start() returns False (single-flight guard), callback is safe.
  6. Backfill does NOT start when login.success is False (failed login → no callback).

Layer: Application services (Layer 2 per docs/architecture/09-test-strategy.md).
All dependencies injected via Fake Protocol implementations.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import LoginOutcome, OnboardingState
from fis_monitor.services.login import LoginService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes (anti-mock §6: ALL Protocol methods implemented and called in test_all_fakes)
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock fake."""

    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 1000.0


class FakeLoginSession:
    """Fake LoginSession — blocks until released, returns configured outcome."""

    def __init__(
        self,
        outcome: LoginOutcome | None = None,
        refresh_outcome: LoginOutcome | None = None,
    ) -> None:
        self._outcome = outcome or LoginOutcome(success=True, cookies_updated=True, error=None)
        self._refresh_outcome = refresh_outcome or LoginOutcome(
            success=True, cookies_updated=True, error=None
        )
        self._release = threading.Event()
        self._open_called = False
        self._refresh_called = False
        self._cancel_called = False

    def open_headed_login(self, *, deadline: float) -> LoginOutcome:
        self._open_called = True
        self._release.wait(timeout=5.0)
        return self._outcome

    def silent_refresh(self, *, deadline: float) -> LoginOutcome:
        self._refresh_called = True
        self._release.wait(timeout=5.0)
        return self._refresh_outcome

    def cancel(self) -> None:
        self._cancel_called = True
        self._release.set()

    def release(self) -> None:
        """Unblock the worker thread (simulate login/refresh completed)."""
        self._release.set()


class FakeOnboardingService:
    """Fake OnboardingService — returns configurable state."""

    def __init__(self, state: OnboardingState = OnboardingState.COMPLETED) -> None:
        self._state = state
        self.current_calls: int = 0

    def current(self) -> OnboardingState:
        self.current_calls += 1
        return self._state

    def can_advance(self, from_state: OnboardingState, to_state: OnboardingState) -> bool:
        return True

    def advance(self, from_state: OnboardingState, to_state: OnboardingState) -> None:
        pass

    def skip_email(self) -> None:
        pass

    def url_for_current_step(self) -> str:
        return "/"


class FakeLotRepository:
    """Fake LotRepository — records count_active() calls."""

    def __init__(self, active_count: int = 0) -> None:
        self._active_count = active_count
        self.count_active_calls: int = 0

    def count_active(self) -> int:
        self.count_active_calls += 1
        return self._active_count


class FakeBackfillService:
    """Fake BackfillService — records start() calls; simulates single-flight."""

    def __init__(self, *, already_running: bool = False) -> None:
        self._running = already_running
        self.start_calls: int = 0

    def start(self, stop_event: object) -> bool:
        self.start_calls += 1
        if self._running:
            return False
        self._running = True
        return True

    def is_running(self) -> bool:
        return self._running


class FakeSupervisor:
    """Fake ThreadSupervisor — immediately executes the target with a stop_event."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self._stop = threading.Event()

    def start(self, name: str, target: Callable[..., object]) -> None:
        self.started.append(name)
        # Execute synchronously so the test can assert start_calls without racing.
        target(self._stop)


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL Fake methods
# ---------------------------------------------------------------------------


def test_all_fakes_all_methods_exercised() -> None:
    """All Fake methods called — prevents silent API mismatch at runtime."""
    sess = FakeLoginSession()
    sess.cancel()
    assert sess._cancel_called is True
    sess.release()
    r1 = sess.open_headed_login(deadline=300.0)
    assert isinstance(r1, LoginOutcome)
    r2 = sess.silent_refresh(deadline=30.0)
    assert isinstance(r2, LoginOutcome)

    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    assert onb.current() is OnboardingState.COMPLETED
    assert onb.url_for_current_step() == "/"
    onb.can_advance(OnboardingState.COMPLETED, OnboardingState.COMPLETED)
    onb.advance(OnboardingState.RECIPIENTS_SET, OnboardingState.COMPLETED)
    onb.skip_email()

    lot_repo = FakeLotRepository(active_count=0)
    assert lot_repo.count_active() == 0

    bf = FakeBackfillService()
    assert bf.start(threading.Event()) is True
    assert bf.is_running() is True

    sup = FakeSupervisor()
    sup.start("test", lambda _stop: None)
    assert "test" in sup.started


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_callback(
    onboarding: FakeOnboardingService,
    lot_repo: FakeLotRepository,
    backfill: FakeBackfillService,
    supervisor: FakeSupervisor,
    regions: list[int] | None = None,
) -> Callable[[LoginOutcome], None]:
    """Build the on_login_success closure mirroring composition.py logic."""
    from tests.factories import make_settings

    _settings = make_settings(regions=regions if regions is not None else [1, 2])
    _supervisor_cell: list[object] = [supervisor]

    def _trigger(_outcome: object) -> None:
        if onboarding.current() != OnboardingState.COMPLETED:
            return
        active = lot_repo.count_active()
        if active != 0:
            return
        if not _settings.regions:
            return
        sup = _supervisor_cell[0]
        if sup is None:
            return
        sup.start("backfill-auto", lambda stop: backfill.start(stop))  # type: ignore[union-attr]

    return _trigger


def _run_headed_login(
    session: FakeLoginSession,
    callback: Callable[[LoginOutcome], None] | None,
) -> LoginOutcome:
    """Start a headed login in a real thread, release it, and wait for result."""
    clock = FakeClock()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc = LoginService(
            login_session=session,
            clock=clock,
            executor=ex,
            on_login_success=callback,
        )
        handle = svc.start_login()
        session.release()
        return handle.future.result(timeout=5.0)


def _run_refresh(
    session: FakeLoginSession,
    callback: Callable[[LoginOutcome], None] | None,
) -> LoginOutcome:
    """Start a silent refresh in a real thread, release it, and wait for result."""
    clock = FakeClock()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc = LoginService(
            login_session=session,
            clock=clock,
            executor=ex,
            on_login_success=callback,
        )
        handle = svc.start_refresh()
        session.release()
        return handle.future.result(timeout=5.0)


# ---------------------------------------------------------------------------
# Test 1: Backfill starts on login.succeeded + onboarding.COMPLETED + count_active==0
# ---------------------------------------------------------------------------


def test_backfill_starts_after_successful_login_when_guards_pass() -> None:
    """Happy path: login success → all guards pass → backfill started exactly once."""
    session = FakeLoginSession(
        outcome=LoginOutcome(success=True, cookies_updated=True, error=None)
    )
    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    lot_repo = FakeLotRepository(active_count=0)
    backfill = FakeBackfillService()
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    outcome = _run_headed_login(session, cb)

    assert outcome.success is True
    assert supervisor.started == ["backfill-auto"]
    assert backfill.start_calls == 1


# ---------------------------------------------------------------------------
# Test 2: Backfill does NOT start if onboarding is not COMPLETED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        OnboardingState.NOT_STARTED,
        OnboardingState.REGIONS_SET,
        OnboardingState.SMTP_CONFIGURED,
        OnboardingState.RECIPIENTS_SET,
    ],
)
def test_backfill_not_started_when_onboarding_incomplete(state: OnboardingState) -> None:
    """Login success alone is not sufficient — onboarding must be COMPLETED."""
    session = FakeLoginSession(
        outcome=LoginOutcome(success=True, cookies_updated=True, error=None)
    )
    onb = FakeOnboardingService(state=state)
    lot_repo = FakeLotRepository(active_count=0)
    backfill = FakeBackfillService()
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    _run_headed_login(session, cb)

    assert supervisor.started == [], f"Expected no backfill for state={state}"
    assert backfill.start_calls == 0


# ---------------------------------------------------------------------------
# Test 3: Backfill does NOT start if count_active > 0
# ---------------------------------------------------------------------------


def test_backfill_not_started_when_catalogue_already_populated() -> None:
    """If count_active() > 0 the catalogue already exists — skip backfill."""
    session = FakeLoginSession(
        outcome=LoginOutcome(success=True, cookies_updated=True, error=None)
    )
    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    lot_repo = FakeLotRepository(active_count=42)  # catalogue not empty
    backfill = FakeBackfillService()
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    _run_headed_login(session, cb)

    assert supervisor.started == []
    assert backfill.start_calls == 0


# ---------------------------------------------------------------------------
# Test 4: start_refresh() success does NOT trigger on_login_success callback
# ---------------------------------------------------------------------------


def test_silent_refresh_does_not_trigger_backfill() -> None:
    """start_refresh() is NOT a headed login — callback must NOT fire on refresh."""
    session = FakeLoginSession(
        refresh_outcome=LoginOutcome(success=True, cookies_updated=True, error=None)
    )
    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    lot_repo = FakeLotRepository(active_count=0)
    backfill = FakeBackfillService()
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    outcome = _run_refresh(session, cb)

    assert outcome.success is True
    # Callback must NOT have been invoked for silent refresh.
    assert supervisor.started == []
    assert backfill.start_calls == 0


# ---------------------------------------------------------------------------
# Test 5: Re-trigger idempotency — second login.succeeded with backfill running
# ---------------------------------------------------------------------------


def test_backfill_not_started_twice_when_already_running() -> None:
    """Second login.succeeded with backfill already in-flight → start() returns False."""
    session = FakeLoginSession(
        outcome=LoginOutcome(success=True, cookies_updated=True, error=None)
    )
    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    lot_repo = FakeLotRepository(active_count=0)
    # Simulate first backfill already started (running=True).
    backfill = FakeBackfillService(already_running=True)
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    _run_headed_login(session, cb)

    # Supervisor did enqueue "backfill-auto" (it always calls start via lambda),
    # but BackfillService.start() returned False (single-flight guard).
    assert supervisor.started == ["backfill-auto"]  # supervisor was called
    assert backfill.start_calls == 1  # start() was called once
    # The first call returned False because already_running=True.
    # No second backfill started — single-flight invariant holds.


# ---------------------------------------------------------------------------
# Test 6: Failed login → on_login_success callback must NOT fire
# ---------------------------------------------------------------------------


def test_backfill_not_started_on_failed_login() -> None:
    """outcome.success=False → the on_login_success guard blocks callback; no backfill."""
    session = FakeLoginSession(
        outcome=LoginOutcome(success=False, cookies_updated=False, error="login_timeout")
    )
    onb = FakeOnboardingService(state=OnboardingState.COMPLETED)
    lot_repo = FakeLotRepository(active_count=0)
    backfill = FakeBackfillService()
    supervisor = FakeSupervisor()

    cb = _make_callback(onb, lot_repo, backfill, supervisor)
    outcome = _run_headed_login(session, cb)

    assert outcome.success is False
    # Callback must NOT have been invoked — failed login must never trigger backfill.
    assert supervisor.started == []
    assert backfill.start_calls == 0
