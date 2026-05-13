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

if TYPE_CHECKING:
    from fis_monitor.container import Container
    from fis_monitor.services.diagnostics import DiagnosticsService
    from fis_monitor.services.enrichment import EnrichmentService
    from fis_monitor.services.full_scan import FullScanService
    from fis_monitor.services.login import LoginService
    from fis_monitor.services.lot_query import LotQueryService
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
