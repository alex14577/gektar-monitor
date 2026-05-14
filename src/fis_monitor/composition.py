"""Composition root: build_container() — topological assembly per ADR-004.

This module contains the single public entry point ``build_container()`` that
wires together all infrastructure and service-layer objects.  No DI framework
is used — explicit Python construction in topological (dependency) order.

Layer order:
  0 — clock, event_bus, conn_provider, locker, config_source, cycle_progress_signal
  1 — repositories (all depend on conn_provider + clock)
  2 — http_client, parsers, login_session, session_probe, autostart, smtp_host_policy
  3 — NotifierRegistry + Notifier plugins (built BEFORE Dispatcher)
  4 — NotifierDispatcher → then monitor_cycle, full_scan, enrichment, onboarding, …

Several fields are stubbed pending future bd-issues.  Stubs raise
``NotImplementedError`` on any method call — they satisfy structural Protocols
at import-time (Python Protocols without ``@runtime_checkable`` are purely
static) but will explode at runtime.  This is intentional: build_container is
a *structural* assembler, not a runtime exerciser.

Stubbed fields and their tracking tasks:
  - clock            → bye.9 (SystemClock)
  - config_source    → bye.9 (WatchdogConfigSource)
  - user_state_repo  → bye.7 (SqliteUserStateRepository)
  - autostart        → a4t.9 (platform-specific AutostartManager)
  - session_probe    → a4t.8 (HttpSessionProbe)
  - login            → real LoginService (executor bound in lifespan per j19)
  - session_monitor  → a4t.7 (SessionMonitor)
  - diagnostics      → a4t.8 (DiagnosticsService)
  - lot_query        → a4t.7 (LotQueryService)

See: docs/architecture/04-composition-root.md §4.2
"""

from __future__ import annotations

import threading
from pathlib import Path

import requests

from fis_monitor.container import Container, Infra, Services
from fis_monitor.domain.models import Settings
from fis_monitor.infra.clock import SystemClock
from fis_monitor.infra.config_source import WatchdogConfigSource
from fis_monitor.infra.http.client import RequestsHttpClient
from fis_monitor.infra.lock import FileLocker
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.infra.parsers.detail_parser import SelectolaxDetailParser
from fis_monitor.infra.parsers.list_parser import SelectolaxListParser
from fis_monitor.infra.playwright.login import PlaywrightLoginSession
from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier
from fis_monitor.infra.smtp.host_policy import DefaultSmtpHostPolicy
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db
from fis_monitor.infra.sqlite.migrations import default_migration_runner
from fis_monitor.infra.sqlite.repositories.cycles import SqliteCyclesRepository
from fis_monitor.infra.sqlite.repositories.lots import SqliteLotRepository
from fis_monitor.infra.sqlite.repositories.notifications import (
    SqliteNotificationsRepository,
)
from fis_monitor.infra.sqlite.repositories.settings import SqliteSettingsRepository
from fis_monitor.infra.sqlite.repositories.smtp_credentials import (
    SqliteSmtpCredentialsRepository,
)
from fis_monitor.infra.sse.browser_sse_notifier import BrowserSseNotifier
from fis_monitor.infra.sse.bus import ThreadEventBus
from fis_monitor.services.enrichment import EnrichmentService
from fis_monitor.services.full_scan import FullScanService
from fis_monitor.services.login import LoginService
from fis_monitor.services.monitor_cycle import MonitorCycleService
from fis_monitor.services.notifier_dispatcher import NotifierDispatcher
from fis_monitor.services.onboarding import OnboardingService
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService

# ---------------------------------------------------------------------------
# Schema SQL — loaded once at module level (no repeated I/O on build_container
# calls in tests).
# ---------------------------------------------------------------------------
_SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "db" / "schema.sql"


def _load_schema_sql() -> str:
    return _SCHEMA_SQL_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stubs for deferred implementations
# Each stub:
#   - Is private (prefix "_")
#   - Has a single docstring stating the deferred bd-issue
#   - Raises NotImplementedError("<class> impl deferred to <task>") on all methods
# ---------------------------------------------------------------------------

class _NotImplementedUserStateRepository:
    """Stub for UserStateRepository until bye.7 lands (SqliteUserStateRepository)."""

    def get(self, lot_id: int) -> object:
        raise NotImplementedError(
            "_NotImplementedUserStateRepository.get() deferred to bye.7"
        )

    def save(self, state: object) -> None:
        raise NotImplementedError(
            "_NotImplementedUserStateRepository.save() deferred to bye.7"
        )


class _NotImplementedAutostartManager:
    """Stub for AutostartManager until a4t.9 lands (platform-specific impl)."""

    def enable(self) -> None:
        raise NotImplementedError(
            "_NotImplementedAutostartManager.enable() deferred to a4t.9"
        )

    def disable(self) -> None:
        raise NotImplementedError(
            "_NotImplementedAutostartManager.disable() deferred to a4t.9"
        )

    def is_enabled(self) -> bool:
        raise NotImplementedError(
            "_NotImplementedAutostartManager.is_enabled() deferred to a4t.9"
        )


class _NotImplementedSessionProbe:
    """Stub for SessionProbe until a4t.8 lands (HttpSessionProbe)."""

    def check(self) -> object:
        raise NotImplementedError(
            "_NotImplementedSessionProbe.check() deferred to a4t.8"
        )


class _NotImplementedSessionMonitor:
    """Stub for SessionMonitor until a4t.7 lands."""

    def run_forever(self, stop_event: threading.Event) -> None:
        raise NotImplementedError(
            "_NotImplementedSessionMonitor.run_forever() deferred to a4t.7"
        )


class _NotImplementedDiagnosticsService:
    """Stub for DiagnosticsService until a4t.8 lands."""

    def generate_bundle(self) -> object:
        raise NotImplementedError(
            "_NotImplementedDiagnosticsService.generate_bundle() deferred to a4t.8"
        )


class _NotImplementedLotQueryService:
    """Stub for LotQueryService until a4t.7 lands."""

    def recent_feed(self, *, limit: int) -> list:
        raise NotImplementedError(
            "_NotImplementedLotQueryService.recent_feed() deferred to a4t.7"
        )


# ---------------------------------------------------------------------------
# Torgi.gov.ru allowed hosts for PlaywrightLoginSession
# ---------------------------------------------------------------------------
_TORGI_ALLOWED_HOSTS: tuple[str, ...] = (
    "torgi.gov.ru",
    "www.torgi.gov.ru",
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_container(settings: Settings | None, data_dir: Path) -> Container:
    """Topologically assemble Container per ADR-004.

    Layer order:
      0 — clock, event_bus, conn_provider, locker, config_source, cycle_progress_signal
      1 — repositories
      2 — http_client, parsers, login_session, session_probe, autostart, smtp_host_policy
      3 — NotifierRegistry + Notifiers (built BEFORE Dispatcher)
      4 — NotifierDispatcher → MonitorCycleService / FullScanService / EnrichmentService / etc.

    Raises:
        FrozenInstanceError: never (Container is mutable; Infra/Services are frozen).

    Note: Several Infra/Services fields are stubbed pending bd-issues
    (bye.7, bye.9, a4t.7-a4t.9). Stubs raise NotImplementedError on call.
    Build is structural, not runtime — assembled but not exercised.
    """
    # ── Layer 0: systemwide utilities ──────────────────────────────────────
    clock = SystemClock()
    event_bus = ThreadEventBus()
    conn_provider = ConnectionProvider(db_path=data_dir / "state.db")
    locker = FileLocker(path=data_dir / "app.lock")
    config_source = WatchdogConfigSource(path=data_dir / "config.json", clock=clock)
    cycle_progress_signal = threading.Event()

    # Initialise the schema (fresh DB → apply DDL; existing → verify version).
    # Migration runner handles the v1→v2 upgrade path.
    schema_sql = _load_schema_sql()
    init_db(
        conn_provider,
        schema_sql=schema_sql,
        latest_version=2,
        migration_runner=default_migration_runner(),
    )

    # ── Layer 1: repositories (depend on conn_provider + clock) ────────────
    lot_repo = SqliteLotRepository(conn_provider=conn_provider, clock=clock)
    user_state_repo = _NotImplementedUserStateRepository()
    settings_repo = SqliteSettingsRepository(conn_provider=conn_provider, clock=clock)
    notif_repo = SqliteNotificationsRepository(conn_provider=conn_provider, clock=clock)
    cycles_repo = SqliteCyclesRepository(conn_provider=conn_provider, clock=clock)
    smtp_creds_repo = SqliteSmtpCredentialsRepository(
        conn_provider=conn_provider, clock=clock
    )

    # ── Layer 2: infra adapters ─────────────────────────────────────────────
    http_session = requests.Session()
    http_client = RequestsHttpClient(session=http_session)
    list_parser = SelectolaxListParser()
    detail_parser = SelectolaxDetailParser()
    login_session = PlaywrightLoginSession(
        profile_dir=data_dir / "profile",
        allowed_hosts=_TORGI_ALLOWED_HOSTS,
        clock=clock,
    )
    session_probe = _NotImplementedSessionProbe()
    autostart = _NotImplementedAutostartManager()
    # R4-M12: SmtpHostPolicy — pure logic, no deps, instantiated in Layer 2.
    smtp_host_policy = DefaultSmtpHostPolicy()

    infra = Infra(
        clock=clock,
        event_bus=event_bus,
        conn_provider=conn_provider,
        locker=locker,
        config_source=config_source,
        cycle_progress_signal=cycle_progress_signal,
        lot_repo=lot_repo,
        user_state_repo=user_state_repo,
        settings_repo=settings_repo,
        notif_repo=notif_repo,
        cycles_repo=cycles_repo,
        smtp_creds_repo=smtp_creds_repo,
        http_client=http_client,
        list_parser=list_parser,
        detail_parser=detail_parser,
        login_session=login_session,
        session_probe=session_probe,
        autostart=autostart,
        smtp_host_policy=smtp_host_policy,
    )

    # ── Layer 3: Notifiers + registry (assembled BEFORE Dispatcher) ─────────
    # Production graph: no with_retry wrapper — retry-logic lives in Dispatcher
    # (durable retry poised on NotificationsRepository, survives restarts).
    # NOTE: HeartbeatNotifier (3rd notifier in canon §4.2) is deferred to bd
    # gektar_monitor-czs. Registry currently has email + browser only.
    email_notifier = SmtpEmailNotifier(
        smtp_creds_repo=smtp_creds_repo,
        config_source=config_source,
        clock=clock,
        host_policy=smtp_host_policy,
    )
    registry = ExplicitNotifierRegistry()
    registry.register(email_notifier)
    registry.register(BrowserSseNotifier(event_bus=event_bus))

    # ── Layer 4: use cases ──────────────────────────────────────────────────
    # Dispatcher built first — monitor_cycle and full_scan depend on it.
    dispatcher_stop_event = threading.Event()
    notifier_dispatcher = NotifierDispatcher(
        registry=registry,
        notif_repo=notif_repo,
        lot_repo=lot_repo,
        config_source=config_source,
        clock=clock,
        event_bus=event_bus,
        stop_event=dispatcher_stop_event,
        settings_repo=settings_repo,
        retry_attempts=3,
        retry_backoff=(2.0, 4.0, 8.0),
    )

    enrichment = EnrichmentService(
        http=http_client,
        parser=detail_parser,
    )

    monitor_cycle = MonitorCycleService(
        http=http_client,
        list_parser=list_parser,
        enrichment=enrichment,
        lot_repo=lot_repo,
        cycles_repo=cycles_repo,
        notifier_dispatcher=notifier_dispatcher,
        event_bus=event_bus,
        config_source=config_source,
        clock=clock,
        cycle_progress_signal=cycle_progress_signal,
    )

    full_scan = FullScanService(
        http=http_client,
        list_parser=list_parser,
        lot_repo=lot_repo,
        cycles_repo=cycles_repo,
        config_source=config_source,
        clock=clock,
        event_bus=event_bus,
        cycle_progress_signal=cycle_progress_signal,
    )

    onboarding = OnboardingService(
        settings_repo=settings_repo,
        config_source=config_source,
    )

    login = LoginService(login_session=login_session, clock=clock)

    settings_service = SettingsService(
        smtp_creds_repo=smtp_creds_repo,
        host_policy=smtp_host_policy,
    )

    smtp_test = SmtpTestService(
        notifier=email_notifier,
        settings_repo=settings_repo,
        clock=clock,
    )

    session_monitor = _NotImplementedSessionMonitor()
    diagnostics = _NotImplementedDiagnosticsService()
    lot_query = _NotImplementedLotQueryService()

    services = Services(
        notifier_dispatcher=notifier_dispatcher,
        monitor_cycle=monitor_cycle,
        enrichment=enrichment,
        full_scan=full_scan,
        onboarding=onboarding,
        login=login,
        settings_service=settings_service,
        smtp_test=smtp_test,
        session_monitor=session_monitor,
        diagnostics=diagnostics,
        lot_query=lot_query,
    )

    return Container(infra=infra, services=services)
