"""Tests for web/deps.py — FastAPI provider functions.

Verifies:
1. get_container reads Container from request.app.state.container.
2. Each provider returns the corresponding Services field.
3. FastAPI's Depends() injection actually wires through:
   a route declares `service: X = Depends(get_X)` and receives the
   container.services.X instance.
4. app.dependency_overrides[get_X] = fake substitutes the provider —
   the route receives the fake instead of the real service.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fis_monitor.container import Container, Infra, Services
from fis_monitor.web.deps import (
    get_container,
    get_diagnostics,
    get_enrichment,
    get_full_scan,
    get_login,
    get_lot_query,
    get_monitor_cycle,
    get_notifier_dispatcher,
    get_onboarding,
    get_session_monitor,
    get_settings_service,
    get_smtp_test,
)


@pytest.fixture
def mock_container() -> Container:
    """Container with Mock() for every field — no real services touched."""

    def _mock_infra() -> Infra:
        return Infra(
            clock=Mock(),
            event_bus=Mock(),
            conn_provider=Mock(),
            locker=Mock(),
            config_source=Mock(),
            cycle_progress_signal=Mock(),
            lot_repo=Mock(),
            user_state_repo=Mock(),
            settings_repo=Mock(),
            state_repo=Mock(),
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

    def _mock_services() -> Services:
        return Services(
            notifier_dispatcher=Mock(name="notifier_dispatcher"),
            monitor_cycle=Mock(name="monitor_cycle"),
            enrichment=Mock(name="enrichment"),
            full_scan=Mock(name="full_scan"),
            onboarding=Mock(name="onboarding"),
            login=Mock(name="login"),
            settings_service=Mock(name="settings_service"),
            smtp_test=Mock(name="smtp_test"),
            session_monitor=Mock(name="session_monitor"),
            diagnostics=Mock(name="diagnostics"),
            lot_query=Mock(name="lot_query"),
            lot_user_state=Mock(name="lot_user_state"),
            backfill=Mock(name="backfill"),
            dnd=Mock(name="dnd"),
            catchup_dismiss=Mock(name="catchup_dismiss"),
            session_expired_email=Mock(name="session_expired_email"),
        )

    return Container(infra=_mock_infra(), services=_mock_services())


@pytest.fixture
def app(mock_container: Container) -> FastAPI:
    """FastAPI app with mock_container wired into app.state.container."""
    app = FastAPI()
    app.state.container = mock_container
    return app


# ---------------------------------------------------------------------------
# Direct unit tests: providers return c.services.<field>
# ---------------------------------------------------------------------------


class TestGetContainer:
    def test_returns_container_from_app_state(
        self, app: FastAPI, mock_container: Container
    ) -> None:
        request = Mock()
        request.app = app
        assert get_container(request) is mock_container


PROVIDER_FIELD_MAP = [
    (get_notifier_dispatcher, "notifier_dispatcher"),
    (get_monitor_cycle, "monitor_cycle"),
    (get_enrichment, "enrichment"),
    (get_full_scan, "full_scan"),
    (get_onboarding, "onboarding"),
    (get_login, "login"),
    (get_settings_service, "settings_service"),
    (get_smtp_test, "smtp_test"),
    (get_session_monitor, "session_monitor"),
    (get_diagnostics, "diagnostics"),
    (get_lot_query, "lot_query"),
]


@pytest.mark.parametrize(("provider", "field_name"), PROVIDER_FIELD_MAP)
def test_provider_returns_services_field(
    provider, field_name: str, mock_container: Container
) -> None:
    """Each provider returns c.services.<field_name>."""
    result = provider(c=mock_container)
    expected = getattr(mock_container.services, field_name)
    assert result is expected


# ---------------------------------------------------------------------------
# Integration: FastAPI Depends() wiring + dependency_overrides
# ---------------------------------------------------------------------------


class TestDependsInjection:
    def test_route_receives_service_via_depends(
        self, app: FastAPI, mock_container: Container
    ) -> None:
        """A route declaring Depends(get_lot_query) receives container.services.lot_query."""
        captured: dict[str, object] = {}

        @app.get("/probe")
        def probe(svc=Depends(get_lot_query)) -> dict:
            captured["svc"] = svc
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get("/probe")

        assert response.status_code == 200
        assert captured["svc"] is mock_container.services.lot_query

    def test_dependency_override_substitutes_service(
        self, app: FastAPI, mock_container: Container
    ) -> None:
        """app.dependency_overrides[get_X] swaps the provider — route gets the fake."""
        fake_onboarding = Mock(name="fake_onboarding")
        app.dependency_overrides[get_onboarding] = lambda: fake_onboarding

        captured: dict[str, object] = {}

        @app.get("/probe-onboarding")
        def probe(svc=Depends(get_onboarding)) -> dict:
            captured["svc"] = svc
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get("/probe-onboarding")

        assert response.status_code == 200
        assert captured["svc"] is fake_onboarding
        # Real container's onboarding was NOT used.
        assert captured["svc"] is not mock_container.services.onboarding
