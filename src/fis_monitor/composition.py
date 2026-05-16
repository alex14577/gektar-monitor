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
  - autostart        → a4t.9 (platform-specific AutostartManager)
  - session_probe    → a4t.8 (HttpSessionProbe)
  - login            → real LoginService (executor bound in lifespan per j19)
  - session_monitor  → a4t.7 (SessionMonitor)

See: docs/architecture/04-composition-root.md §4.2
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import requests

from fis_monitor.container import Container, Infra, Services
from fis_monitor.domain.models import Settings
from fis_monitor.infra.clock import SystemClock
from fis_monitor.infra.config_source import WatchdogConfigSource
from fis_monitor.infra.http.client import RequestsHttpClient
from fis_monitor.infra.http.cookie_bridge import RequestsCookieStore
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.infra.lock import FileLocker
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.infra.parsers.detail_parser import SelectolaxDetailParser
from fis_monitor.infra.parsers.list_parser import SelectolaxListParser
from fis_monitor.infra.playwright.login import PlaywrightLoginSession
from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier
from fis_monitor.infra.smtp.host_policy import DefaultSmtpHostPolicy
from fis_monitor.infra.smtp.provider_catalog import StaticSmtpProviderCatalog
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db
from fis_monitor.infra.sqlite.migrations import default_migration_runner
from fis_monitor.infra.sqlite.repositories.cycles import SqliteCyclesRepository
from fis_monitor.infra.sqlite.repositories.lots import SqliteLotRepository
from fis_monitor.infra.sqlite.repositories.notifications import (
    SqliteNotificationsRepository,
)
from fis_monitor.infra.sqlite.repositories.region_subscriptions import (
    SqliteRegionSubscriptionRepository,
)
from fis_monitor.infra.sqlite.repositories.settings import SqliteSettingsRepository
from fis_monitor.infra.sqlite.repositories.smtp_credentials import (
    SqliteSmtpCredentialsRepository,
)
from fis_monitor.infra.sqlite.repositories.state import SqliteStateRepository
from fis_monitor.infra.sqlite.repositories.user_state import SqliteUserStateRepository
from fis_monitor.infra.sse.browser_sse_notifier import BrowserSseNotifier
from fis_monitor.infra.sse.bus import ThreadEventBus
from fis_monitor.infra.sse.sse_stream import SseStreamer
from fis_monitor.services.backfill import BackfillService
from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.services.diagnostics.exclude_policy import DiagnosticsExcludePolicy
from fis_monitor.services.diagnostics.service import DiagnosticsService
from fis_monitor.services.dnd import DndService
from fis_monitor.services.enrichment import EnrichmentService
from fis_monitor.services.filter_matcher import AllFiltersMatcher, RfSubjectFilterMatcher
from fis_monitor.services.full_scan import FullScanService
from fis_monitor.services.login import LoginService
from fis_monitor.services.lot_query import LotQueryService
from fis_monitor.services.lot_user_state import LotUserStateService
from fis_monitor.services.monitor_cycle import MonitorCycleService
from fis_monitor.services.notifier_dispatcher import (
    NotifierDispatcher,
    SubscribedAtFilteredNotifier,
)
from fis_monitor.services.onboarding import OnboardingService
from fis_monitor.services.paginated_list_fetcher import PaginatedListFetcher
from fis_monitor.services.session_expired_email import SessionExpiredEmailService
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema SQL — loaded once at module level (no repeated I/O on build_container
# calls in tests).
# ---------------------------------------------------------------------------
def _resolve_schema_path() -> Path:
    # PyInstaller --onedir layout: bundled data sits under sys._MEIPASS
    # (= bin/_internal/), schema lands at _internal/docs/db/schema.sql per
    # build/fis-monitor.spec.  Dev layout: schema lives at <project-root>/docs/db/.
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "docs" / "db" / "schema.sql"
    return Path(__file__).resolve().parent.parent.parent / "docs" / "db" / "schema.sql"


_SCHEMA_SQL_PATH = _resolve_schema_path()


def _load_schema_sql() -> str:
    return _SCHEMA_SQL_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stubs for deferred implementations
# Each stub:
#   - Is private (prefix "_")
#   - Has a single docstring stating the deferred bd-issue
#   - Raises NotImplementedError("<class> impl deferred to <task>") on all methods
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Allowed hosts for PlaywrightLoginSession
# ---------------------------------------------------------------------------
# The login flow is: гектар /cabinet/ -> OAuth redirect -> ЕСИА login form ->
# back to гектар. Every host in this chain must be whitelisted; everything
# else (3rd-party analytics, ad networks, fonts CDNs) is aborted by the
# route handler — preventing cookie/UA exfil during a headed session.
#
# Entries starting with "." use suffix-match (see PlaywrightLoginSession.
# _make_route_handler) — chosen for the gosuslugi.ru subdomain fan-out
# (esia, id, lk, pos, static, …) which is too volatile to enumerate.
# We do NOT use a bare "*" or empty whitelist — that would be a security
# regression (any host could be contacted from a headed user session).
_TORGI_ALLOWED_HOSTS: tuple[str, ...] = (
    # Target site — гектар (надальнийвосток.рф) ─────────────────────────────
    "xn--80aaggvgieoeoa2bo7l.xn--p1ai",  # Punycode for надальнийвосток.рф
    "надальнийвосток.рф",  # unicode alias

    # Госуслуги OAuth / ЕСИА login chain ────────────────────────────────────
    # Suffix-match covers esia., id., lk., pos., static., my., … subdomains.
    # The OAuth code is hosted by Минцифры; the exact subdomain set changes
    # across releases (id.gosuslugi.ru is the newer flow, esia.gosuslugi.ru
    # the legacy one — both are live in 2026).
    ".gosuslugi.ru",
    "gosuslugi.ru",  # bare apex (some redirects land here briefly)
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

    Note: Three Infra/Services fields remain stubbed pending deferred
    bd-issues (autostart=bye.7, session_probe=a4t.9, session_monitor=a4t.9).
    Those stubs raise NotImplementedError on call. Every other field is a
    real implementation — build_container is fully runtime-exercisable on
    the monitoring + notification paths.
    """
    # ── Layer 0: systemwide utilities ──────────────────────────────────────
    clock = SystemClock()
    event_bus = ThreadEventBus()
    conn_provider = ConnectionProvider(db_path=data_dir / "state.db")
    locker = FileLocker(path=data_dir / "app.lock")
    cycle_progress_signal = threading.Event()

    # Initialise the schema (fresh DB → apply DDL; existing → verify version).
    # Migration runner handles the v1→v2 and v2→v3 upgrade paths.
    schema_sql = _load_schema_sql()
    init_db(
        conn_provider,
        schema_sql=schema_sql,
        latest_version=4,
        migration_runner=default_migration_runner(),
    )

    # ── Layer 1: repositories (depend on conn_provider + clock) ────────────
    lot_repo = SqliteLotRepository(conn_provider=conn_provider, clock=clock)
    user_state_repo = SqliteUserStateRepository(conn_provider=conn_provider, clock=clock)
    settings_repo = SqliteSettingsRepository(conn_provider=conn_provider, clock=clock)
    state_repo = SqliteStateRepository(conn_provider=conn_provider, clock=clock)
    notif_repo = SqliteNotificationsRepository(conn_provider=conn_provider, clock=clock)
    cycles_repo = SqliteCyclesRepository(conn_provider=conn_provider, clock=clock)
    smtp_creds_repo = SqliteSmtpCredentialsRepository(
        conn_provider=conn_provider, clock=clock
    )
    region_sub_repo = SqliteRegionSubscriptionRepository(conn_provider=conn_provider)

    # config_source is created after init_db so region_sub_repo can safely write
    # to region_subscriptions (ADR-039: WatchdogConfigSource._do_reload diff logic).
    config_source = WatchdogConfigSource(
        path=data_dir / "config.json",
        clock=clock,
        region_subs_repo=region_sub_repo,
    )

    # Build TorgiUrlBuilder from current settings (ADR-024).
    # base_url trailing slash is stripped by TargetConfig validator at construction;
    # no rstrip here.
    url_builder = TorgiUrlBuilder(base_url=config_source.current().target.base_url)

    # ── Layer 2: infra adapters ─────────────────────────────────────────────
    http_session = requests.Session()
    # cookie_store bridges Playwright-obtained cookies into requests.Session so
    # monitor_cycle / backfill requests carry valid session cookies (ADR-034).
    cookie_store = RequestsCookieStore(http_session)
    target_cfg = config_source.current().target
    http_client = RequestsHttpClient(
        session=http_session,
        verify=False,  # ADR-024: upstream uses self-signed cert in chain
        default_timeout=(5.0, float(target_cfg.request_timeout_seconds)),
    )
    list_parser = SelectolaxListParser()
    detail_parser = SelectolaxDetailParser()
    login_session = PlaywrightLoginSession(
        profile_dir=data_dir / "profile",
        allowed_hosts=_TORGI_ALLOWED_HOSTS,
        clock=clock,
        cookie_store=cookie_store,
    )
    session_probe = _NotImplementedSessionProbe()
    autostart = _NotImplementedAutostartManager()
    # R4-M12: SmtpHostPolicy — pure logic, no deps, instantiated in Layer 2.
    smtp_host_policy = DefaultSmtpHostPolicy()
    # ADR-038: SMTP provider catalog — pure in-memory dict, no I/O, Layer 2.
    smtp_provider_catalog = StaticSmtpProviderCatalog()

    # SseStreamer: constructed without executor (late-binding, ADR-014).
    # Executor is bound in lifespan via container.infra.sse_streamer.bind_executor().
    sse_streamer = SseStreamer(event_bus=event_bus)

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
        state_repo=state_repo,
        notif_repo=notif_repo,
        cycles_repo=cycles_repo,
        smtp_creds_repo=smtp_creds_repo,
        region_sub_repo=region_sub_repo,
        http_client=http_client,
        list_parser=list_parser,
        detail_parser=detail_parser,
        login_session=login_session,
        session_probe=session_probe,
        autostart=autostart,
        smtp_host_policy=smtp_host_policy,
        smtp_provider_catalog=smtp_provider_catalog,
        sse_streamer=sse_streamer,
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
    registry.register(SubscribedAtFilteredNotifier(
        inner=email_notifier,
        region_sub_repo=region_sub_repo,
    ))
    registry.register(BrowserSseNotifier(event_bus=event_bus))

    # ── Layer 4: use cases ──────────────────────────────────────────────────
    # dnd_service is built before NotifierDispatcher so it can be injected.
    dnd = DndService(settings_repo=settings_repo)

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
        dnd_service=dnd,
        settings_repo=settings_repo,
        retry_attempts=3,
        retry_backoff=(2.0, 4.0, 8.0),
    )

    enrichment = EnrichmentService(
        http=http_client,
        parser=detail_parser,
        url_builder=url_builder,
    )

    filter_matcher = AllFiltersMatcher([RfSubjectFilterMatcher()])

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
        filter_matcher=filter_matcher,
        url_builder=url_builder,
    )

    paginated_fetcher = PaginatedListFetcher(
        http=http_client,
        list_parser=list_parser,
        url_builder=url_builder,
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
        url_builder=url_builder,
        paginated_fetcher=paginated_fetcher,
    )

    onboarding = OnboardingService(
        settings_repo=settings_repo,
        config_source=config_source,
    )

    # Build backfill BEFORE login so the on_login_success closure can capture it.
    backfill = BackfillService(
        fetcher=paginated_fetcher,
        lot_repo=lot_repo,
        config_source=config_source,
        monitor_cycle=monitor_cycle,
        event_bus=event_bus,
    )
    # Late-bind backfill into monitor_cycle (breaks circular dep: monitor_cycle
    # was built before backfill, so it received backfill=None in its constructor).
    monitor_cycle.set_backfill(backfill)

    # ---------------------------------------------------------------------------
    # on_login_success: backfill auto-trigger on headed-login completion (f5u fix).
    #
    # ADR-032 originally triggered backfill from onboarding step-4 completion.
    # Prod logs showed the race: Playwright headed-login takes 10-60 s, but the
    # trigger fired immediately — backfill ran without valid session cookies,
    # got ParseBugErrors, produced 0 rows.  After the first monitor_cycle writes
    # lots, count_active() > 0, and the guard blocks all future auto-backfills.
    #
    # Fix: trigger on *login success* (headed login only, not silent refresh).
    # Guards remain identical: onboarding_completed AND count_active() == 0.
    # The supervisor reference is captured lazily as a mutable cell to avoid a
    # circular dependency (supervisor is created in app.py lifespan, after
    # build_container returns; None-guard prevents premature invocation during
    # test setups that skip lifespan).
    # ---------------------------------------------------------------------------
    _supervisor_cell: list[object] = [None]  # mutable cell; filled by app.py lifespan

    def _backfill_on_login_success(_outcome: object) -> None:
        # Secondary fallback only. Primary backfill trigger is delta-check in
        # MonitorCycleService. Activate when total_count=None ≥ N cycles AND db is empty.
        onboarding_state = onboarding.current()
        from fis_monitor.domain.models import OnboardingState  # local to avoid circular
        if onboarding_state != OnboardingState.COMPLETED:
            _log.debug(
                "on_login_success: onboarding not completed (state=%s) — skip backfill",
                onboarding_state,
            )
            return
        active = lot_repo.count_active()
        if active != 0:
            _log.debug(
                "on_login_success: catalogue not empty (count_active=%d) — skip backfill",
                active,
            )
            return
        current_settings = config_source.current()
        if not current_settings.regions:
            _log.debug("on_login_success: regions empty — skip backfill")
            return
        region_ids = list(current_settings.regions)
        all_skip_none = all(
            monitor_cycle.last_delta_decision(rid) == "skip_none" for rid in region_ids
        )
        if not all_skip_none:
            _log.debug(
                "on_login_success: delta-trigger operative for ≥1 region — skip secondary fallback"
            )
            return
        sup = _supervisor_cell[0]
        if sup is None:
            _log.warning(
                "on_login_success: supervisor not yet bound — backfill NOT started"
            )
            return
        _log.info("on_login_success: secondary fallback guards passed → auto-backfill scheduled")
        sup.start("backfill-auto", lambda stop: backfill.start(stop))  # type: ignore[union-attr]

    # Build SessionExpiredEmailService before login so the reset callback can
    # capture it via closure without circular reference.
    session_expired_email_stop = threading.Event()
    session_expired_email_svc = SessionExpiredEmailService(
        email_notifier=email_notifier,
        state_repo=state_repo,
        config_source=config_source,
        event_bus=event_bus,
        clock=clock,
        dnd_service=dnd,
        stop_event=session_expired_email_stop,
    )

    def _reset_session_expired_flag(_outcome: object) -> None:
        """Reset idempotency flag on any successful login or refresh."""
        session_expired_email_svc.on_login_or_refresh_success()

    login = LoginService(
        login_session=login_session,
        clock=clock,
        on_login_success=_backfill_on_login_success,
        on_any_success=_reset_session_expired_flag,
    )
    # Expose the supervisor cell so app.py lifespan can fill it after building
    # the supervisor. Stored on the login service instance for easy access.
    login._supervisor_cell = _supervisor_cell  # type: ignore[attr-defined]

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
    exclude_policy = DiagnosticsExcludePolicy()
    diagnostics = DiagnosticsService(
        data_dir=data_dir,
        conn_provider=conn_provider,
        clock=clock,
        exclude_policy=exclude_policy,
    )
    lot_query = LotQueryService(
        lot_repo=lot_repo,
        user_state_repo=user_state_repo,
        conn_provider=conn_provider,
        clock=clock,
    )

    lot_user_state = LotUserStateService(
        lot_repo=lot_repo,
        user_state_repo=user_state_repo,
    )

    catchup_dismiss = CatchupDismissService(state_repo=settings_repo, clock=clock)

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
        lot_user_state=lot_user_state,
        backfill=backfill,
        dnd=dnd,
        catchup_dismiss=catchup_dismiss,
        session_expired_email=session_expired_email_svc,
    )

    return Container(infra=infra, services=services)
