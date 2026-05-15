"""Unit tests for POST /cycle/run route.

Tests use TestClient + app.dependency_overrides with a FakeMonitorCycleService.
Anti-mock pattern: FakeMonitorCycleService implements request_run_now() and has
a dedicated all-methods test.

Coverage:
  (a) POST /cycle/run → 202 Accepted + body {"status": "queued"}.
  (b) Two POST /cycle/run within 10s → 429 on the second request.
  (c) svc.request_run_now() is actually called (tracked via spy flag).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.web.deps import get_monitor_cycle
from fis_monitor.web.rate_limit import RateLimiter
from fis_monitor.web.routes.cycle import router

# ---------------------------------------------------------------------------
# Fake MonitorCycleService (anti-mock)
# ---------------------------------------------------------------------------


class FakeMonitorCycleService:
    """Minimal fake that tracks request_run_now() calls.

    Implements only the subset of MonitorCycleService's public API that the
    route handler depends on.  Per the anti-mock invariant, all implemented
    methods must be exercised in test_fake_all_methods().
    """

    def __init__(self) -> None:
        self.run_now_call_count: int = 0

    def request_run_now(self) -> None:
        self.run_now_call_count += 1


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_app(
    fake_svc: FakeMonitorCycleService,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the cycle router and injected fakes."""
    import fis_monitor.web.routes.cycle as cycle_module

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_monitor_cycle] = lambda: fake_svc

    if rate_limiter is not None:
        cycle_module._cycle_rate_limiter = rate_limiter

    return app


# ---------------------------------------------------------------------------
# Anti-mock: all methods of FakeMonitorCycleService
# ---------------------------------------------------------------------------


def test_fake_monitor_cycle_service_all_methods() -> None:
    """Invoke ALL methods to detect runtime API bugs in the fake."""
    fake = FakeMonitorCycleService()
    fake.request_run_now()
    assert fake.run_now_call_count == 1

    fake.request_run_now()
    assert fake.run_now_call_count == 2


# ---------------------------------------------------------------------------
# Test (a): POST /cycle/run → 202 + body {"status": "queued"}
# ---------------------------------------------------------------------------


class TestCycleRunAccepted:
    """POST /cycle/run returns 202 with the expected JSON body."""

    def test_returns_202_with_queued_status(self) -> None:
        fake = FakeMonitorCycleService()
        # Fresh rate limiter per test to avoid cross-test contamination.
        limiter = RateLimiter(max_requests=1, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/cycle/run")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        assert resp.json() == {"status": "queued"}, f"Unexpected body: {resp.json()}"


# ---------------------------------------------------------------------------
# Test (c): request_run_now() is actually called
# ---------------------------------------------------------------------------


class TestCycleRunCallsRequestRunNow:
    """POST /cycle/run causes svc.request_run_now() to be invoked."""

    def test_request_run_now_called(self) -> None:
        fake = FakeMonitorCycleService()
        limiter = RateLimiter(max_requests=5, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            client.post("/cycle/run")

        assert fake.run_now_call_count == 1, (
            f"Expected request_run_now() called once, got {fake.run_now_call_count}"
        )

    def test_each_accepted_request_calls_run_now(self) -> None:
        """Each accepted (non-rate-limited) POST calls request_run_now() once."""
        fake = FakeMonitorCycleService()
        # Allow 3 requests so we can verify the count matches.
        limiter = RateLimiter(max_requests=3, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            for _ in range(3):
                client.post("/cycle/run")

        assert fake.run_now_call_count == 3, (
            f"Expected 3 request_run_now() calls, got {fake.run_now_call_count}"
        )


# ---------------------------------------------------------------------------
# Test (b): rate limit — second request within 10s → 429
# ---------------------------------------------------------------------------


class TestCycleRunRateLimit:
    """POST /cycle/run is rate-limited to 1 request per 10s per client IP."""

    def test_second_request_within_window_returns_429(self) -> None:
        fake = FakeMonitorCycleService()
        limiter = RateLimiter(max_requests=1, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            first = client.post("/cycle/run")
            second = client.post("/cycle/run")

        assert first.status_code == 202, f"First request expected 202, got {first.status_code}"
        assert second.status_code == 429, f"Second request expected 429, got {second.status_code}"

    def test_rate_limited_request_does_not_call_run_now(self) -> None:
        """A 429 response must not trigger request_run_now()."""
        fake = FakeMonitorCycleService()
        limiter = RateLimiter(max_requests=1, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            client.post("/cycle/run")   # accepted
            client.post("/cycle/run")   # rate-limited

        # Only the first (accepted) request should have called request_run_now().
        assert fake.run_now_call_count == 1, (
            f"Expected run_now called once (not for rate-limited req), "
            f"got {fake.run_now_call_count}"
        )


# ---------------------------------------------------------------------------
# Test: HTMX content negotiation — HTML fragment response
# ---------------------------------------------------------------------------


class TestCycleRunHtmxFragment:
    """POST /cycle/run with HX-Request header returns HTML fragment."""

    def test_htmx_request_returns_html_fragment_with_ok_result(self) -> None:
        """HTMX POST returns text/html containing success indicator."""
        fake = FakeMonitorCycleService()
        limiter = RateLimiter(max_requests=5, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/cycle/run", headers={"HX-Request": "true"})

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", ""), (
            f"Expected text/html content-type, got: {resp.headers.get('content-type')}"
        )
        assert "cycle-result--ok" in resp.text, (
            f"Expected ok CSS class in fragment, got: {resp.text!r}"
        )

    def test_htmx_rate_limited_returns_html_fragment_with_error(self) -> None:
        """HTMX POST returns text/html with error indicator when rate-limited."""
        fake = FakeMonitorCycleService()
        limiter = RateLimiter(max_requests=1, window_seconds=10.0)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            client.post("/cycle/run", headers={"HX-Request": "true"})   # accepted
            resp = client.post("/cycle/run", headers={"HX-Request": "true"})  # rate-limited

        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", ""), (
            f"Expected text/html content-type, got: {resp.headers.get('content-type')}"
        )
        assert "cycle-result--err" in resp.text, (
            f"Expected err CSS class in fragment, got: {resp.text!r}"
        )
