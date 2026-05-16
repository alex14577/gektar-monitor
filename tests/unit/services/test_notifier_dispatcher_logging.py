"""Logging tests for NotifierDispatcher DEBUG events (gektar_monitor-b9wq).

Covers:
- dispatcher.dispatch.entry (lot_id, region_id, channels_count)
- dispatcher.channel.invoked (lot_id, channel_id, recipients_count)
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from fis_monitor.domain.models import (
    LotPublicDTO,
    NotifyResult,
    Settings,
)
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.services.dnd import DndService
from fis_monitor.services.notifier_dispatcher import NotifierDispatcher
from tests.factories import make_lot

_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
_LOGGER = "fis_monitor.services.notifier_dispatcher"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class _FakeNotifRepo:
    def reserve(self, lot_id: int, channel: str, recipient: str) -> None:
        pass

    def status_of(self, lot_id: int, channel: str, recipient: str) -> str | None:
        return "sent"  # skip delivery — we only test dispatch events here

    def mark_attempt(self, lot_id: int, channel: str, recipient: str, at: datetime) -> int | None:
        return 1

    def mark_sent(self, lot_id: int, channel: str, recipient: str, at: datetime) -> None:
        pass

    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None:
        pass

    def list_pending_older_than(self, age: Any) -> list[Any]:
        return []


class _FakeLotRepo:
    def get(self, lot_id: int) -> None:
        return None

    def upsert(self, lot: Any, *, tracked: Any) -> Any:
        raise NotImplementedError

    def list_active(self, *, limit: int, offset: int) -> list[Any]:
        return []

    def get_last_known_id(self, region: int) -> None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: list[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


class _FakeEventSubscription:
    alive: bool = True

    def wait_one(self, timeout: float) -> None:
        return None

    def unsubscribe(self) -> None:
        pass

    def iter(self) -> list[Any]:
        return []


class _FakeEventBus:
    def publish(self, event: Any) -> None:
        pass

    def subscribe(self) -> _FakeEventSubscription:
        return _FakeEventSubscription()


class _FakeConfigSource:
    def __init__(self, recipients: list[str] | None = None) -> None:
        from fis_monitor.domain.models import EmailConfig, NotificationsConfig
        email_cfg = EmailConfig(enabled=True, recipients=list(recipients or []))
        notif_cfg = NotificationsConfig(email=email_cfg)
        self._settings = Settings(notifications=notif_cfg)

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class _FakeRegionSubRepo:
    def get_subscribed_at(self, region_id: int) -> None:
        return None

    def set_if_absent(self, region_id: int, at: datetime) -> None:
        pass

    def delete(self, region_id: int) -> None:
        pass


class _FakeSettingsRepo:
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


class _FakeBrowserNotifier:
    channel_id: ClassVar[str] = "browser"
    display_name: ClassVar[str] = "Browser"
    description: ClassVar[str] = ""
    config_schema: ClassVar[type] = type(None)
    recipient_label: ClassVar[str] = "local"
    recipient_placeholder: ClassVar[str] = ""

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        return NotifyResult(ok=True, detail="ok", retryable=False)

    def test(self, recipient: str) -> NotifyResult:
        return NotifyResult(ok=True, detail="ok", retryable=False)


def _make_dispatcher(
    *,
    recipients: list[str] | None = None,
    with_browser: bool = True,
) -> NotifierDispatcher:
    stop = threading.Event()
    registry = ExplicitNotifierRegistry()
    if with_browser:
        registry.register(_FakeBrowserNotifier())
    clock = _FakeClock()
    dnd = DndService(settings_repo=_FakeSettingsRepo())
    return NotifierDispatcher(
        registry=registry,
        notif_repo=_FakeNotifRepo(),
        lot_repo=_FakeLotRepo(),
        config_source=_FakeConfigSource(recipients=recipients),
        clock=clock,
        event_bus=_FakeEventBus(),
        stop_event=stop,
        dnd_service=dnd,
    )


def _make_lot_dto(lot_id: int = 42, region_id: int = 77) -> LotPublicDTO:
    lot = make_lot(id=lot_id, region_id=region_id)
    from fis_monitor.domain.models import lot_to_public_dto
    return lot_to_public_dto(lot)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dispatch_emits_dispatch_entry_debug(caplog: pytest.LogCaptureFixture) -> None:
    """dispatcher.dispatch.entry emitted with lot_id + region_id + channels_count."""
    dispatcher = _make_dispatcher()
    lot = _make_lot_dto(lot_id=55, region_id=99)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        dispatcher.dispatch(lot)

    records = [r for r in caplog.records if r.getMessage() == "dispatcher.dispatch.entry"]
    assert records, "expected dispatcher.dispatch.entry debug event"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 55
    assert rec.__dict__.get("region_id") == 99
    assert "channels_count" in rec.__dict__


def test_dispatch_channels_emits_channel_invoked_debug(caplog: pytest.LogCaptureFixture) -> None:
    """dispatcher.channel.invoked emitted for each registered channel."""
    dispatcher = _make_dispatcher(with_browser=True)
    lot = _make_lot_dto(lot_id=55)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        dispatcher._dispatch_all_channels(lot)

    records = [r for r in caplog.records if r.getMessage() == "dispatcher.channel.invoked"]
    assert records, "expected dispatcher.channel.invoked debug event"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 55
    assert rec.__dict__.get("channel_id") == "browser"
    assert "recipients_count" in rec.__dict__
