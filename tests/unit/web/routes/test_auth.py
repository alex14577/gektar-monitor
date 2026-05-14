"""Unit tests for /auth routes.

Tests use TestClient + app.dependency_overrides with a FakeLoginService.
Anti-mock pattern: FakeLoginService implements ALL methods and has a dedicated
all-methods test.

Coverage:
  1. POST /auth/start → 202 Accepted on success.
  2. POST /auth/start while job running → 409 Conflict.
  3. POST /auth/cancel → 204 No Content (idempotent).
  4. GET /auth/status → 200 with valid JSON body.
  5. GET /auth/status reflects running=True and last_outcome.
  6. Rate limit: second POST /auth/start within 60s → 429.
  7. Rate limit: POST /auth/start after 60s → 202 again.
  8. FakeLoginService all-methods invocation (anti-mock §6).
  9. POST /auth/refresh → 202 Accepted on success.
 10. POST /auth/refresh → 409 when busy.
 11. POST /auth/refresh → 429 when rate-limited.
 12. POST /auth/refresh → 503 when no executor.
"""

from __future__ import annotations

from concurrent.futures import Future

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import LoginOutcome
from fis_monitor.services.login import (
    LoginBusyError,
    LoginJobHandle,
    LoginStatus,
)
from fis_monitor.web.deps import get_login
from fis_monitor.web.rate_limit import RateLimiter
from fis_monitor.web.routes.auth import router

# ---------------------------------------------------------------------------
# Fake LoginService (anti-mock)
# ---------------------------------------------------------------------------


class FakeLoginService:
    """Fake LoginService that implements ALL public methods of the real service.

    Configurable to simulate: idle, running, busy, no-executor (503), outcomes.
    ``refresh_busy`` controls whether ``start_refresh()`` raises LoginBusyError
    independently of the ``busy`` flag (allows testing refresh-specific 409).
    """

    def __init__(
        self,
        *,
        busy: bool = False,
        running: bool = False,
        last_outcome: LoginOutcome | None = None,
        no_executor: bool = False,
        refresh_busy: bool = False,
        refresh_no_executor: bool = False,
    ) -> None:
        self._busy = busy
        self._running = running
        self._last_outcome = last_outcome
        self._no_executor = no_executor
        self._refresh_busy = refresh_busy
        self._refresh_no_executor = refresh_no_executor
        self.start_called = False
        self.cancel_called = False
        self.status_called = False
        self.bind_executor_called = False
        self.refresh_called = False

    def start_login(self) -> LoginJobHandle:
        self.start_called = True
        if self._busy:
            raise LoginBusyError("Login already in progress")
        if self._no_executor:
            raise RuntimeError("LoginService: no executor bound — call bind_executor() first")
        f: Future[LoginOutcome] = Future()
        f.set_result(
            LoginOutcome(success=True, cookies_updated=True, error=None)
        )
        return LoginJobHandle(future=f)

    def start_refresh(self) -> LoginJobHandle:
        self.refresh_called = True
        if self._refresh_busy or self._busy:
            raise LoginBusyError("Login/refresh already in progress")
        if self._refresh_no_executor or self._no_executor:
            raise RuntimeError("LoginService: no executor bound — call bind_executor() first")
        f: Future[LoginOutcome] = Future()
        f.set_result(
            LoginOutcome(success=True, cookies_updated=True, error=None)
        )
        return LoginJobHandle(future=f)

    def cancel_active_job(self) -> None:
        self.cancel_called = True

    def status(self) -> LoginStatus:
        self.status_called = True
        return LoginStatus(running=self._running, last_outcome=self._last_outcome)

    def bind_executor(self, executor: object) -> None:
        self.bind_executor_called = True


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_app(
    fake_svc: FakeLoginService,
    rate_limiter: RateLimiter | None = None,
    refresh_rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the auth router and injected fakes."""
    import fis_monitor.web.routes.auth as auth_module

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_login] = lambda: fake_svc

    if rate_limiter is not None:
        # Replace the module-level singleton for this test.
        auth_module._auth_rate_limiter = rate_limiter

    if refresh_rate_limiter is not None:
        auth_module._refresh_rate_limiter = refresh_rate_limiter

    return app


# ---------------------------------------------------------------------------
# Anti-mock: all methods of FakeLoginService
# ---------------------------------------------------------------------------


def test_fake_login_service_all_methods() -> None:
    """Invoke ALL methods to detect runtime API bugs in the fake."""
    fake = FakeLoginService()
    handle = fake.start_login()
    assert isinstance(handle, LoginJobHandle)
    assert fake.start_called is True

    handle2 = fake.start_refresh()
    assert isinstance(handle2, LoginJobHandle)
    assert fake.refresh_called is True

    fake.cancel_active_job()
    assert fake.cancel_called is True

    st = fake.status()
    assert isinstance(st, LoginStatus)
    assert fake.status_called is True

    fake.bind_executor(object())
    assert fake.bind_executor_called is True


# ---------------------------------------------------------------------------
# POST /auth/start
# ---------------------------------------------------------------------------


def test_auth_start_202() -> None:
    """POST /auth/start returns 202 when no job is running."""
    fake = FakeLoginService()
    # Unlimited rate limiter for this test.
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/start")
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"


def test_auth_start_409_when_busy() -> None:
    """POST /auth/start returns 409 when a job is already running."""
    fake = FakeLoginService(busy=True)
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/start")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /auth/cancel
# ---------------------------------------------------------------------------


def test_auth_cancel_204() -> None:
    """POST /auth/cancel returns 204 No Content."""
    fake = FakeLoginService()
    app = _build_app(fake)
    with TestClient(app) as client:
        resp = client.post("/auth/cancel")
    assert resp.status_code == 204
    assert fake.cancel_called is True


def test_auth_cancel_idempotent() -> None:
    """POST /auth/cancel is idempotent (returns 204 even when idle)."""
    fake = FakeLoginService(running=False)
    app = _build_app(fake)
    with TestClient(app) as client:
        resp1 = client.post("/auth/cancel")
        resp2 = client.post("/auth/cancel")
    assert resp1.status_code == 204
    assert resp2.status_code == 204


# ---------------------------------------------------------------------------
# GET /auth/status
# ---------------------------------------------------------------------------


def test_auth_status_idle() -> None:
    """GET /auth/status returns running=False, last_outcome=None when idle."""
    fake = FakeLoginService(running=False, last_outcome=None)
    app = _build_app(fake)
    with TestClient(app) as client:
        resp = client.get("/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["last_outcome"] is None


def test_auth_status_running() -> None:
    """GET /auth/status returns running=True when a job is in progress."""
    fake = FakeLoginService(running=True, last_outcome=None)
    app = _build_app(fake)
    with TestClient(app) as client:
        resp = client.get("/auth/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is True


def test_auth_status_with_outcome() -> None:
    """GET /auth/status serialises last_outcome when present."""
    outcome = LoginOutcome(success=True, cookies_updated=True, error=None)
    fake = FakeLoginService(running=False, last_outcome=outcome)
    app = _build_app(fake)
    with TestClient(app) as client:
        resp = client.get("/auth/status")
    body = resp.json()
    assert body["last_outcome"]["success"] is True
    assert body["last_outcome"]["error"] is None


# ---------------------------------------------------------------------------
# Rate limiting on /auth/start
# ---------------------------------------------------------------------------


def test_auth_start_rate_limit_429() -> None:
    """Second POST /auth/start within 60s (same IP) → 429."""
    fake = FakeLoginService()
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    app = _build_app(fake, rate_limiter=rl)

    # Inject a mock clock so we control 'now'.
    _now = [0.0]

    original_acquire = rl.acquire

    def _controlled_acquire(key: str, *, now: float) -> bool:
        return original_acquire(key, now=_now[0])

    rl.acquire = _controlled_acquire  # type: ignore[method-assign]

    with TestClient(app) as client:
        resp1 = client.post("/auth/start")
        assert resp1.status_code == 202

        # Still within 60s window.
        _now[0] = 30.0
        resp2 = client.post("/auth/start")
        assert resp2.status_code == 429


def test_auth_start_rate_limit_resets_after_window() -> None:
    """POST /auth/start after 60s window → 202 again."""
    fake = FakeLoginService()
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    app = _build_app(fake, rate_limiter=rl)

    _now = [0.0]
    original_acquire = rl.acquire

    def _controlled_acquire(key: str, *, now: float) -> bool:
        return original_acquire(key, now=_now[0])

    rl.acquire = _controlled_acquire  # type: ignore[method-assign]

    with TestClient(app) as client:
        resp1 = client.post("/auth/start")
        assert resp1.status_code == 202

        _now[0] = 61.0
        resp2 = client.post("/auth/start")
        assert resp2.status_code == 202


# ---------------------------------------------------------------------------
# 503 Service Unavailable — executor not bound (M1 guard)
# ---------------------------------------------------------------------------


def test_auth_start_503_when_no_executor() -> None:
    """POST /auth/start returns 503 when executor is not yet bound (startup incomplete)."""
    fake = FakeLoginService(no_executor=True)
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/start")
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


def test_auth_refresh_202() -> None:
    """POST /auth/refresh returns 202 with status='refreshing' on success."""
    fake = FakeLoginService()
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, refresh_rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/refresh")
    assert resp.status_code == 202
    assert resp.json()["status"] == "refreshing"
    assert fake.refresh_called is True


def test_auth_refresh_409_when_busy() -> None:
    """POST /auth/refresh returns 409 when a login/refresh job is already running."""
    fake = FakeLoginService(refresh_busy=True)
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, refresh_rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/refresh")
    assert resp.status_code == 409


def test_auth_refresh_429_rate_limit() -> None:
    """POST /auth/refresh returns 429 when rate limit is exceeded."""
    fake = FakeLoginService()
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    app = _build_app(fake, refresh_rate_limiter=rl)

    _now = [0.0]
    original_acquire = rl.acquire

    def _controlled_acquire(key: str, *, now: float) -> bool:
        return original_acquire(key, now=_now[0])

    rl.acquire = _controlled_acquire  # type: ignore[method-assign]

    with TestClient(app) as client:
        resp1 = client.post("/auth/refresh")
        assert resp1.status_code == 202

        # Still within 60s window.
        _now[0] = 30.0
        resp2 = client.post("/auth/refresh")
        assert resp2.status_code == 429


def test_auth_refresh_503_when_no_executor() -> None:
    """POST /auth/refresh returns 503 when executor is not yet bound."""
    fake = FakeLoginService(refresh_no_executor=True)
    rl = RateLimiter(max_requests=100, window_seconds=60.0)
    app = _build_app(fake, refresh_rate_limiter=rl)
    with TestClient(app) as client:
        resp = client.post("/auth/refresh")
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"]
