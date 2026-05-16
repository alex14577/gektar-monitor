"""Logging tests for SessionExpiredEmailService DEBUG events (gektar_monitor-b9wq).

Covers:
- session_expired.detected (INFO — always emitted on handle)
- session_expired.idempotency_skip (DEBUG — when guard already set)
- session_expired.notification.queued (DEBUG — after send attempt)
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.models import (
    NotifyResult,
    Settings,
    SseSessionExpired,
)
from fis_monitor.services.dnd import DndService
from fis_monitor.services.session_expired_email import (
    SESSION_EXPIRED_EMAIL_SENT_KEY,
    SessionExpiredEmailService,
)

_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
_LOGGER = "fis_monitor.services.session_expired_email"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStateRepo:
    def __init__(self, guard_value: str | None = None) -> None:
        self._store: dict[str, str] = {}
        if guard_value is not None:
            self._store[SESSION_EXPIRED_EMAIL_SENT_KEY] = guard_value

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class _FakeSettingsRepo:
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


class _FakeConfigSource:
    def __init__(self, *, email_enabled: bool = True, recipients: list[str] | None = None) -> None:
        from fis_monitor.domain.models import EmailConfig, NotificationsConfig
        email_cfg = EmailConfig(enabled=email_enabled, recipients=list(recipients or []))
        notif_cfg = NotificationsConfig(email=email_cfg)
        self._settings = Settings(notifications=notif_cfg)

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class _FakeEmailNotifier:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    def send_session_expired(self, recipient: str) -> NotifyResult:
        return NotifyResult(ok=self._ok, detail="ok" if self._ok else "fail", retryable=False)


class _FakeEventSubscription:
    alive: bool = True
    _events: list[Any]

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def wait_one(self, timeout: float) -> Any:
        return self._events.pop(0) if self._events else None

    def unsubscribe(self) -> None:
        pass

    def __enter__(self) -> _FakeEventSubscription:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeEventBus:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def subscribe(self) -> _FakeEventSubscription:
        return _FakeEventSubscription(self._events)

    def publish(self, event: Any) -> None:
        pass


def _make_svc(
    *,
    guard_already_set: bool = False,
    email_enabled: bool = True,
    recipients: list[str] | None = None,
    notifier_ok: bool = True,
) -> SessionExpiredEmailService:
    state_repo = _FakeStateRepo(guard_value="1" if guard_already_set else None)
    config_source = _FakeConfigSource(
        email_enabled=email_enabled,
        recipients=recipients or ["user@example.com"],
    )
    dnd = DndService(settings_repo=_FakeSettingsRepo())
    notifier = _FakeEmailNotifier(ok=notifier_ok)
    return SessionExpiredEmailService(
        email_notifier=notifier,  # type: ignore[arg-type]
        state_repo=state_repo,
        config_source=config_source,
        event_bus=_FakeEventBus([]),
        clock=_FakeClock(),
        dnd_service=dnd,
        stop_event=threading.Event(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_handle_emits_session_expired_detected(caplog: pytest.LogCaptureFixture) -> None:
    """session_expired.detected emitted at INFO on every handle() call."""
    svc = _make_svc()
    event = SseSessionExpired(timestamp=_NOW)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc._handle(event)

    records = [r for r in caplog.records if r.getMessage() == "session_expired.detected"]
    assert records, "expected session_expired.detected"
    assert records[0].__dict__.get("event_type") == "SseSessionExpired"


def test_handle_emits_idempotency_skip_when_guard_set(caplog: pytest.LogCaptureFixture) -> None:
    """session_expired.idempotency_skip emitted at DEBUG when guard already set."""
    svc = _make_svc(guard_already_set=True)
    event = SseSessionExpired(timestamp=_NOW)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc._handle(event)

    records = [r for r in caplog.records if r.getMessage() == "session_expired.idempotency_skip"]
    assert records, "expected session_expired.idempotency_skip"
    assert records[0].__dict__.get("guard_key") == SESSION_EXPIRED_EMAIL_SENT_KEY


def test_handle_emits_notification_queued_after_send(caplog: pytest.LogCaptureFixture) -> None:
    """session_expired.notification.queued emitted at DEBUG after send attempt."""
    svc = _make_svc(recipients=["a@example.com"])
    event = SseSessionExpired(timestamp=_NOW)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc._handle(event)

    records = [r for r in caplog.records if r.getMessage() == "session_expired.notification.queued"]
    assert records, "expected session_expired.notification.queued"
    assert records[0].__dict__.get("recipients_count") == 1
