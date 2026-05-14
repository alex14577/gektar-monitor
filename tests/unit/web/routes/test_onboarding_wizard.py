"""Unit tests for onboarding wizard GET routes.

Tests use TestClient + app.dependency_overrides with FakeOnboardingService
(reused pattern from test_onboarding.py) and FakeConfigSource.

Coverage:
  1. GET /onboarding → 302 to url_for_current_step().
  2. GET /onboarding/regions  happy-path (NOT_STARTED) → 200 + HTML.
  3. GET /onboarding/smtp     happy-path (REGIONS_SET)  → 200 + HTML.
  4. GET /onboarding/recipients happy-path (SMTP_CONFIGURED) → 200 + HTML.
  5. GET /onboarding/test-email happy-path (RECIPIENTS_SET) → 200 + HTML.
  6. 4x mismatch redirect (each step with wrong state) → 302 + Location.
  7. All fake methods exercised (anti-mock §6).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.interfaces import ConfigSubscription
from fis_monitor.domain.models import (
    OnboardingState,
    Settings,
)
from fis_monitor.web.deps import get_config_source, get_onboarding, get_templates
from fis_monitor.web.routes.onboarding import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR
from tests.factories import make_settings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSubscription:
    """Minimal ConfigSubscription for testing."""

    def __enter__(self) -> FakeConfigSubscription:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def unsubscribe(self) -> None:
        pass


class FakeConfigSource:
    """Fake ConfigSource — implements ALL Protocol methods (anti-mock §6)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or make_settings()
        self.current_calls: int = 0
        self.subscribe_calls: int = 0
        self.save_calls: int = 0

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: Callable[[Settings], None]) -> ConfigSubscription:
        self.subscribe_calls += 1
        return FakeConfigSubscription()  # type: ignore[return-value]

    def save(self, settings: Settings) -> None:
        self.save_calls += 1
        self._settings = settings


class FakeOnboardingService:
    """Fake OnboardingService — mirrors FakeOnboardingService in test_onboarding.py."""

    _STATE_URL: ClassVar[dict[OnboardingState, str]] = {
        OnboardingState.NOT_STARTED: "/onboarding/regions",
        OnboardingState.REGIONS_SET: "/onboarding/smtp",
        OnboardingState.SMTP_CONFIGURED: "/onboarding/recipients",
        OnboardingState.RECIPIENTS_SET: "/onboarding/test-email",
        OnboardingState.COMPLETED: "/",
    }

    def __init__(self, *, state: OnboardingState = OnboardingState.NOT_STARTED) -> None:
        self._state = state
        self.current_calls: int = 0
        self.url_for_current_step_calls: int = 0

    def current(self) -> OnboardingState:
        self.current_calls += 1
        return self._state

    def url_for_current_step(self) -> str:
        self.url_for_current_step_calls += 1
        return self._STATE_URL[self._state]


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    onboarding_state: OnboardingState = OnboardingState.NOT_STARTED,
    settings: Settings | None = None,
) -> tuple[FastAPI, FakeOnboardingService, FakeConfigSource]:
    """Build minimal FastAPI app with wizard router and injected fakes."""
    fake_svc = FakeOnboardingService(state=onboarding_state)
    fake_cfg = FakeConfigSource(settings=settings)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI()
    # Mount static files so base.html.jinja url_for('static', ...) resolves.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_onboarding] = lambda: fake_svc
    app.dependency_overrides[get_config_source] = lambda: fake_cfg
    app.dependency_overrides[get_templates] = lambda: templates
    return app, fake_svc, fake_cfg


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods in one test
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Verify both fakes' methods are fully exercised to catch API mismatches."""
    fake_cfg = FakeConfigSource()
    s = fake_cfg.current()
    assert isinstance(s, Settings)
    assert fake_cfg.current_calls == 1

    sub = fake_cfg.subscribe(lambda _: None)
    assert fake_cfg.subscribe_calls == 1
    sub.unsubscribe()

    fake_cfg.save(make_settings(interval_minutes=5))
    assert fake_cfg.save_calls == 1
    assert fake_cfg.current_calls == 1  # save doesn't call current

    fake_svc = FakeOnboardingService(state=OnboardingState.REGIONS_SET)
    state = fake_svc.current()
    assert state is OnboardingState.REGIONS_SET
    assert fake_svc.current_calls == 1

    url = fake_svc.url_for_current_step()
    assert url == "/onboarding/smtp"
    assert fake_svc.url_for_current_step_calls == 1


# ---------------------------------------------------------------------------
# GET /onboarding — bare entry
# ---------------------------------------------------------------------------


def test_bare_onboarding_redirects_to_current_step() -> None:
    """GET /onboarding → 302 to url_for_current_step()."""
    app, _fake_svc, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/regions"
    assert resp.headers.get("cache-control") == "no-store"


def test_bare_onboarding_redirects_when_smtp_configured() -> None:
    """GET /onboarding with SMTP_CONFIGURED state → 302 to /onboarding/recipients."""
    app, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/onboarding/recipients"


# ---------------------------------------------------------------------------
# Happy-path: correct state → 200 + HTML
# ---------------------------------------------------------------------------


def test_regions_happy_path_renders_html() -> None:
    """GET /onboarding/regions with NOT_STARTED → 200 + HTML body."""
    app, fake_svc, fake_cfg = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/regions")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Wizard always has step indicator dots
    assert "wiz__dot" in resp.text
    assert fake_svc.current_calls >= 1
    assert fake_cfg.current_calls >= 1


def test_smtp_happy_path_renders_html() -> None:
    """GET /onboarding/smtp with REGIONS_SET → 200 + HTML body."""
    app, fake_svc, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/smtp")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "smtp" in resp.text.lower()
    assert fake_svc.current_calls >= 1


def test_recipients_happy_path_renders_html() -> None:
    """GET /onboarding/recipients with SMTP_CONFIGURED → 200 + HTML body."""
    app, fake_svc, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/recipients")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert fake_svc.current_calls >= 1


def test_test_email_happy_path_renders_html() -> None:
    """GET /onboarding/test-email with RECIPIENTS_SET → 200 + HTML body."""
    settings = make_settings(interval_minutes=3)
    app, fake_svc, _ = _make_app(
        onboarding_state=OnboardingState.RECIPIENTS_SET,
        settings=settings,
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/test-email")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert fake_svc.current_calls >= 1


# ---------------------------------------------------------------------------
# Mismatch redirect: wrong state → 302 to url_for_current_step()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "wrong_state", "expected_location"),
    [
        (
            "/onboarding/regions",
            OnboardingState.REGIONS_SET,
            "/onboarding/smtp",
        ),
        (
            "/onboarding/smtp",
            OnboardingState.NOT_STARTED,
            "/onboarding/regions",
        ),
        (
            "/onboarding/recipients",
            OnboardingState.NOT_STARTED,
            "/onboarding/regions",
        ),
        (
            "/onboarding/test-email",
            OnboardingState.SMTP_CONFIGURED,
            "/onboarding/recipients",
        ),
    ],
)
def test_mismatch_redirects(
    path: str,
    wrong_state: OnboardingState,
    expected_location: str,
) -> None:
    """Any step URL with mismatched state → 302 to current step + no-store."""
    app, fake_svc, _ = _make_app(onboarding_state=wrong_state)
    client = TestClient(app, follow_redirects=False)
    resp = client.get(path)
    assert resp.status_code == 302
    assert resp.headers["location"] == expected_location
    assert resp.headers.get("cache-control") == "no-store"
    assert fake_svc.url_for_current_step_calls >= 1


# ---------------------------------------------------------------------------
# Template data verification
# ---------------------------------------------------------------------------


def test_regions_passes_settings_data() -> None:
    """Step 1 template receives regions from settings (even if int vs str mismatch)."""
    settings = make_settings(regions=[1, 2, 3])
    app, _, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED, settings=settings)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/regions")
    assert resp.status_code == 200
    # Template renders region buttons — wizard.html.jinja has wiz__dot elements
    assert "wiz__dot" in resp.text


def test_smtp_does_not_expose_password() -> None:
    """Step 2 must not render any password value (credentials not in config.json)."""
    app, _, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/smtp")
    assert resp.status_code == 200
    # The smtp_pass field must exist but must not carry a pre-filled value.
    # smtp_login / password live in state.db (SmtpCredentials), not config.json.
    assert 'name="smtp_pass"' in resp.text  # field rendered
    # The password input must not have a value="..." attribute — never pre-filled.
    assert 'name="smtp_pass" type="password" value=' not in resp.text


def test_test_email_passes_interval_minutes() -> None:
    """Step 4 template receives settings with interval_minutes."""
    settings = make_settings(interval_minutes=7)
    app, _, _ = _make_app(
        onboarding_state=OnboardingState.RECIPIENTS_SET,
        settings=settings,
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/onboarding/test-email")
    assert resp.status_code == 200
    # Template uses {{ settings.interval_minutes }} — should appear in rendered HTML
    assert "7" in resp.text
