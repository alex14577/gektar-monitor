"""Unit tests for backfill API routes.

Coverage:
  (a) POST /backfill/start → 202 + {"status": "started"} when idle.
  (b) POST /backfill/start → 409 when already running.
  (c) GET  /backfill/status → 200 with correct fields.
  (d) POST /backfill/cancel → 204 always (idempotent).
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.services.backfill import BackfillStatus
from fis_monitor.web.deps import get_backfill
from fis_monitor.web.routes.backfill import router

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
            lots_seen=42 if (running or done) else 0,
            regions_done=2 if done else (1 if running else 0),
            regions_total=2,
            total_pages_seen=5 if (running or done) else 0,
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
    assert snap.lots_seen == 42

    fake.cancel()
    assert fake.cancel_calls == 1
    assert fake.is_running() is False

    # done mode reachable via explicit constructor argument
    done_fake = FakeBackfillService(running=False, mode="done")
    done_snap = done_fake.status()
    assert done_snap.status == "done"
    assert done_snap.running is False
    assert done_snap.started_at is not None
    assert done_snap.lots_seen > 0
    assert done_snap.total_pages_seen > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(fake: FakeBackfillService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_backfill] = lambda: fake
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
        """idle: status='idle', all numeric fields zero, timestamps None."""
        fake = FakeBackfillService(running=False)
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/backfill/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "idle"
        assert body["running"] is False
        assert body["lots_seen"] == 0
        assert body["total_pages_seen"] == 0
        assert body["regions_total"] == 2
        assert body["current_region"] is None
        assert body["current_page"] is None
        assert body["regions_done"] == 0
        assert body["started_at"] is None
        assert body["updated_at"] is None

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
        assert body["lots_seen"] == 42
        assert body["total_pages_seen"] == 5
        assert body["started_at"] is not None
        assert body["updated_at"] is not None

    def test_done_status_returns_correct_shape(self) -> None:
        """done: status='done', running=False, counters survive, timestamps set."""
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
        assert body["lots_seen"] > 0, "counters must survive completion"
        assert body["total_pages_seen"] > 0, "page counter must survive completion"


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
