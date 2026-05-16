"""Tests for build_container() — topological assembly per ADR-004.

Verifies:
1. Returns a Container instance.
2. Layer topology — concrete types for ready layers, stubs for deferred ones.
3. NotifierRegistry is built BEFORE Dispatcher (Dispatcher receives a populated registry).
4. smtp_host_policy is DefaultSmtpHostPolicy (R4-M12).
5. Protocol substitution — Container.services can be rebound (acceptance #6).
6. Topological smoke — build_container does not raise on the standard graph.
   (Full cycle detection is the job of import-linter in CI, see vgm.5.)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from fis_monitor.composition import build_container
from fis_monitor.container import Container, Infra, Services
from fis_monitor.domain.models import TargetConfig
from fis_monitor.infra.http.client import RequestsHttpClient
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier
from fis_monitor.infra.smtp.host_policy import DefaultSmtpHostPolicy
from fis_monitor.infra.sqlite.repositories.cycles import SqliteCyclesRepository
from fis_monitor.infra.sqlite.repositories.lots import SqliteLotRepository
from fis_monitor.infra.sqlite.repositories.notifications import (
    SqliteNotificationsRepository,
)
from fis_monitor.infra.sqlite.repositories.settings import SqliteSettingsRepository
from fis_monitor.infra.sqlite.repositories.smtp_credentials import (
    SqliteSmtpCredentialsRepository,
)
from fis_monitor.infra.sqlite.repositories.user_state import SqliteUserStateRepository
from fis_monitor.services.diagnostics.service import DiagnosticsService
from fis_monitor.services.full_scan import FullScanService
from fis_monitor.services.lot_query import LotQueryService
from fis_monitor.services.monitor_cycle import MonitorCycleService
from fis_monitor.services.notifier_dispatcher import NotifierDispatcher
from fis_monitor.services.onboarding import OnboardingService


@pytest.fixture
def container(tmp_path: Path) -> Container:
    """Standard container build into a temp data_dir."""
    return build_container(settings=None, data_dir=tmp_path)


class TestBuildContainerBasic:
    def test_returns_container(self, container: Container) -> None:
        assert isinstance(container, Container)
        assert isinstance(container.infra, Infra)
        assert isinstance(container.services, Services)


class TestBuildContainerLayerTopology:
    """Concrete types in Infra+Services for fields that are ready (Wave 1-5).

    Stubs are NOT asserted — their types are intentional private impls and
    locking those down would over-couple the test to file-local names.
    """

    def test_layer_1_repositories(self, container: Container) -> None:
        assert isinstance(container.infra.lot_repo, SqliteLotRepository)
        assert isinstance(container.infra.user_state_repo, SqliteUserStateRepository)
        assert isinstance(container.infra.settings_repo, SqliteSettingsRepository)
        assert isinstance(container.infra.notif_repo, SqliteNotificationsRepository)
        assert isinstance(container.infra.cycles_repo, SqliteCyclesRepository)
        assert isinstance(
            container.infra.smtp_creds_repo, SqliteSmtpCredentialsRepository
        )

    def test_layer_2_adapters(self, container: Container) -> None:
        assert isinstance(container.infra.http_client, RequestsHttpClient)
        # Parsers / login_session are concrete — checked by import alone.
        assert container.infra.list_parser is not None
        assert container.infra.detail_parser is not None
        assert container.infra.login_session is not None

    def test_http_client_default_timeout_uses_target_config(
        self, container: Container
    ) -> None:
        """default_timeout read-timeout follows TargetConfig.request_timeout_seconds."""
        client = container.infra.http_client
        assert isinstance(client, RequestsHttpClient)
        connect_t, read_t = client._default_timeout  # type: ignore[union-attr]
        assert connect_t == 5.0
        assert read_t == float(TargetConfig().request_timeout_seconds)

    def test_layer_4_services(self, container: Container) -> None:
        assert isinstance(
            container.services.notifier_dispatcher, NotifierDispatcher
        )
        assert isinstance(container.services.monitor_cycle, MonitorCycleService)
        assert isinstance(container.services.full_scan, FullScanService)
        assert isinstance(container.services.onboarding, OnboardingService)
        assert isinstance(container.services.diagnostics, DiagnosticsService)
        assert isinstance(container.services.lot_query, LotQueryService)


class TestNotifierRegistryBeforeDispatcher:
    """Acceptance #3 — Registry assembled BEFORE Dispatcher.

    Verified by: Dispatcher's registry is populated with channels
    ('email', 'browser') at the moment of inspection.
    """

    def test_registry_attached_to_dispatcher(self, container: Container) -> None:
        dispatcher = container.services.notifier_dispatcher
        registry = dispatcher._registry
        assert isinstance(registry, ExplicitNotifierRegistry)

    def test_registry_has_email_channel(self, container: Container) -> None:
        from fis_monitor.services.notifier_dispatcher import SubscribedAtFilteredNotifier

        dispatcher = container.services.notifier_dispatcher
        registry = dispatcher._registry
        email = registry.get("email")
        assert isinstance(email, SubscribedAtFilteredNotifier)
        assert isinstance(email._inner, SmtpEmailNotifier)

    def test_registry_has_browser_channel(self, container: Container) -> None:
        dispatcher = container.services.notifier_dispatcher
        registry = dispatcher._registry
        browser = registry.get("browser")
        assert browser is not None  # BrowserSseNotifier instance


class TestSmtpHostPolicyInLayer2:
    """Acceptance #4 (R4-M12) — smtp_host_policy is DefaultSmtpHostPolicy."""

    def test_smtp_host_policy_type(self, container: Container) -> None:
        assert isinstance(container.infra.smtp_host_policy, DefaultSmtpHostPolicy)


class TestProtocolSubstitution:
    """Acceptance #6 — Protocol implementations are substitutable.

    Demonstration: after build, container.services can be rebound to a new
    Services instance with a mocked monitor_cycle. Container is mutable
    (not frozen), Services is frozen — so we replace the whole Services.
    """

    def test_replace_services_with_mock_monitor_cycle(
        self, container: Container
    ) -> None:
        original_services = container.services
        mock_cycle = Mock(spec=MonitorCycleService)

        # Build a new Services with the mock; reuse other refs from original.
        new_services = Services(
            notifier_dispatcher=original_services.notifier_dispatcher,
            monitor_cycle=mock_cycle,
            enrichment=original_services.enrichment,
            full_scan=original_services.full_scan,
            onboarding=original_services.onboarding,
            login=original_services.login,
            settings_service=original_services.settings_service,
            smtp_test=original_services.smtp_test,
            session_monitor=original_services.session_monitor,
            diagnostics=original_services.diagnostics,
            lot_query=original_services.lot_query,
            lot_user_state=original_services.lot_user_state,
            backfill=original_services.backfill,
            dnd=original_services.dnd,
            catchup_dismiss=original_services.catchup_dismiss,
            session_expired_email=original_services.session_expired_email,
        )
        container.services = new_services

        assert container.services.monitor_cycle is mock_cycle


class TestTopologicalNoCycles:
    """Acceptance #5 — build_container assembles without raising.

    A real cycle in the dependency graph would surface as a TypeError or
    AttributeError at construction time. Static structural cycle detection
    is the job of import-linter (tracked in vgm.5/vgm.4), not runtime tests.
    """

    def test_build_does_not_raise(self, tmp_path: Path) -> None:
        c = build_container(settings=None, data_dir=tmp_path)
        # Sanity: both halves present, immutable Infra preserved.
        assert c.infra is not None
        assert c.services is not None

    def test_two_builds_are_independent(self, tmp_path: Path) -> None:
        """Each build_container call produces a fresh graph (no module-level state)."""
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        c1 = build_container(settings=None, data_dir=d1)
        c2 = build_container(settings=None, data_dir=d2)
        assert c1 is not c2
        assert c1.infra is not c2.infra
        assert c1.infra.lot_repo is not c2.infra.lot_repo
