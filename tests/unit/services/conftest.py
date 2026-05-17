# Shared fixtures for tests/unit/services (ADR-041 §Logging tests)

# --- dispatcher logging fixtures (bd cne5) ---
from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, ClassVar

from fis_monitor.domain.models import (
    LotPublicDTO,
    NotifyResult,
    Settings,
)
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.services.dnd import DndService
from fis_monitor.services.notifier_dispatcher import NotifierDispatcher
from tests.factories import make_lot
from tests.fakes.lot_repository import FakeLotRepository

DISPATCHER_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
DISPATCHER_LOGGER = "fis_monitor.services.notifier_dispatcher"


class FakeClock:
    def now(self) -> datetime:
        return DISPATCHER_NOW

    def monotonic(self) -> float:
        return 0.0


class FakeNotifRepo:
    def reserve(self, lot_id: int, channel: str, recipient: str) -> None:
        pass

    def status_of(self, lot_id: int, channel: str, recipient: str) -> str | None:
        return "sent"

    def mark_attempt(self, lot_id: int, channel: str, recipient: str, at: datetime) -> int | None:
        return 1

    def mark_sent(self, lot_id: int, channel: str, recipient: str, at: datetime) -> None:
        pass

    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None:
        pass

    def list_pending_older_than(self, age: Any) -> list[Any]:
        return []



class FakeEventSubscription:
    alive: bool = True

    def wait_one(self, timeout: float) -> None:
        return None

    def unsubscribe(self) -> None:
        pass

    def iter(self) -> list[Any]:
        return []


class FakeEventBus:
    def publish(self, event: Any) -> None:
        pass

    def subscribe(self) -> FakeEventSubscription:
        return FakeEventSubscription()


class FakeConfigSource:
    def __init__(self, recipients: list[str] | None = None) -> None:
        from fis_monitor.domain.models import EmailConfig, NotificationsConfig
        email_cfg = EmailConfig(enabled=True, recipients=list(recipients or []))
        notif_cfg = NotificationsConfig(email=email_cfg)
        self._settings = Settings(notifications=notif_cfg)

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class FakeRegionSubRepo:
    def get_subscribed_at(self, region_id: int) -> None:
        return None

    def set_if_absent(self, region_id: int, at: datetime) -> None:
        pass

    def delete(self, region_id: int) -> None:
        pass


class FakeSettingsRepo:
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


class FakeBrowserNotifier:
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


def make_dispatcher(
    *,
    recipients: list[str] | None = None,
    with_browser: bool = True,
) -> NotifierDispatcher:
    stop = threading.Event()
    registry = ExplicitNotifierRegistry()
    if with_browser:
        registry.register(FakeBrowserNotifier())
    dnd = DndService(settings_repo=FakeSettingsRepo())
    return NotifierDispatcher(
        registry=registry,
        notif_repo=FakeNotifRepo(),
        lot_repo=FakeLotRepository(),
        config_source=FakeConfigSource(recipients=recipients),
        clock=FakeClock(),
        event_bus=FakeEventBus(),
        stop_event=stop,
        dnd_service=dnd,
    )


def make_lot_dto(lot_id: int = 42, region_id: int = 77) -> LotPublicDTO:
    lot = make_lot(id=lot_id, region_id=region_id)
    from fis_monitor.domain.models import lot_to_public_dto
    return lot_to_public_dto(lot)


# ---------------------------------------------------------------------------
# Shared minimal fakes for MonitorCycleService satellite tests (ADR-041 §32ph)
# ---------------------------------------------------------------------------

from fis_monitor.domain.models import (  # noqa: E402
    CycleResult,
    HttpResponse,
    ParsedListPage,
    ParsedListRow,
)

_MC_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class MinimalHttpClient:
    """Stateless HttpClient fake — always returns 200 with empty HTML."""

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)


class MinimalListParser:
    """Stateless ListParser fake — returns an empty or configured page."""

    def __init__(self, rows: list[ParsedListRow] | None = None) -> None:
        self._rows = rows or []

    def parse(self, html: str) -> ParsedListPage:
        return ParsedListPage(rows=self._rows, total_count=len(self._rows))


class MinimalEnrichmentService:
    """Pass-through EnrichmentService fake — returns input lots unchanged."""

    def enrich_lots(self, lots: list[Any], *, max_workers: int) -> list[Any]:
        return list(lots)


class MinimalCyclesRepository:
    """CyclesRepository fake with auto-incrementing cycle ids."""

    def __init__(self) -> None:
        self._next_id = 1

    def open(self, region: int, at: datetime) -> int:
        cid = self._next_id
        self._next_id += 1
        return cid

    def close(self, cycle_id: int, result: CycleResult) -> None:
        pass

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


class MinimalNotifierDispatcher:
    """Stateless NotifierDispatcher fake — drops all dispatch calls."""

    def dispatch(self, lot: Any) -> None:
        pass


class MinimalEventBus:
    """Stateless EventBus fake — drops all publish calls."""

    def publish(self, event: Any) -> None:
        pass

    def subscribe(self) -> Any:
        raise NotImplementedError


class MinimalConfigSource:
    """ConfigSource fake returning a fixed Settings instance."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:
        raise NotImplementedError


class MinimalClock:
    """Clock fake with a fixed timestamp and zero monotonic."""

    def now(self) -> datetime:
        return _MC_NOW

    def monotonic(self) -> float:
        return 0.0
