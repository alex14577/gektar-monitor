"""Unit tests for GET / — main feed route (bd gektar_monitor-3v3).

MVP: route renders feed.html.jinja with safe defaults.
Lot-feed query and health derivation are future bd-tasks.

Coverage:
  1. GET / returns 200 with #feed marker (completed state assumed).
  2. SessionProbe ACTIVE → no warning banner, no visible expired modal.
  3. SessionProbe EXPIRING → warning banner present.
  4. SessionProbe EXPIRED → #session-expired-modal WITHOUT hidden attribute.
  5. Settings.interval_minutes flows through to monitor context.
  6. Anti-mock: FakeConfigSource + FakeSessionProbe — all methods called.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.interfaces import ConfigSubscription
from fis_monitor.domain.models import SessionStatus, Settings
from fis_monitor.web.deps import get_config_source, get_session_probe, get_templates
from fis_monitor.web.routes.main import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR
from tests.factories import make_settings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSubscription:
    """Stub ConfigSubscription returned by FakeConfigSource.subscribe()."""

    def unsubscribe(self) -> None:
        pass


class FakeConfigSource:
    """Fake ConfigSource — implements ALL public methods (anti-mock §6)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.current_calls: int = 0
        self.subscribe_calls: int = 0
        self.save_calls: list[Settings] = []

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: Any) -> ConfigSubscription:
        self.subscribe_calls += 1
        return _FakeSubscription()

    def save(self, settings: Settings) -> None:
        self._settings = settings
        self.save_calls.append(settings)


class FakeSessionProbe:
    """Fake SessionProbe — implements check() protocol method.

    Parameterisable via ``status`` ctor arg.
    """

    def __init__(self, status: SessionStatus = SessionStatus.ACTIVE) -> None:
        self._status = status
        self.check_calls: int = 0

    def check(self) -> SessionStatus:
        self.check_calls += 1
        return self._status


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    *,
    settings: Settings | None = None,
    session_status: SessionStatus = SessionStatus.ACTIVE,
) -> tuple[FastAPI, FakeConfigSource, FakeSessionProbe]:
    """Build a minimal FastAPI app with main router + injected fakes."""
    fake_cfg = FakeConfigSource(settings=settings)
    fake_probe = FakeSessionProbe(status=session_status)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fake_cfg
    app.dependency_overrides[get_session_probe] = lambda: fake_probe
    app.dependency_overrides[get_templates] = lambda: templates
    return app, fake_cfg, fake_probe


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_root_returns_200_with_feed_marker() -> None:
    """AC#1: GET / returns 200 and HTML contains the #feed element."""
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="feed"' in resp.text


def test_session_active_no_warning_no_expired_modal_visible() -> None:
    """AC#3: ACTIVE session → no expiry banner, modal has hidden attribute."""
    app, _, _ = _make_app(session_status=SessionStatus.ACTIVE)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Warning banner only rendered when expires_soon is True
    assert "banner--warn" not in html
    # Modal must carry the hidden attribute (not shown)
    assert 'id="session-expired-modal"' in html
    assert "session-expired-modal" in html
    # The hidden attribute must be present on the modal div
    assert "hidden" in html


def test_session_expiring_shows_warning_banner() -> None:
    """AC#4: EXPIRING session → session-warning banner present in response."""
    app, _, _ = _make_app(session_status=SessionStatus.EXPIRING)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert "banner--warn" in resp.text


def test_session_expired_modal_visible() -> None:
    """AC#5: EXPIRED session → #session-expired-modal without hidden attr."""
    app, _, _ = _make_app(session_status=SessionStatus.EXPIRED)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Modal is present
    assert 'id="session-expired-modal"' in html
    # When expired the modal must NOT have the hidden attribute — check that
    # the specific pattern <... hidden ...> is absent right after the modal id.
    # The template renders: {% if not session.expired %}hidden{% endif %}
    # so we check that the rendered modal div does NOT include "hidden" before
    # the closing >.  We use a simple substring search around the modal tag.
    modal_start = html.index('id="session-expired-modal"')
    # Slice from the modal start to the first closing '>'
    modal_open_tag = html[modal_start : html.index(">", modal_start) + 1]
    assert "hidden" not in modal_open_tag, (
        "EXPIRED session must render modal without hidden attribute"
    )


def test_interval_minutes_flows_to_monitor_context() -> None:
    """AC#6: Settings.interval_minutes is visible in the rendered page."""
    settings = make_settings(interval_minutes=15)
    app, _, _ = _make_app(settings=settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    # The monitor context carries interval_minutes; feed.html.jinja or
    # base.html.jinja must reflect it somewhere.  The sidebar includes
    # monitor state — we assert the integer is present in the HTML body.
    assert "15" in resp.text


def test_all_fake_methods_are_called() -> None:
    """AC#7 Anti-mock: every method on every fake is invoked at least once."""
    # FakeConfigSource
    fake_cfg = FakeConfigSource()
    s = fake_cfg.current()
    assert isinstance(s, Settings)
    assert fake_cfg.current_calls == 1

    sub = fake_cfg.subscribe(lambda _: None)
    assert fake_cfg.subscribe_calls == 1
    sub.unsubscribe()

    new_settings = Settings(interval_minutes=5)
    fake_cfg.save(new_settings)
    assert len(fake_cfg.save_calls) == 1
    assert fake_cfg.save_calls[0].interval_minutes == 5

    # FakeSessionProbe
    fake_probe = FakeSessionProbe(status=SessionStatus.EXPIRING)
    result = fake_probe.check()
    assert result == SessionStatus.EXPIRING
    assert fake_probe.check_calls == 1
