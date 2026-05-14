"""FastAPI Depends() providers per use case.

Canonical pattern per docs/architecture/04-composition-root.md §4.3:
the lifespan hook stores `container` in `app.state.container`; routes
declare typed dependencies on individual use cases via `Depends(...)`.
This keeps routes decoupled from the Container itself — they see only
the use case they actually need, which makes them trivial to test by
overriding `app.dependency_overrides[get_X]` with a fake.

Adding a new use case: add a `get_<name>` function that returns
`c.services.<name>`. Do NOT expose the Container directly to routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from fis_monitor.services.view_filters import ViewFiltersService

if TYPE_CHECKING:
    from fis_monitor.container import Container
    from fis_monitor.domain.interfaces import Clock, ConfigSource, NotificationsRepository
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
    from fis_monitor.services.session_monitor import SessionMonitor
    from fis_monitor.services.settings import SettingsService
    from fis_monitor.services.smtp_test import SmtpTestService


def get_container(request: Request) -> Container:
    """Root provider: read Container from app.state.

    Set in the lifespan hook (`app.state.container = build_container(...)`).
    Every other provider in this module depends on this one — routes never
    call it directly.
    """
    return request.app.state.container


def get_notifier_dispatcher(
    c: Container = Depends(get_container),
) -> NotifierDispatcher:
    return c.services.notifier_dispatcher


def get_monitor_cycle(
    c: Container = Depends(get_container),
) -> MonitorCycleService:
    return c.services.monitor_cycle


def get_enrichment(c: Container = Depends(get_container)) -> EnrichmentService:
    return c.services.enrichment


def get_full_scan(c: Container = Depends(get_container)) -> FullScanService:
    return c.services.full_scan


def get_onboarding(c: Container = Depends(get_container)) -> OnboardingService:
    return c.services.onboarding


def get_login(c: Container = Depends(get_container)) -> LoginService:
    return c.services.login


def get_settings_service(
    c: Container = Depends(get_container),
) -> SettingsService:
    return c.services.settings_service


def get_smtp_test(c: Container = Depends(get_container)) -> SmtpTestService:
    return c.services.smtp_test


def get_session_monitor(
    c: Container = Depends(get_container),
) -> SessionMonitor:
    return c.services.session_monitor


def get_diagnostics(
    c: Container = Depends(get_container),
) -> DiagnosticsService:
    return c.services.diagnostics


def get_lot_query(c: Container = Depends(get_container)) -> LotQueryService:
    return c.services.lot_query


def get_lot_user_state_service(
    c: Container = Depends(get_container),
) -> LotUserStateService:
    """Return LotUserStateService from the composition root.

    Composition wires this in build_container(); route tests override via
    ``app.dependency_overrides[get_lot_user_state_service]``.
    """
    return c.services.lot_user_state


def get_notifications_repo(
    c: Container = Depends(get_container),
) -> NotificationsRepository:
    return c.infra.notif_repo


def get_sse_streamer(request: Request) -> SseStreamer:
    """Return SseStreamer from the composition root.

    Stored at ``app.state.container.infra.sse_streamer`` by ``build_container``.
    Route tests override this via ``app.dependency_overrides``.
    """
    return request.app.state.container.infra.sse_streamer


def get_config_source(c: Container = Depends(get_container)) -> ConfigSource:
    """Return the live ConfigSource from the composition root.

    ``config_source`` lives on ``Infra`` (layer 0 — systemic utility without
    business-logic dependencies).  Routes use this to serve a Settings snapshot
    without depending on the Container directly.
    """
    return c.infra.config_source


def get_session_probe(c: Container = Depends(get_container)) -> object:
    """Return the SessionProbe from the composition root.

    ``session_probe`` lives on ``Infra`` (layer 2 — external-system adapter).
    Protocol: ``check() -> SessionStatus``.
    Route tests override via ``app.dependency_overrides[get_session_probe]``.
    """
    return c.infra.session_probe


def get_backfill(c: Container = Depends(get_container)) -> BackfillService:
    """Return BackfillService from the composition root.

    Composition wires this in build_container() as ``c.services.backfill``.
    Route tests override via ``app.dependency_overrides[get_backfill]``.
    """
    return c.services.backfill  # type: ignore[return-value]


def get_dnd_service(c: Container = Depends(get_container)) -> DndService:
    """Return DndService from the composition root.

    Composition wires this in build_container() as ``c.services.dnd``.
    Route tests override via ``app.dependency_overrides[get_dnd_service]``.
    """
    return c.services.dnd  # type: ignore[return-value]


def get_clock(request: Request) -> Clock:
    """Return the Clock from the composition root.

    Stored at ``app.state.container.infra.clock`` by ``build_container``.
    Route tests override this via ``app.dependency_overrides[get_clock]``.
    """
    return request.app.state.container.infra.clock  # type: ignore[return-value]


def get_catchup_dismiss(
    c: Container = Depends(get_container),
) -> CatchupDismissService:
    """Return CatchupDismissService from the composition root.

    Composition wires this in build_container() as ``c.services.catchup_dismiss``.
    Route tests override via ``app.dependency_overrides[get_catchup_dismiss]``.
    """
    return c.services.catchup_dismiss  # type: ignore[return-value]


def get_view_filters_service() -> ViewFiltersService:
    """Return a stateless ViewFiltersService instance.

    The service has no external dependencies and is constructed fresh per-request.
    The function identity is the stable DI key for dependency_overrides in tests.
    """
    return ViewFiltersService()


def get_templates(request: Request) -> Jinja2Templates:
    """Return Jinja2Templates from app.state.

    Set in the lifespan hook (``app.state.templates = Jinja2Templates(...)``).
    Route tests override this via ``app.dependency_overrides[get_templates]``.
    """
    return request.app.state.templates


def get_csrf_origin_whitelist(request: Request) -> frozenset[str]:
    """Return the loopback Origin whitelist stored in app.state.

    Set in the lifespan hook alongside the CSRF middleware construction:
    ``app.state.csrf_origin_whitelist = origin_whitelist``.

    The whitelist values are already normalised to lowercase by
    ``loopback_csrf_config`` → no case-folding needed here.
    """
    return request.app.state.csrf_origin_whitelist
