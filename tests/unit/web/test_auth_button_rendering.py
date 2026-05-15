"""Tests for auth button rendering (bd gektar_monitor-oem).

Variant B: login buttons are <button data-action="login-start">, not <a href="/auth/login">.
auth.js is loaded via <script defer src=".../auth.js">.

Coverage:
  1. base.html.jinja: no href="/auth/login", data-action="login-start" present.
  2. feed.html.jinja: no href="/auth/login", data-action="login-start" present.
  3. base.html.jinja: <script defer src=".../auth.js"> present.
  4. Static file auth.js exists on disk.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import SessionStatus, Settings
from fis_monitor.services.login import LoginStatus
from fis_monitor.web.deps import (
    get_catchup_dismiss,
    get_clock,
    get_config_source,
    get_dnd_service,
    get_login,
    get_lot_repo,
    get_session_probe,
    get_templates,
    get_user_state_repo,
)
from fis_monitor.web.routes.main import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR
from tests.factories import make_settings

# ---------------------------------------------------------------------------
# Fakes (minimal — mirrors test_main.py pattern)
# ---------------------------------------------------------------------------


class _FakeSubscription:
    def unsubscribe(self) -> None:
        pass


class _FakeConfigSource:
    def __init__(self) -> None:
        self._settings = make_settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: object) -> _FakeSubscription:
        return _FakeSubscription()

    def save(self, settings: Settings) -> None:
        self._settings = settings


class _FakeSessionProbe:
    def __init__(self, status: SessionStatus = SessionStatus.EXPIRED) -> None:
        self._status = status

    def check(self) -> SessionStatus:
        return self._status


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    session_status: SessionStatus = SessionStatus.EXPIRED,
) -> FastAPI:
    """Minimal FastAPI app with main router + injected fakes."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = _FakeConfigSource
    app.dependency_overrides[get_session_probe] = lambda: _FakeSessionProbe(session_status)

    class _StubLogin:
        def status(self) -> LoginStatus:
            return LoginStatus(running=False, last_outcome=None)

    app.dependency_overrides[get_login] = lambda: _StubLogin()
    app.dependency_overrides[get_templates] = lambda: templates

    # Feed integration deps (bd jh2) — auth-button tests only care about the
    # session/login HTML, so we provide do-nothing stubs for the rest.
    class _StubDnd:
        def is_active(self, now: datetime) -> bool:
            return False

        def until(self, now: datetime) -> datetime | None:
            return None

    class _StubCatchup:
        def is_dismissed(self, now: datetime) -> bool:
            return True  # suppress banner — irrelevant to auth-button tests

    class _StubUserStateRepo:
        def last_visit(self) -> datetime | None:
            return None

    class _StubLotRepo:
        def count_active(self) -> int:
            return 0

    class _StubClock:
        def now(self) -> datetime:
            return datetime(2026, 5, 15, 12, 0, tzinfo=UTC)

        def monotonic(self) -> float:
            return 0.0

    app.dependency_overrides[get_dnd_service] = lambda: _StubDnd()
    app.dependency_overrides[get_catchup_dismiss] = lambda: _StubCatchup()
    app.dependency_overrides[get_user_state_repo] = lambda: _StubUserStateRepo()
    app.dependency_overrides[get_lot_repo] = lambda: _StubLotRepo()
    app.dependency_overrides[get_clock] = lambda: _StubClock()
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_base_template_no_href_auth_login() -> None:
    """AC#1a: base.html.jinja must not contain href="/auth/login"."""
    app = _make_app(session_status=SessionStatus.EXPIRED)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/auth/login"' not in resp.text, (
        "base.html.jinja still contains broken href='/auth/login'"
    )


def test_base_template_has_login_start_button() -> None:
    """AC#1b: base.html.jinja must contain data-action="login-start" button."""
    app = _make_app(session_status=SessionStatus.EXPIRED)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-action="login-start"' in resp.text, (
        "base.html.jinja missing data-action='login-start' button"
    )


def test_base_template_auth_js_script_tag() -> None:
    """AC#3: base.html.jinja must include <script defer src=".../auth.js">."""
    app = _make_app(session_status=SessionStatus.ACTIVE)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "auth.js" in resp.text, (
        "base.html.jinja missing script tag for auth.js"
    )


def test_feed_template_no_href_auth_login() -> None:
    """AC#2a: feed.html.jinja must not contain href="/auth/login"."""
    app = _make_app(session_status=SessionStatus.EXPIRING)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/auth/login"' not in resp.text, (
        "feed.html.jinja still contains broken href='/auth/login'"
    )


def test_feed_template_has_login_start_button() -> None:
    """AC#2b: feed.html.jinja must contain data-action="login-start" button.

    The "Войти заново" button is rendered in the session-expiry warning banner
    which is only visible when session is EXPIRING.
    """
    app = _make_app(session_status=SessionStatus.EXPIRING)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-action="login-start"' in resp.text, (
        "feed.html.jinja missing data-action='login-start' button in expiry banner"
    )


def test_auth_js_file_exists_on_disk() -> None:
    """AC#4: auth.js must be present in the static directory."""
    auth_js = STATIC_DIR / "auth.js"
    assert auth_js.is_file(), f"Missing static file: {auth_js}"
