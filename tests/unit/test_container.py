"""Tests for Infra and Services dataclasses.

Verifies:
1. Infra and Services are frozen (immutable).
2. Container is NOT frozen (mutable, allows supervisor rebinding).
3. repr() does not leak sensitive values.
4. Both can be instantiated with mock/placeholder objects.
"""

import dataclasses
import threading
from unittest.mock import Mock

import pytest

from fis_monitor.container import Container, Infra, Services


class TestInfraFrozen:
    """Infra MUST be frozen=True."""

    def test_infra_is_frozen_dataclass(self) -> None:
        """Verify frozen=True attribute."""
        assert Infra.__dataclass_params__.frozen is True

    def test_infra_raises_on_mutation(self) -> None:
        """Mutating a field raises FrozenInstanceError."""
        infra = Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=threading.Event(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            notif_repo=Mock(),
            cycles_repo=Mock(),
            smtp_creds_repo=Mock(),
            http_client=Mock(),
            list_parser=Mock(),
            detail_parser=Mock(),
            login_session=Mock(),
            session_probe=Mock(),
            autostart=Mock(),
            smtp_host_policy=Mock(),
            sse_streamer=Mock(),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            infra.clock = Mock()  # type: ignore


class TestServicesFrozen:
    """Services MUST be frozen=True."""

    def test_services_is_frozen_dataclass(self) -> None:
        """Verify frozen=True attribute."""
        assert Services.__dataclass_params__.frozen is True

    def test_services_raises_on_mutation(self) -> None:
        """Mutating a field raises FrozenInstanceError."""
        services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            services.notifier_dispatcher = Mock()  # type: ignore


class TestContainerNotFrozen:
    """Container MUST NOT be frozen (allows supervisor rebinding)."""

    def test_container_is_not_frozen(self) -> None:
        """Container allows field mutations."""
        infra = Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=threading.Event(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            notif_repo=Mock(),
            cycles_repo=Mock(),
            smtp_creds_repo=Mock(),
            http_client=Mock(),
            list_parser=Mock(),
            detail_parser=Mock(),
            login_session=Mock(),
            session_probe=Mock(),
            autostart=Mock(),
            smtp_host_policy=Mock(),
            sse_streamer=Mock(),
        )

        services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )

        container = Container(infra=infra, services=services)

        # Verify we can mutate container (not frozen)
        new_services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )
        container.services = new_services  # type: ignore
        assert container.services is new_services


class TestReprNoPII:
    """repr() MUST NOT expose sensitive values."""

    def test_infra_repr_no_values(self) -> None:
        """repr(infra) shows class name but hides field values."""
        infra = Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=threading.Event(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            notif_repo=Mock(),
            cycles_repo=Mock(),
            smtp_creds_repo=Mock(),
            http_client=Mock(),
            list_parser=Mock(),
            detail_parser=Mock(),
            login_session=Mock(),
            session_probe=Mock(),
            autostart=Mock(),
            smtp_host_policy=Mock(),
            sse_streamer=Mock(),
        )

        repr_str = repr(infra)

        # Should contain class name
        assert "Infra" in repr_str or "container" in repr_str.lower()
        # Should NOT contain Mock or object ids (which indicate __repr__ did show values)
        # (Custom repr=False should either omit all, or show a safe placeholder)
        # Main invariant: no secrets or sensitive Mock() stringified contents
        assert "Mock" not in repr_str

    def test_services_repr_no_values(self) -> None:
        """repr(services) shows class name but hides field values."""
        services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )

        repr_str = repr(services)

        # Should contain class name or be a safe placeholder
        assert "Services" in repr_str or "container" in repr_str.lower()
        # Should NOT leak Mock details
        assert "Mock" not in repr_str

    def test_container_repr_no_values(self) -> None:
        """repr(container) shows class name but hides field values."""
        infra = Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=threading.Event(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            notif_repo=Mock(),
            cycles_repo=Mock(),
            smtp_creds_repo=Mock(),
            http_client=Mock(),
            list_parser=Mock(),
            detail_parser=Mock(),
            login_session=Mock(),
            session_probe=Mock(),
            autostart=Mock(),
            smtp_host_policy=Mock(),
            sse_streamer=Mock(),
        )

        services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )

        container = Container(infra=infra, services=services)
        repr_str = repr(container)

        # Should contain class name
        assert "Container" in repr_str or "container" in repr_str.lower()
        # Should NOT leak Mock details
        assert "Mock" not in repr_str


class TestContainerConstruction:
    """Container can be constructed with mocks."""

    def test_construct_all_with_mocks(self) -> None:
        """All fields can be instantiated with Mock objects."""
        infra = Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=threading.Event(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            notif_repo=Mock(),
            cycles_repo=Mock(),
            smtp_creds_repo=Mock(),
            http_client=Mock(),
            list_parser=Mock(),
            detail_parser=Mock(),
            login_session=Mock(),
            session_probe=Mock(),
            autostart=Mock(),
            smtp_host_policy=Mock(),
            sse_streamer=Mock(),
        )

        services = Services(
            notifier_dispatcher=Mock(),
            monitor_cycle=Mock(),
            enrichment=Mock(),
            full_scan=Mock(),
            onboarding=Mock(),
            login=Mock(),
            settings_service=Mock(),
            smtp_test=Mock(),
            session_monitor=Mock(),
            diagnostics=Mock(),
            lot_query=Mock(),
            lot_user_state=Mock(),
            backfill=Mock(),
            dnd=Mock(),
            catchup_dismiss=Mock(),
        )

        container = Container(infra=infra, services=services)

        assert container.infra is infra
        assert container.services is services
        assert isinstance(container.infra.cycle_progress_signal, threading.Event)
