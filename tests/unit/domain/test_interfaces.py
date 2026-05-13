"""Unit tests for domain/interfaces.py Protocol seams.

TDD coverage:
  1. Structural conformance: fake implementations satisfy each Protocol
     via duck-typing annotation (``_: Proto = Fake()``).
  2. Import guard: interfaces.py contains no forbidden imports
     (infra / services / web / composition).
  3. Notifier ClassVar: ``channel_id`` is declared as a ClassVar.
  4. runtime_checkable: Clock and Notifier support isinstance(); others do not.
  5. EventSubscription / ConfigSubscription moved from models.py are present.
"""

from __future__ import annotations

import ast
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from fis_monitor.domain.interfaces import (
    AutostartManager,
    Clock,
    ConfigSource,
    ConfigSubscription,
    ConnectionProvider,
    CyclesRepository,
    DetailParser,
    EventBus,
    EventSubscription,
    HttpClient,
    ListParser,
    Locker,
    LoginSession,
    LotRepository,
    MigrationRunner,
    NotificationsRepository,
    Notifier,
    SettingsRepository,
    SmtpCredentialsRepository,
    SmtpHostPolicy,
    UserStateRepository,
)
from fis_monitor.domain.models import (
    CycleResult,
    HttpResponse,
    LockHandle,
    Lot,
    LotPublicDTO,
    LotUpsertResult,
    LotUserState,
    NotificationRecord,
    NotifierConfig,
    NotifyResult,
    OnboardingState,
    ParsedDetail,
    ResolvedSmtpEndpoint,
    Settings,
    SmtpCredentials,
    SseEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INTERFACES_PY = (
    Path(__file__).parents[3] / "src" / "fis_monitor" / "domain" / "interfaces.py"
)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ===========================================================================
# Test: no forbidden imports
# ===========================================================================


def test_no_forbidden_imports() -> None:
    """interfaces.py must not import infra, services, web, or composition."""
    forbidden = {"infra", "services", "web", "composition"}
    source = INTERFACES_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(INTERFACES_PY))

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if forbidden & set(parts):
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if forbidden & set(parts):
                violations.append(node.module)

    assert not violations, f"Forbidden imports found: {violations}"


# ===========================================================================
# Test: Notifier has ClassVar channel_id
# ===========================================================================


def test_notifier_has_classvar_channel_id() -> None:
    """Notifier Protocol must declare channel_id as a ClassVar."""
    ann = Notifier.__annotations__  # type: ignore[attr-defined]
    assert "channel_id" in ann, "channel_id must be annotated on Notifier"
    ann_str = str(ann["channel_id"])
    assert "ClassVar" in ann_str, f"channel_id annotation must be ClassVar, got: {ann_str}"


# ===========================================================================
# Test: runtime_checkable only on Clock and Notifier
# ===========================================================================


def test_clock_is_runtime_checkable() -> None:
    class FakeClock:
        def now(self) -> datetime:
            return datetime.now(tz=UTC)

        def monotonic(self) -> float:
            return 0.0

    assert isinstance(FakeClock(), Clock)


def test_notifier_is_runtime_checkable() -> None:
    class FakeNotifier:
        channel_id: ClassVar[str] = "fake"
        display_name: ClassVar[str] = "Fake"
        description: ClassVar[str] = "A fake channel for testing."
        config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
        recipient_label: ClassVar[str] = "To"
        recipient_placeholder: ClassVar[str] = "recipient@example.com"

        def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
            return NotifyResult(ok=True, detail="ok", retryable=False)

        def test(self, recipient: str) -> NotifyResult:
            return NotifyResult(ok=True, detail="ok", retryable=False)

    assert isinstance(FakeNotifier(), Notifier)


def test_locker_is_not_runtime_checkable() -> None:
    """Locker is structural-only; isinstance() should raise TypeError."""
    with pytest.raises(TypeError):
        isinstance(object(), Locker)  # type: ignore[arg-type]


def test_event_bus_is_not_runtime_checkable() -> None:
    with pytest.raises(TypeError):
        isinstance(object(), EventBus)  # type: ignore[arg-type]


# ===========================================================================
# Test: structural conformance of all Protocol fakes
# ===========================================================================


# --- Layer 0 ----------------------------------------------------------------


class _FakeClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return 0.0


def test_clock_structural() -> None:
    _: Clock = _FakeClock()


class _FakeConnectionProvider:
    def get(self) -> Any:
        return None

    def close_all(self) -> None:
        pass


def test_connection_provider_structural() -> None:
    _: ConnectionProvider = _FakeConnectionProvider()


class _FakeLocker:
    def acquire(self) -> LockHandle:
        return LockHandle(pid=1, path="/tmp/test.lock")

    def release(self, handle: LockHandle) -> None:
        pass


def test_locker_structural() -> None:
    _: Locker = _FakeLocker()


class _FakeConfigSubscription:
    def __enter__(self) -> ConfigSubscription:
        return self  # type: ignore[return-value]

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        return None

    def unsubscribe(self) -> None:
        pass


class _FakeConfigSource:
    def current(self) -> Settings:
        return Settings()

    def subscribe(self, cb: Any) -> ConfigSubscription:
        return _FakeConfigSubscription()  # type: ignore[return-value]


def test_config_source_structural() -> None:
    _: ConfigSource = _FakeConfigSource()


class _FakeEventSubscription:
    def __enter__(self) -> EventSubscription[SseEvent]:
        return self  # type: ignore[return-value]

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        return None

    def unsubscribe(self) -> None:
        pass

    def iter(self) -> Iterator[SseEvent]:
        return iter([])


class _FakeEventBus:
    def publish(self, event: SseEvent) -> None:
        pass

    def subscribe(self) -> EventSubscription[SseEvent]:
        return _FakeEventSubscription()  # type: ignore[return-value]


def test_event_bus_structural() -> None:
    _: EventBus = _FakeEventBus()


# --- Layer 1 ----------------------------------------------------------------


class _FakeLotRepository:
    def upsert(self, lot: Lot, *, tracked: Any) -> LotUpsertResult:
        return LotUpsertResult(was_new=True, changes=[])

    def get(self, lot_id: int) -> Lot | None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return []

    def get_last_known_id(self, region: int) -> int | None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: Any, at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


def test_lot_repository_structural() -> None:
    _: LotRepository = _FakeLotRepository()


class _FakeUserStateRepository:
    def get(self, lot_id: int) -> LotUserState | None:
        return None

    def set_starred(self, lot_id: int, value: bool) -> None:
        pass

    def set_submitted(self, lot_id: int, value: bool, at: Any) -> None:
        pass

    def set_note(self, lot_id: int, note: Any) -> None:
        pass

    def mark_visited(self, at: datetime) -> None:
        pass

    def last_visit(self) -> datetime | None:
        return None


def test_user_state_repository_structural() -> None:
    _: UserStateRepository = _FakeUserStateRepository()


class _FakeNotificationsRepository:
    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
        return True

    def status_of(
        self, lot_id: int, channel: str, recipient: str
    ) -> Literal["pending", "sent", "permanent_fail"] | None:
        return None

    def mark_attempt(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> int | None:
        return 1

    def mark_sent(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> None:
        pass

    def mark_permanent_fail(
        self, lot_id: int, channel: str, recipient: str
    ) -> None:
        pass

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
        return []

    def list_recent(self, limit: int) -> list[NotificationRecord]:
        return []


def test_notifications_repository_structural() -> None:
    _: NotificationsRepository = _FakeNotificationsRepository()


def test_notifications_repository_mark_attempt_returns_none_when_terminal() -> None:
    """mark_attempt() returning None is a valid part of the Protocol contract.

    Per ADR-019 / R4-C4: if the row is already in a terminal state (sent or
    permanent_fail) a concurrent consumer may have won the race. The method
    MUST return None in that case; callers MUST skip the send.

    This test documents the None-path as an *expected* contract, not an error.
    """

    class _FakeTerminalRepo:
        """Fake where mark_attempt always returns None (terminal-state race)."""

        def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
            return False

        def status_of(
            self, lot_id: int, channel: str, recipient: str
        ) -> Literal["pending", "sent", "permanent_fail"] | None:
            return "sent"

        def mark_attempt(
            self, lot_id: int, channel: str, recipient: str, at: datetime
        ) -> int | None:
            # Row already terminal — caller must skip the send.
            return None

        def mark_sent(
            self, lot_id: int, channel: str, recipient: str, at: datetime
        ) -> None:
            pass

        def mark_permanent_fail(
            self, lot_id: int, channel: str, recipient: str
        ) -> None:
            pass

        def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
            return []

        def list_recent(self, limit: int) -> list[NotificationRecord]:
            return []

    repo: NotificationsRepository = _FakeTerminalRepo()

    result = repo.mark_attempt(1, "email", "user@example.com", _utcnow())
    assert result is None, (
        "mark_attempt must be allowed to return None when row is in terminal state "
        "(race-safe semantics per ADR-019 R4-C4)"
    )


class _FakeSettingsRepository:
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        pass

    def get_onboarding(self) -> OnboardingState:
        return OnboardingState.NOT_STARTED

    def set_onboarding(self, st: OnboardingState) -> None:
        pass


def test_settings_repository_structural() -> None:
    _: SettingsRepository = _FakeSettingsRepository()


class _FakeSmtpCredentialsRepository:
    def load(self) -> SmtpCredentials | None:
        return None

    def save(self, creds: SmtpCredentials) -> None:
        pass


def test_smtp_credentials_repository_structural() -> None:
    _: SmtpCredentialsRepository = _FakeSmtpCredentialsRepository()


class _FakeCyclesRepository:
    def open(self, region: int, at: datetime) -> int:
        return 1

    def close(self, cycle_id: int, result: CycleResult) -> None:
        pass

    def list_recent(self, limit: int) -> list[CycleResult]:
        return []


def test_cycles_repository_structural() -> None:
    _: CyclesRepository = _FakeCyclesRepository()


# --- Layer 2 ----------------------------------------------------------------


class _FakeHttpClient:
    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: Any = None,
    ) -> HttpResponse:
        return HttpResponse(status=200, text="", headers={}, final_url=url)


def test_http_client_structural() -> None:
    _: HttpClient = _FakeHttpClient()


class _FakeListParser:
    def parse(self, html: str) -> list[Any]:
        return []


def test_list_parser_structural() -> None:
    _: ListParser = _FakeListParser()


class _FakeDetailParser:
    def parse(self, html: str) -> ParsedDetail:
        return ParsedDetail(
            lat=None,
            lon=None,
            has_boundaries=None,
            date_update=None,
            raw_json={},
        )


def test_detail_parser_structural() -> None:
    _: DetailParser = _FakeDetailParser()


class _FakeLoginSession:
    def open_headed_login(self, *, deadline: float) -> Any:
        from fis_monitor.domain.models import LoginOutcome

        return LoginOutcome(success=False, cookies_updated=False, error=None)

    def cancel(self) -> None:
        pass


def test_login_session_structural() -> None:
    _: LoginSession = _FakeLoginSession()


class _FakeSmtpHostPolicy:
    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        return ResolvedSmtpEndpoint(
            ip="93.184.216.34",
            family=socket.AF_INET,
            port=port,
            original_host=host,
        )


def test_smtp_host_policy_structural() -> None:
    _: SmtpHostPolicy = _FakeSmtpHostPolicy()


class _FakeAutostartManager:
    def is_enabled(self) -> bool:
        return False

    def enable(self) -> None:
        pass

    def disable(self) -> None:
        pass


def test_autostart_manager_structural() -> None:
    _: AutostartManager = _FakeAutostartManager()


class _FakeMigrationRunner:
    def run(self, target_version: int) -> None:
        pass


def test_migration_runner_structural() -> None:
    _: MigrationRunner = _FakeMigrationRunner()


# --- Layer 3 ----------------------------------------------------------------


class _FakeNotifier:
    channel_id: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "Fake Channel"
    description: ClassVar[str] = "A fake notification channel used in tests."
    config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
    recipient_label: ClassVar[str] = "Recipient"
    recipient_placeholder: ClassVar[str] = "recipient@example.com"

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        return NotifyResult(ok=True, detail="sent", retryable=False)

    def test(self, recipient: str) -> NotifyResult:
        return NotifyResult(ok=True, detail="test ok", retryable=False)


def test_notifier_structural() -> None:
    _: Notifier = _FakeNotifier()


# --- Subscription handles ---------------------------------------------------


def test_event_subscription_structural() -> None:
    _: EventSubscription[SseEvent] = _FakeEventSubscription()


def test_config_subscription_structural() -> None:
    _: ConfigSubscription = _FakeConfigSubscription()


# ===========================================================================
# Test: __all__ exports all expected names
# ===========================================================================


def test_all_exports() -> None:
    from fis_monitor.domain import interfaces

    expected_protocols = {
        "Clock",
        "ConnectionProvider",
        "Locker",
        "ConfigSource",
        "EventBus",
        "LotRepository",
        "UserStateRepository",
        "NotificationsRepository",
        "SettingsRepository",
        "SmtpCredentialsRepository",
        "CyclesRepository",
        "HttpClient",
        "ListParser",
        "DetailParser",
        "LoginSession",
        "SmtpHostPolicy",
        "AutostartManager",
        "MigrationRunner",
        "Notifier",
        "EventSubscription",
        "ConfigSubscription",
    }
    missing = expected_protocols - set(interfaces.__all__)
    assert not missing, f"Missing from __all__: {missing}"
