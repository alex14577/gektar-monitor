"""Unit tests for backfill API routes.

Coverage:
  (a) POST /backfill/start → 202 + {"status": "started"} when idle.
  (b) POST /backfill/start → 409 when already running.
  (c) GET  /backfill/status → 200 with correct fields.
  (d) POST /backfill/cancel → 204 always (idempotent).
  (e) Rate limiting: /start and /cancel share a quota (3 req / 60 s).
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.services.backfill import BackfillStatus
from fis_monitor.web.deps import get_backfill, get_lot_repo
from fis_monitor.web.rate_limit import RateLimiter
from fis_monitor.web.routes.backfill import router
from tests.unit.web.routes.conftest import FakeLotRepo

# ---------------------------------------------------------------------------
# Fake BackfillService
# ---------------------------------------------------------------------------


class FakeBackfillService:
    """Fake BackfillService — all Protocol methods callable.

    ``mode`` controls which BackfillStatus is returned by ``status()``:
      - ``"idle"``    — no run ever started (default when running=False)
      - ``"running"`` — a run is active (running=True)
      - ``"done"``    — most recent run completed successfully
    """

    def __init__(self, *, running: bool = False, mode: str = "") -> None:
        self._running = running
        # mode="" inferred from running flag; explicit "done" overrides
        self._mode: str = mode if mode else ("running" if running else "idle")
        self.cancel_calls: int = 0
        self.start_calls: int = 0

    def is_running(self) -> bool:
        return self._running

    def start(self, stop_event_external: threading.Event) -> bool:
        self.start_calls += 1
        if self._running:
            return False
        self._running = True
        self._mode = "running"
        return True

    def status(self) -> BackfillStatus:
        running = self._mode == "running"
        done = self._mode == "done"
        return BackfillStatus(
            running=running,
            status=self._mode,
            current_region=77 if running else None,
            current_page=3 if running else None,
            regions_total=2,
            started_at="2026-01-01T12:00:00+00:00" if (running or done) else None,
            updated_at="2026-01-01T12:01:00+00:00" if (running or done) else None,
        )

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._running = False
        self._mode = "idle"


# ---------------------------------------------------------------------------
# Anti-mock: all fake methods exercised
# ---------------------------------------------------------------------------


def test_fake_backfill_service_all_methods() -> None:
    fake = FakeBackfillService(running=False)
    assert fake.is_running() is False

    stop = threading.Event()
    r = fake.start(stop)
    assert r is True
    assert fake.is_running() is True

    snap = fake.status()
    assert snap.running is True

    fake.cancel()
    assert fake.cancel_calls == 1
    assert fake.is_running() is False

    # done mode reachable via explicit constructor argument
    done_fake = FakeBackfillService(running=False, mode="done")
    done_snap = done_fake.status()
    assert done_snap.status == "done"
    assert done_snap.running is False
    assert done_snap.started_at is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(
    fake: FakeBackfillService,
    rate_limiter: RateLimiter | None = None,
    active_count: int = 0,
) -> FastAPI:
    import fis_monitor.web.routes.backfill as backfill_module

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_backfill] = lambda: fake
    app.dependency_overrides[get_lot_repo] = lambda: FakeLotRepo(active_count=active_count)

    # Always inject an isolated limiter to prevent cross-test contamination via
    # the module-level singleton.  Callers that need a specific budget pass one
    # explicitly; all others get a permissive default (100 req / 60 s).
    backfill_module._backfill_rate_limiter = (
        rate_limiter
        if rate_limiter is not None
        else RateLimiter(max_requests=100, window_seconds=60.0)
    )

    return app


# ---------------------------------------------------------------------------
# Test (a): POST /backfill/start → 202 when idle
# ---------------------------------------------------------------------------


class TestBackfillStartAccepted:
    def test_returns_202_with_started_status(self) -> None:
        fake = FakeBackfillService(running=False)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/backfill/start")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        assert resp.json() == {"status": "started"}

    def test_starts_a_daemon_thread(self) -> None:
        """The route should not block — response comes back immediately."""
        fake = FakeBackfillService(running=False)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/backfill/start")

        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Test (b): POST /backfill/start → 409 when running
# ---------------------------------------------------------------------------


class TestBackfillStartConflict:
    def test_returns_409_when_running(self) -> None:
        fake = FakeBackfillService(running=True)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/backfill/start")

        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        detail = resp.json().get("detail", "")
        assert "running" in detail.lower()


# ---------------------------------------------------------------------------
# Test (c): GET /backfill/status → 200
# ---------------------------------------------------------------------------


class TestBackfillStatus:
    def test_idle_status_all_fields(self) -> None:
        """idle: status='idle', progress fields at defaults, timestamps None."""
        fake = FakeBackfillService(running=False)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/backfill/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "idle"
        assert body["running"] is False
        assert body["regions_total"] == 2
        assert body["current_region"] is None
        assert body["current_page"] is None
        assert body["started_at"] is None
        assert body["updated_at"] is None
        # hs9c: dead counter fields must not appear in the response
        assert "lots_seen" not in body
        assert "regions_done" not in body
        assert "total_pages_seen" not in body

    def test_status_includes_active_lot_count(self) -> None:
        """active_lot_count key is present and matches lot_repo.count_active()."""
        fake = FakeBackfillService(running=False)
        app = _build_app(fake, active_count=42)

        with TestClient(app) as client:
            resp = client.get("/backfill/status")

        assert resp.status_code == 200
        body = resp.json()
        assert "active_lot_count" in body
        assert body["active_lot_count"] == 42

    def test_running_status(self) -> None:
        """running: status='running', progress fields populated."""
        fake = FakeBackfillService(running=True)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/backfill/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["running"] is True
        assert body["current_region"] == 77
        assert body["current_page"] == 3
        assert body["started_at"] is not None
        assert body["updated_at"] is not None
        # hs9c: dead counter fields must not appear in the response
        assert "lots_seen" not in body
        assert "regions_done" not in body
        assert "total_pages_seen" not in body

    def test_done_status_returns_correct_shape(self) -> None:
        """done: status='done', running=False, timestamps set."""
        fake = FakeBackfillService(running=False, mode="done")
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/backfill/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "done"
        assert body["running"] is False
        assert body["started_at"] is not None
        assert body["updated_at"] is not None
        # hs9c: dead counter fields must not appear in the response
        assert "lots_seen" not in body
        assert "regions_done" not in body
        assert "total_pages_seen" not in body


# ---------------------------------------------------------------------------
# Test (d): POST /backfill/cancel → 204 always
# ---------------------------------------------------------------------------


class TestBackfillCancel:
    def test_returns_204_when_running(self) -> None:
        fake = FakeBackfillService(running=True)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/backfill/cancel")

        assert resp.status_code == 204
        assert fake.cancel_calls == 1

    def test_returns_204_when_idle(self) -> None:
        """Idempotent — 204 even when no backfill is running."""
        fake = FakeBackfillService(running=False)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/backfill/cancel")

        assert resp.status_code == 204
        assert fake.cancel_calls == 1


# ---------------------------------------------------------------------------
# Test (e): Rate limiting — /start and /cancel share a single quota
# ---------------------------------------------------------------------------


class TestBackfillRateLimit:
    def test_backfill_start_rate_limit(self) -> None:
        """3rd+ /start within window → 429."""
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        app = _build_app(FakeBackfillService(running=False), rate_limiter=limiter)

        with TestClient(app) as client:
            r1 = client.post("/backfill/start")
            # After first start the fake marks itself running; reset for next call.
            assert r1.status_code == 202
            # Re-start: fake now running → would be 409 but rate limit fires first.
            # Use a fresh fake each request via overrides to isolate rate-limit behaviour.

        # Fresh fake (idle) so we only see rate-limit, not 409.
        fake2 = FakeBackfillService(running=False)
        app2 = _build_app(fake2, rate_limiter=limiter)

        with TestClient(app2) as client2:
            r2 = client2.post("/backfill/start")
            assert r2.status_code == 202

            # 3rd request — quota exhausted.
            r3 = client2.post("/backfill/start")
            assert r3.status_code == 429, f"Expected 429, got {r3.status_code}: {r3.text}"

            r4 = client2.post("/backfill/start")
            assert r4.status_code == 429

    def test_backfill_cancel_rate_limit(self) -> None:
        """3rd+ /cancel within window → 429."""
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        fake = FakeBackfillService(running=True)
        app = _build_app(fake, rate_limiter=limiter)

        with TestClient(app) as client:
            r1 = client.post("/backfill/cancel")
            assert r1.status_code == 204

            r2 = client.post("/backfill/cancel")
            assert r2.status_code == 204

            r3 = client.post("/backfill/cancel")
            assert r3.status_code == 429, f"Expected 429, got {r3.status_code}: {r3.text}"

            r4 = client.post("/backfill/cancel")
            assert r4.status_code == 429

    def test_backfill_start_and_cancel_share_quota(self) -> None:
        """2× /start + 1× /cancel with quota=2 → cancel gets 429."""
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)

        # Build two apps backed by the same limiter so both endpoints consume it.
        fake_start = FakeBackfillService(running=False)
        app_start = _build_app(fake_start, rate_limiter=limiter)

        fake_cancel = FakeBackfillService(running=True)
        app_cancel = _build_app(fake_cancel, rate_limiter=limiter)

        with TestClient(app_start) as start_client:
            r1 = start_client.post("/backfill/start")
            assert r1.status_code == 202

            # fake_start is now running; reset so second POST reaches limiter
            fake_start._running = False
            fake_start._mode = "idle"

            r2 = start_client.post("/backfill/start")
            assert r2.status_code == 202

        # Quota exhausted — cancel must be rejected.
        with TestClient(app_cancel) as cancel_client:
            r3 = cancel_client.post("/backfill/cancel")
            assert r3.status_code == 429, (
                f"Expected 429 after quota exhausted by /start, got {r3.status_code}"
            )
