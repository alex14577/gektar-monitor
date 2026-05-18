"""Unit tests for GET /settings content-negotiation (HTML vs JSON).

Coverage:
  1. GET /settings + Accept: application/json -> 200 application/json (unchanged).
  2. GET /settings + Accept: text/html       -> 200 text/html with anchor ids.
  3. GET /settings (no Accept header)         -> 200 application/json (API default).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import Settings
from fis_monitor.web.deps import (
    get_config_source,
    get_settings_service,
    get_smtp_test,
    get_templates,
)
from fis_monitor.web.routes.settings import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSource:
    """Minimal ConfigSource stub for settings page tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> object:
        return object()

    def save(self, settings: Settings) -> None:
        self._settings = settings


class FakeSettingsService:
    def set_smtp_credentials(self, creds: Any) -> None:
        pass


class FakeSmtpTestService:
    def test_send(self, lot: Any, recipient: str) -> Any:
        return object()


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(settings: Settings | None = None) -> FastAPI:
    """Build minimal FastAPI app with real templates mounted for HTML rendering."""
    fc = FakeConfigSource(settings=settings)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI()
    # Static mount is required because base.html.jinja calls url_for('static', ...).
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fc
    app.dependency_overrides[get_settings_service] = lambda: FakeSettingsService()
    app.dependency_overrides[get_smtp_test] = lambda: FakeSmtpTestService()
    app.dependency_overrides[get_templates] = lambda: templates
    # bd 47uh: header-status VM deps
    from datetime import UTC
    from datetime import datetime as _dt

    from fis_monitor.web.deps import get_clock, get_lot_repo
    from tests.fakes.lot_repository import FakeLotRepository

    class _C:
        def now(self):
            return _dt(2026, 5, 18, tzinfo=UTC)
        def monotonic(self):
            return 0.0

    app.dependency_overrides[get_lot_repo] = lambda: FakeLotRepository()
    app.dependency_overrides[get_clock] = lambda: _C()
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_settings_json_explicit_accept() -> None:
    """Accept: application/json -> 200 with JSON body (original behaviour)."""
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert "regions" in body
    assert "interval_minutes" in body


def test_get_settings_html_accept() -> None:
    """Accept: text/html -> 200 with HTML containing expected section anchors."""
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="scope"' in body
    assert 'id="smtp"' in body
    assert 'id="notifications"' in body
    # Monitoring section was renamed to schedule-section per ADR-033.
    assert 'id="schedule-section"' in body


def test_get_settings_no_accept_defaults_to_json() -> None:
    """No Accept header -> JSON (safe default for API clients / curl)."""
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        # TestClient sends no Accept header by default when headers dict is omitted.
        resp = client.get("/settings", headers={"Accept": ""})
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    resp.json()  # must be valid JSON
