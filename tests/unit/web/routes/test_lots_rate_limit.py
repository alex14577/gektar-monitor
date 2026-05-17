"""Unit tests for rate limiting on POST /lots/{lot_id}/note.

Pattern mirrors test_cycle.py: reassign module-level ``_note_rate_limiter``
on the lots_module to inject a tight limiter, then drive requests through
TestClient.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import fis_monitor.web.routes.lots as lots_module
from fis_monitor.web.deps import get_lot_user_state_service
from fis_monitor.web.rate_limit import RateLimiter
from fis_monitor.web.routes.lots import router


# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------


class FakeLotUserStateService:
    """Minimal fake: set_note always succeeds."""

    def set_note(self, lot_id: int, note: str | None) -> None:  # noqa: D401
        pass


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(rate_limiter: RateLimiter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_lot_user_state_service] = lambda: FakeLotUserStateService()
    lots_module._note_rate_limiter = rate_limiter
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoteRateLimitEnforced:
    """3rd POST within window → 429."""

    def test_third_request_returns_429(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        app = _build_app(limiter)

        with TestClient(app) as client:
            r1 = client.post("/lots/1/note", json={"note": "a"})
            r2 = client.post("/lots/1/note", json={"note": "b"})
            r3 = client.post("/lots/1/note", json={"note": "c"})

        assert r1.status_code == 204
        assert r2.status_code == 204
        assert r3.status_code == 429
        assert r3.json()["detail"] == "rate limit exceeded"


class TestNoteWithinLimit:
    """2 POSTs within a 2-request window → both succeed."""

    def test_two_requests_both_succeed(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        app = _build_app(limiter)

        with TestClient(app) as client:
            r1 = client.post("/lots/1/note", json={"note": "first"})
            r2 = client.post("/lots/1/note", json={"note": "second"})

        assert r1.status_code == 204
        assert r2.status_code == 204
