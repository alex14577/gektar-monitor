"""Unit tests for OnboardingGateMiddleware (ADR-018, bd task oxy.2).

Anti-mock pattern: FakeOnboardingService implements all methods used by the
middleware's OnboardingQuery protocol (current + url_for_current_step), plus a
dedicated all-methods invocation test.

TestClient is always constructed with follow_redirects=False so redirects are
captured rather than auto-followed.

Coverage (12 required by acceptance):
 1.  not_started + GET /          → 302 to /onboarding/regions
 2.  regions_set + GET /          → 302 to /onboarding/smtp
 3.  completed   + GET /          → 200 (passes through)
 4.  not_started + GET /?step=4   → 302 to /onboarding/regions (no bypass)
 5.  not_started + GET /static/…  → 200 (whitelist)
 6.  not_started + GET /onboarding/regions → 200 (whitelist)
 7.  not_started + GET /sse/events → 200 (whitelist)
 8.  not_started + GET /api/health → 200 (whitelist)
 9.  not_started + GET /auth/start → 200 (whitelist)
 10. OPTIONS /   in not_started    → passes through (CORS preflight)
 11. Redirect response has Cache-Control: no-store
 12. State queried once per non-whitelisted request; zero times for whitelisted
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from fis_monitor.domain.models import OnboardingState
from fis_monitor.web.onboarding_gate import OnboardingGateMiddleware

# ---------------------------------------------------------------------------
# Fake OnboardingService (anti-mock — all protocol methods exercised)
# ---------------------------------------------------------------------------

# Maps OnboardingState → redirect URL (mirrors _STATE_URL in services/onboarding.py)
_STATE_URL: dict[OnboardingState, str] = {
    OnboardingState.NOT_STARTED: "/onboarding/regions",
    OnboardingState.REGIONS_SET: "/onboarding/smtp",
    OnboardingState.SMTP_CONFIGURED: "/onboarding/recipients",
    OnboardingState.RECIPIENTS_SET: "/onboarding/test-email",
    OnboardingState.COMPLETED: "/",
}


class FakeOnboardingService:
    """Fake implementing the OnboardingQuery protocol.

    Tracks how many times ``current`` and ``url_for_current_step`` are called
    so tests can assert query count.
    """

    def __init__(self, state: OnboardingState) -> None:
        self._state = state
        self.current_call_count: int = 0
        self.url_call_count: int = 0

    def current(self) -> OnboardingState:
        self.current_call_count += 1
        return self._state

    def url_for_current_step(self) -> str:
        self.url_call_count += 1
        return _STATE_URL[self._state]


# ---------------------------------------------------------------------------
# Anti-mock: all-methods test
# ---------------------------------------------------------------------------


def test_fake_onboarding_service_all_methods() -> None:
    """Invoke ALL protocol methods to catch runtime API bugs in the fake."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)

    state = fake.current()
    assert state is OnboardingState.NOT_STARTED
    assert fake.current_call_count == 1

    url = fake.url_for_current_step()
    assert url == "/onboarding/regions"
    assert fake.url_call_count == 1


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_app(fake: FakeOnboardingService) -> FastAPI:
    """Build a minimal FastAPI app with the gate middleware and a catch-all route."""
    app = FastAPI()

    @app.get("/{path:path}")
    async def _catch_all(path: str = "") -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.options("/{path:path}")
    async def _catch_options(path: str = "") -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_middleware(OnboardingGateMiddleware, svc=fake)
    return app


# ---------------------------------------------------------------------------
# Tests 1-3: redirect vs pass-through for different states
# ---------------------------------------------------------------------------


def test_not_started_redirects_to_regions() -> None:
    """Test 1: not_started + GET / → 302 to /onboarding/regions."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/regions"


def test_regions_set_redirects_to_smtp() -> None:
    """Test 2: regions_set + GET / → 302 to /onboarding/smtp."""
    fake = FakeOnboardingService(OnboardingState.REGIONS_SET)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/smtp"


def test_completed_passes_through() -> None:
    """Test 3: completed + GET / → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.COMPLETED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 4: query-param bypass cannot work
# ---------------------------------------------------------------------------


def test_query_param_bypass_not_possible() -> None:
    """Test 4: query-param value MUST NOT influence redirect target.

    Use a state (REGIONS_SET → server-decided /onboarding/smtp) different from
    the URL the query-param "would" point at (?step=1 → /onboarding/regions).
    A vacuous test that only checks "302 happened" would pass even if the
    middleware was secretly reading ?step. Asserting the SERVER-decided URL
    wins proves the middleware never reads query-params (ADR-018 #19).
    """
    fake = FakeOnboardingService(OnboardingState.REGIONS_SET)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/?step=1")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/smtp"


def test_onboarding_lookalike_not_whitelisted() -> None:
    """Boundary: /onboarding-evil/* must NOT be whitelisted (prefix has trailing /)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/onboarding-evil/wipe")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/regions"


# ---------------------------------------------------------------------------
# Tests 5-9: whitelist prefixes
# ---------------------------------------------------------------------------


def test_static_whitelisted() -> None:
    """Test 5: not_started + GET /static/app.css → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/static/app.css")
    assert resp.status_code == 200


def test_onboarding_prefix_whitelisted() -> None:
    """Test 6: not_started + GET /onboarding/regions → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/onboarding/regions")
    assert resp.status_code == 200


def test_sse_whitelisted() -> None:
    """Test 7: not_started + GET /sse/events → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/sse/events")
    assert resp.status_code == 200


def test_api_health_whitelisted() -> None:
    """Test 8: not_started + GET /api/health → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_auth_whitelisted() -> None:
    """Test 9: not_started + GET /auth/start → 200 (no redirect)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/auth/start")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 10: OPTIONS passes through (CORS preflight)
# ---------------------------------------------------------------------------


def test_options_passes_through() -> None:
    """Test 10: OPTIONS / in not_started → passes through (not gated)."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.options("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 11: Cache-Control: no-store on redirect
# ---------------------------------------------------------------------------


def test_redirect_has_cache_no_store() -> None:
    """Test 11: redirect response has Cache-Control: no-store header."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "no-store" in resp.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# Test 12: state queried exactly once (non-whitelisted) / zero (whitelisted)
# ---------------------------------------------------------------------------


def test_state_queried_once_for_non_whitelisted() -> None:
    """Test 12a: current() called exactly once for a non-whitelisted path."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    client.get("/dashboard")
    assert fake.current_call_count == 1


def test_state_not_queried_for_whitelisted() -> None:
    """Test 12b: current() NOT called for whitelisted path."""
    fake = FakeOnboardingService(OnboardingState.NOT_STARTED)
    client = TestClient(_build_app(fake), follow_redirects=False)
    client.get("/static/style.css")
    assert fake.current_call_count == 0
