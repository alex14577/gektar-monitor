"""Unit tests for DnD routes.

Endpoints under test:
  POST /dnd         — Activate DnD for N minutes. Returns 204.
  GET  /dnd/custom  — HTMX partial with custom-duration form. Returns 200 HTML.

Coverage:
  (a) POST /dnd valid body → 204, DndService.set_dnd_until() called.
  (b) POST /dnd minutes=0  → 422 Unprocessable Entity (Pydantic validation).
  (c) GET  /dnd/custom     → 200 HTML with the form partial.
  (d) Anti-mock: FakeDndService — all methods exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.web.deps import get_clock, get_dnd_service, get_templates
from fis_monitor.web.routes.dnd import router
from fis_monitor.web.templates import TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDndService:
    """Minimal fake DndService tracking set_dnd_until() calls."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[datetime, int]] = []

    def set_dnd_until(self, now: datetime, minutes: int) -> None:
        self.set_calls.append((now, minutes))

    def is_active(self, now: datetime) -> bool:
        return False

    def until(self, now: datetime) -> datetime | None:
        return None


class FakeClock:
    """Deterministic clock returning a fixed UTC datetime."""

    _FIXED = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._FIXED

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Anti-mock: all FakeDndService methods
# ---------------------------------------------------------------------------


def test_fake_dnd_service_all_methods() -> None:
    """Invoke ALL FakeDndService methods to catch runtime API bugs."""
    svc = FakeDndService()
    now = FakeClock()._FIXED

    svc.set_dnd_until(now, 60)
    assert len(svc.set_calls) == 1
    assert svc.set_calls[0] == (now, 60)

    result_active = svc.is_active(now)
    assert isinstance(result_active, bool)

    result_until = svc.until(now)
    assert result_until is None


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(fake_svc: FakeDndService) -> FastAPI:
    """Build a minimal FastAPI app with the DnD router and injected fakes."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    fake_clock = FakeClock()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dnd_service] = lambda: fake_svc
    app.dependency_overrides[get_clock] = lambda: fake_clock
    app.dependency_overrides[get_templates] = lambda: templates
    return app


# ---------------------------------------------------------------------------
# (a) POST /dnd valid → 204, set_dnd_until() called
# ---------------------------------------------------------------------------


class TestPostDndValid:
    def test_returns_204_on_valid_minutes(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": 60})

        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

    def test_set_dnd_until_called_with_correct_minutes(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            client.post("/dnd", json={"minutes": 90})

        assert len(fake.set_calls) == 1, f"Expected 1 call, got {len(fake.set_calls)}"
        _now, minutes = fake.set_calls[0]
        assert minutes == 90, f"Expected minutes=90, got {minutes}"

    def test_minimum_valid_minutes(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": 1})

        assert resp.status_code == 204

    def test_maximum_valid_minutes(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": 10080})

        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# (b) POST /dnd minutes=0 → 422
# ---------------------------------------------------------------------------


class TestPostDndInvalid:
    def test_zero_minutes_returns_422(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": 0})

        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"

    def test_negative_minutes_returns_422(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": -5})

        assert resp.status_code == 422

    def test_exceeds_max_minutes_returns_422(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.post("/dnd", json={"minutes": 10081})

        assert resp.status_code == 422

    def test_set_dnd_until_not_called_on_422(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            client.post("/dnd", json={"minutes": 0})

        assert len(fake.set_calls) == 0, "set_dnd_until must not be called for invalid input"


# ---------------------------------------------------------------------------
# (c) GET /dnd/custom → 200 HTML
# ---------------------------------------------------------------------------


class TestGetDndCustom:
    def test_returns_200(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/dnd/custom")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    def test_returns_html_content_type(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/dnd/custom")

        assert "text/html" in resp.headers.get("content-type", "")

    def test_html_contains_form_with_minutes_input(self) -> None:
        fake = FakeDndService()
        app = _build_app(fake)

        with TestClient(app) as client:
            resp = client.get("/dnd/custom")

        assert 'name="minutes"' in resp.text, (
            "Expected a minutes input in the partial HTML"
        )
