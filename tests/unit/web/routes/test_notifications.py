"""Unit tests for GET /notifications (HTML) and GET /notifications.json (JSON API).

Layer 4 (Web) integration tests per docs/architecture/09-test-strategy.md:
TestClient + fake-infra, verifying Content-Type, template rendering, PII
isolation, and empty-state.

Coverage matrix:
  1. GET /notifications      200 + text/html.
  2. HTML page renders table rows from FakeNotificationsRepository fixtures.
  3. Empty repo → template contains 'Уведомлений пока нет'.
  4. PII: raw recipient string does NOT appear in HTML response.
  5. GET /notifications.json 200 + application/json + correct shape.
  6. /notifications.json honours ?limit= param (passes limit to repo).
  7. /notifications.json rejects limit > 500 (422).
  8. Anti-mock: all FakeNotificationsRepository methods are callable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import NotificationRecord, Settings
from fis_monitor.web.deps import get_config_source, get_notifications_repo, get_templates
from fis_monitor.web.routes.notifications import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Fake repository (ALL Protocol methods — anti-mock §6)
# ---------------------------------------------------------------------------

_DEFAULT_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)

_PLAIN_RECIPIENT = "user@example.com"


def _make_record(
    lot_id: int = 1,
    *,
    status: Literal["pending", "sent", "permanent_fail"] = "pending",
    channel: Literal["email", "browser", "heartbeat"] = "email",
    recipient: str = _PLAIN_RECIPIENT,
) -> NotificationRecord:
    sent_at = _DEFAULT_NOW if status == "sent" else None
    return NotificationRecord(
        lot_id=lot_id,
        channel=channel,
        recipient=recipient,
        status=status,
        attempt_no=1,
        last_attempt_at=_DEFAULT_NOW,
        sent_at=sent_at,
    )


class FakeNotificationsRepository:
    """Fake implementing ALL NotificationsRepository Protocol methods."""

    def __init__(self, records: list[NotificationRecord] | None = None) -> None:
        self._records = records if records is not None else [_make_record()]
        self.list_recent_calls: list[int] = []
        self.reserve_calls: list[tuple[int, str, str]] = []
        self.status_of_calls: list[tuple[int, str, str]] = []
        self.mark_attempt_calls: list[tuple[int, str, str, datetime]] = []
        self.mark_sent_calls: list[tuple[int, str, str, datetime]] = []
        self.mark_permanent_fail_calls: list[tuple[int, str, str]] = []
        self.list_pending_older_than_calls: list[timedelta] = []

    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
        self.reserve_calls.append((lot_id, channel, recipient))
        return True

    def status_of(
        self, lot_id: int, channel: str, recipient: str
    ) -> Literal["pending", "sent", "permanent_fail"] | None:
        self.status_of_calls.append((lot_id, channel, recipient))
        return "pending"

    def mark_attempt(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> int | None:
        self.mark_attempt_calls.append((lot_id, channel, recipient, at))
        return 1

    def mark_sent(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> None:
        self.mark_sent_calls.append((lot_id, channel, recipient, at))

    def mark_permanent_fail(
        self, lot_id: int, channel: str, recipient: str
    ) -> None:
        self.mark_permanent_fail_calls.append((lot_id, channel, recipient))

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
        self.list_pending_older_than_calls.append(age)
        return []

    def list_recent(self, limit: int) -> list[NotificationRecord]:
        self.list_recent_calls.append(limit)
        return self._records[:limit]


# ---------------------------------------------------------------------------
# Minimal ConfigSource stub for base.html.jinja context
# ---------------------------------------------------------------------------


class _FakeConfigSource:
    def __init__(self) -> None:
        self._settings = Settings()

    def current(self) -> Settings:
        return self._settings


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _make_html_app(
    fake: FakeNotificationsRepository | None = None,
) -> tuple[FastAPI, FakeNotificationsRepository]:
    """App with real templates mounted (needed for base.html.jinja url_for calls)."""
    if fake is None:
        fake = FakeNotificationsRepository()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_notifications_repo] = lambda: fake
    app.dependency_overrides[get_config_source] = lambda: _FakeConfigSource()
    app.dependency_overrides[get_templates] = lambda: templates
    # bd 47uh
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
    return app, fake


def _make_json_app(
    fake: FakeNotificationsRepository | None = None,
) -> tuple[FastAPI, FakeNotificationsRepository]:
    """Minimal app for JSON endpoint tests (no templates needed)."""
    if fake is None:
        fake = FakeNotificationsRepository()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_notifications_repo] = lambda: fake
    return app, fake


# ---------------------------------------------------------------------------
# HTML endpoint tests
# ---------------------------------------------------------------------------


def test_notifications_html_200_content_type() -> None:
    """GET /notifications returns 200 with text/html."""
    app, _ = _make_html_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_notifications_html_renders_table_rows() -> None:
    """Template contains table rows for records returned by fake repo."""
    records = [
        _make_record(1, status="sent"),
        _make_record(2, status="permanent_fail"),
        _make_record(3, status="pending"),
    ]
    app, _ = _make_html_app(FakeNotificationsRepository(records=records))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    body = resp.text
    assert "<table" in body
    # All three lot ids must appear
    for lot_id in (1, 2, 3):
        assert str(lot_id) in body


def test_notifications_html_empty_state() -> None:
    """Empty repo → template shows 'Уведомлений пока нет'."""
    app, _ = _make_html_app(FakeNotificationsRepository(records=[]))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    assert resp.status_code == 200
    assert "Уведомлений пока нет" in resp.text


def test_notifications_html_pii_not_exposed() -> None:
    """Raw recipient string must NOT appear anywhere in the HTML response."""
    app, _ = _make_html_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    assert _PLAIN_RECIPIENT not in resp.text


# ---------------------------------------------------------------------------
# JSON endpoint tests
# ---------------------------------------------------------------------------


def test_notifications_json_200_shape() -> None:
    """GET /notifications.json returns 200 application/json with expected fields."""
    app, fake = _make_json_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications.json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert "lot_id" in item
    assert "channel" in item
    assert "status" in item
    assert fake.list_recent_calls == [100]  # default limit


def test_notifications_json_custom_limit() -> None:
    """?limit=N is forwarded to repo.list_recent(N)."""
    records = [_make_record(i) for i in range(1, 6)]
    app, fake = _make_json_app(FakeNotificationsRepository(records=records))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications.json?limit=3")
    assert resp.status_code == 200
    assert fake.list_recent_calls == [3]


def test_notifications_json_limit_above_max_rejected() -> None:
    """limit > 500 must be rejected with 422 (FastAPI Query validation)."""
    app, _ = _make_json_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/notifications.json?limit=501")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Verify every method of FakeNotificationsRepository is reachable (§6)."""
    fake = FakeNotificationsRepository()
    now = _DEFAULT_NOW

    assert fake.reserve(1, "email", "u@x.com") is True
    assert fake.status_of(1, "email", "u@x.com") == "pending"
    assert fake.mark_attempt(1, "email", "u@x.com", now) == 1
    fake.mark_sent(1, "email", "u@x.com", now)
    fake.mark_permanent_fail(1, "email", "u@x.com")
    pending = fake.list_pending_older_than(timedelta(hours=1))
    assert pending == []
    recent = fake.list_recent(10)
    assert len(recent) == 1

    assert len(fake.reserve_calls) == 1
    assert len(fake.status_of_calls) == 1
    assert len(fake.mark_attempt_calls) == 1
    assert len(fake.mark_sent_calls) == 1
    assert len(fake.mark_permanent_fail_calls) == 1
    assert len(fake.list_pending_older_than_calls) == 1
    assert fake.list_recent_calls == [10]
