"""Composition root: dataclasses Infra and Services.

This module defines two frozen dataclasses that split the Container into
infrastructure/systemic layer (Infra) and business logic layer (Services).

- Infra: Layer 0-2 utilities, repositories, external-system adapters.
         Frozen=True for immutability; repr=False to prevent secret leakage
         in crash logs.
- Services: Layer 3-4 use cases. Frozen=True, also no repr for consistency.
- Container: Mutable wrapper (not frozen) that holds Infra+Services.
             Supervisor handles may rebind services in lifespan.

Design philosophy:
  1. High cohesion within each group (systemic vs. business logic).
  2. Clear boundary: services depend ONLY on Infra's Protocol interfaces.
  3. No God-Container: separated into manageable chunks.
  4. Immutability by default (frozen=True) but runtime-rebinding possible (Container not frozen).

See: docs/architecture/04-composition-root.md §4.1, docs/decisions/ADR-004*.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fis_monitor.domain.interfaces import (
    AutostartManager,
    Clock,
    ConfigSource,
    CyclesRepository,
    DetailParser,
    EventBus,
    HttpClient,
    ListParser,
    Locker,
    LoginSession,
    LotRepository,
    NotificationsRepository,
    RegionSubscriptionRepository,
    SettingsRepository,
    SmtpCredentialsRepository,
    SmtpHostPolicy,
    SmtpProviderCatalog,
    StateRepository,
    UserStateRepository,
)
from fis_monitor.infra.sqlite.connection import (
    ConnectionProvider as ThreadLocalConnectionProvider,
)

if TYPE_CHECKING:
    from typing import Protocol

    from fis_monitor.infra.sse.sse_stream import SseStreamer
    from fis_monitor.services.backfill import BackfillService
    from fis_monitor.services.catchup_dismiss import CatchupDismissService
    from fis_monitor.services.diagnostics import DiagnosticsService
    from fis_monitor.services.dnd import DndService
    from fis_monitor.services.enrichment import EnrichmentService
    from fis_monitor.services.full_scan import FullScanService
    from fis_monitor.services.login import LoginService
    from fis_monitor.services.lot_query import LotQueryService
    from fis_monitor.services.lot_user_state import LotUserStateService
    from fis_monitor.services.monitor_cycle import MonitorCycleService
    from fis_monitor.services.notifier_dispatcher import NotifierDispatcher
    from fis_monitor.services.onboarding import OnboardingService
    from fis_monitor.services.session_expired_email import SessionExpiredEmailService
    from fis_monitor.services.session_monitor import SessionMonitor
    from fis_monitor.services.settings import SettingsService
    from fis_monitor.services.smtp_test import SmtpTestService

    # Forward ref for SessionProbe (infra-internal Protocol, not in domain/interfaces.py)
    # Defined in composition.py via HttpSessionProbe or similar concrete implementation.
    # Protocol signature: check() -> SessionStatus
    class SessionProbe(Protocol):  # type: ignore[no-redef]
        """Quick probe to detect session expiry (infra-internal)."""
        def check(self) -> object: ...


@dataclass(frozen=True, repr=False)
class Infra:
    """Systemwide infrastructure: utilities, repositories, adapters.

    Layer topology (not dogmatic, but snapshot of current organization):
      Layer 0: systemwide utilities without dependencies
        - clock, event_bus, conn_provider, locker, config_source, cycle_progress_signal
      Layer 1: repositories (depend on conn_provider)
        - lot_repo, user_state_repo, settings_repo, notif_repo, cycles_repo, smtp_creds_repo
      Layer 2: external-system adapters (HTTP, parsers, login, SMTP, SSE)
        - http_client, list_parser, detail_parser, login_session, session_probe,
          autostart, smtp_host_policy, sse_streamer

    Frozen=True guarantees immutability — no runtime field mutations.
    repr=False prevents accidental secret leakage to crash logs via __repr__.

    Note on NotifierRegistry: it is composition-internal and NOT exposed here.
    Registry is built *within* build_container() to construct NotifierDispatcher,
    then discarded (registry lives inside Dispatcher, not in Container).
    """

    # ── Layer 0: systemwide utilities ──────────────────────────────────────
    clock: Clock
    """Wall-clock and monotonic time source. Used by all time-dependent services."""

    event_bus: EventBus
    """Sync→async bridge for SSE fan-out. Publish() reads event.priority ClassVar."""

    conn_provider: ThreadLocalConnectionProvider
    """Per-thread SQLite connection factory. CONCRETE implementation, not Protocol.
    (ConnectionProvider is infra-internal, repositories use it directly.)"""

    locker: Locker
    """Single-instance OS-level lock. Prevents concurrent app instances."""

    config_source: ConfigSource
    """Live configuration stream with hot-reload. Returns current Settings snapshot."""

    cycle_progress_signal: threading.Event
    """Soft-yield coordinator (R3-M8, ADR-005). Set/clear by MonitorCycleService,
    polled by EnrichmentService to avoid stepping on active cycle. Lost on restart."""

    # ── Layer 1: repositories (depend on Layer 0) ──────────────────────────
    lot_repo: LotRepository
    """Read/write lots with version-check upserts, tracked fields, geo-sync."""

    user_state_repo: UserStateRepository
    """Read/write per-user UI state: filters, paging, selections."""

    settings_repo: SettingsRepository
    """Read/write onboarding state and settings persistence."""

    state_repo: StateRepository
    """Generic KV store over the ``state`` table (get/set/delete).
    Unified repository for critical_event slots, session flags, and other
    single-key state that does not belong to a domain-specific repository."""

    notif_repo: NotificationsRepository
    """Read/write notifications with state machine: reserve → mark_attempt → mark_sent.
    Durable notification retry across restarts (N-C1, ADR-019)."""

    cycles_repo: CyclesRepository
    """Read/write monitor cycle metadata: start_time, end_time, result, error."""

    smtp_creds_repo: SmtpCredentialsRepository
    """Read/write SMTP credentials (encrypted, ADR-020: stored in state.db)."""

    region_sub_repo: RegionSubscriptionRepository
    """Read/write per-region subscription timestamps (ADR-039).
    Used by notifier_dispatcher to suppress lots older than subscribed_at."""

    # ── Layer 2: external-system adapters ──────────────────────────────────
    http_client: HttpClient
    """HTTP requests with configurable timeout and cookie persistence."""

    list_parser: ListParser
    """Parse HTML list page. Returns ParsedListRow[] (name, lot_id, geo, extra)."""

    detail_parser: DetailParser
    """Parse HTML detail page. Returns ParsedDetail (price, changes, metadata)."""

    login_session: LoginSession
    """Playwright-based headed login. Long-lived instance, one Browser per app instance."""

    session_probe: SessionProbe
    """Quick HTTP probe to detect session expiry (fetch login page, check response)."""

    autostart: AutostartManager
    """Platform-specific autostart registration (Windows registry, systemd, etc.)."""

    smtp_host_policy: SmtpHostPolicy
    """SMTP endpoint validation: resolve hostname, check A/AAAA/MX, manual STARTTLS (ADR-021)."""

    smtp_provider_catalog: SmtpProviderCatalog
    """Static catalog: email domain → pre-filled SMTP suggestion DTO (ADR-038).
    Pure in-memory lookup, no network I/O. Implementation: StaticSmtpProviderCatalog."""

    sse_streamer: SseStreamer
    """Sync EventBus → async text/event-stream bridge for SSE fan-out.

    Constructed without an executor in ``build_container()`` (composition root),
    then receives one via ``bind_executor()`` in lifespan startup, mirroring the
    late-binding pattern from ``LoginService`` (ADR-014).  The executor is a
    runtime resource created after wiring is complete.
    """


@dataclass(frozen=True, repr=False)
class Services:
    """Use cases (Layer 3-4): business logic.

    All services depend ONLY on Protocol interfaces from Infra, never on
    concrete implementation classes. This separation allows pluggable
    implementations and testability via mocking.

    Frozen=True for consistency with Infra, though Services are less
    likely to be mutated than Infra. repr=False to prevent PII leakage.

    Lifecycle:
      - Services are instantiated in build_container() (task 8ov.2).
      - Some services are given executor pools in lifespan hook (app.py §4.3.bis).
      - Supervisor handles may replace `container.services` with a new `Services` instance
        during lifespan (Container is not frozen, Services itself is).
    """

    notifier_dispatcher: NotifierDispatcher
    """Retry-logic dispatcher for notifications. Registry of Notifier plugins,
    durable state-machine for retries, publishes NotifyResult to event bus (N-C1, ADR-019)."""

    monitor_cycle: MonitorCycleService
    """Main polling loop: fetch list, upsert lots, trigger enrichment, publish events."""

    enrichment: EnrichmentService
    """Parallel enrichment (fetch detail pages). Polls cycle_progress_signal to avoid overlap."""

    full_scan: FullScanService
    """Full lot graph walk (offline/no-pagination). Periodic or manual trigger."""

    onboarding: OnboardingService
    """Onboarding wizard FSM: regions_set → smtp_configured → recipients_set → completed.
    See ADR-018."""

    login: LoginService
    """Headed login (browser-based). Long-lived LoginSession instance.
    Executor rebind in lifespan."""

    settings_service: SettingsService
    """Read/write side for config + smtp_credentials.
    Validates host policy before persist (R4-M12)."""

    smtp_test: SmtpTestService
    """One-shot SMTP test: send test email via current credentials. Read-only, no state changes."""

    session_monitor: SessionMonitor
    """Monitor session validity (periodic probe). Publishes expiry events."""

    diagnostics: DiagnosticsService
    """Diagnostic bundle generation: zips logs, app.jsonl, schema snapshot, excludes audit.jsonl
    if cloud-sync detected (R4-M7, ADR-010)."""

    lot_query: LotQueryService
    """Read-side for web: recent feed, search, pagination (read-only, no mutations)."""

    lot_user_state: LotUserStateService
    """Per-lot user-state mutations: toggle star/archive, set note, fetch details."""

    backfill: BackfillService
    """Paginated catalogue backfill — manual trigger + auto-on-empty."""

    dnd: DndService
    """Do-Not-Disturb window — used to suppress notifications."""

    catchup_dismiss: CatchupDismissService
    """Catch-up banner dismissal — state.db key catchup_dismissed_until."""

    session_expired_email: SessionExpiredEmailService
    """Sends one email per session-expiry epoch; idempotency via state_repo key."""


@dataclass(repr=False)
class Container:
    """Mutable wrapper for Infra + Services.

    NOT frozen — supervisor handles may rebind service fields during lifespan
    (e.g., replace monitor_cycle with a mock in tests, or rebind executor).

    repr=False to prevent secret leakage from __repr__.

    Invariant: Infra is always immutable (frozen=True), but Container itself
    allows mutation for runtime flexibility. In production, mutation is rare
    (mostly test/debug scenarios).

    Usage:
      - Create once in lifespan startup: container = build_container(...)
      - Store in app.state.container (FastAPI).
      - FastAPI Depends() reads from request.app.state.container.
      - Web handlers depend on specific services, not the container itself.
      - ThreadSupervisor holds references to container.services methods.

    See: docs/architecture/04-composition-root.md §4.1, §4.3.
    """

    infra: Infra
    """Immutable layer 0-2 infrastructure."""

    services: Services
    """Immutable layer 3-4 business logic. (Container may rebind, but Services itself is frozen.)"""
