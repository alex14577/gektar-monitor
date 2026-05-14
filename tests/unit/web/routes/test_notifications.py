"""Unit tests for GET /notifications route.

Uses TestClient + app.dependency_overrides with a FakeNotificationsRepository
that implements ALL methods of the Protocol (anti-mock pattern, §6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import NotificationRecord
from fis_monitor.web.deps import get_notifications_repo
from fis_monitor.web.routes.notifications import router

# ---------------------------------------------------------------------------
# Fake repository
# ---------------------------------------------------------------------------

_DEFAULT_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_record(lot_id: int = 1) -> NotificationRecord:
    return NotificationRecord(
        lot_id=lot_id,
        channel="email",
        recipient="user@example.com",
        status="pending",
        attempt_no=0,
        last_attempt_at=None,
        sent_at=None,
    )


class FakeNotificationsRepository:
    """Fake implementing ALL NotificationsRepository Protocol methods."""

    def __init__(self, records: list[NotificationRecord] | None = None) -> None:
        self._records = records or [_make_record()]
        # Call tracking
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
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    fake: FakeNotificationsRepository | None = None,
) -> tuple[FastAPI, FakeNotificationsRepository]:
    if fake is None:
        fake = FakeNotificationsRepository()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_notifications_repo] = lambda: fake
    return app, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_notifications_returns_200() -> None:
    app, fake = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert fake.list_recent_calls == [100]  # default limit


def test_list_notifications_custom_limit() -> None:
    records = [_make_record(i) for i in range(1, 6)]
    app, fake = _make_app(FakeNotificationsRepository(records=records))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications?limit=3")
    assert resp.status_code == 200
    assert fake.list_recent_calls == [3]


def test_list_notifications_limit_capped_at_500() -> None:
    """limit > 500 must be rejected (FastAPI Query validation)."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/notifications?limit=501")
    assert resp.status_code == 422


def test_list_notifications_limit_min_1() -> None:
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/notifications?limit=0")
    assert resp.status_code == 422


def test_list_notifications_record_shape() -> None:
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/notifications")
    item = resp.json()[0]
    assert "lot_id" in item
    assert "channel" in item
    assert "status" in item


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods in one test
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Verify every method of FakeNotificationsRepository is callable (§6)."""
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
