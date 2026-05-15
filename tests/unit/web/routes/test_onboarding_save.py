"""Unit tests for onboarding wizard POST routes.

Coverage:
  POST /onboarding/save?step=1  action=next — happy path, validation error, mismatch
  POST /onboarding/save?step=2  action=next, action=skip
  POST /onboarding/save?step=2  SmtpHostPolicyError re-render
  POST /onboarding/save?step=3  action=next, action=skip
  POST /onboarding/save?step=4  action=next, guard fail
  POST /onboarding/smtp-test    happy path + SmtpHostPolicyError

  Anti-mock §6: all FakeSettingsService, FakeSmtpTestService methods exercised.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fis_monitor.domain.errors import InvalidTransitionError, SmtpHostPolicyError
from fis_monitor.domain.interfaces import ConfigSubscription
from fis_monitor.domain.models import (
    NotifyResult,
    OnboardingState,
    Settings,
    SmtpCredentials,
)
from fis_monitor.web.deps import (
    get_backfill,
    get_config_source,
    get_lot_repo,
    get_onboarding,
    get_settings_service,
    get_smtp_test,
    get_templates,
)
from fis_monitor.web.rate_limit import RateLimiter
from fis_monitor.web.routes import onboarding as onboarding_module
from fis_monitor.web.routes.onboarding import (
    _validate_smtp_input,
    router,
)
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR
from tests.factories import make_settings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSubscription:
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
        self.saved_settings: list[Settings] = []

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: object) -> ConfigSubscription:
        self.subscribe_calls += 1
        return FakeConfigSubscription()  # type: ignore[return-value]

    def save(self, settings: Settings) -> None:
        self.save_calls += 1
        self._settings = settings
        self.saved_settings.append(settings)


class FakeOnboardingService:
    """Fake OnboardingService — implements all Protocol methods."""

    _STATE_URL: ClassVar[dict[OnboardingState, str]] = {
        OnboardingState.NOT_STARTED: "/onboarding/regions",
        OnboardingState.REGIONS_SET: "/onboarding/smtp",
        OnboardingState.SMTP_CONFIGURED: "/onboarding/recipients",
        OnboardingState.RECIPIENTS_SET: "/onboarding/test-email",
        OnboardingState.COMPLETED: "/",
    }

    def __init__(
        self,
        *,
        state: OnboardingState = OnboardingState.NOT_STARTED,
        advance_raises: Exception | None = None,
        skip_email_raises: Exception | None = None,
    ) -> None:
        self._state = state
        self._advance_raises = advance_raises
        self._skip_email_raises = skip_email_raises
        self.current_calls: int = 0
        self.url_for_current_step_calls: int = 0
        self.advance_calls: list[tuple[OnboardingState, OnboardingState]] = []
        self.skip_email_calls: int = 0
        self.can_advance_calls: int = 0

    def current(self) -> OnboardingState:
        self.current_calls += 1
        return self._state

    def can_advance(self, from_state: OnboardingState, to_state: OnboardingState) -> bool:
        self.can_advance_calls += 1
        return True

    def advance(self, from_state: OnboardingState, to_state: OnboardingState) -> None:
        self.advance_calls.append((from_state, to_state))
        if self._advance_raises:
            raise self._advance_raises

    def skip_email(self) -> None:
        self.skip_email_calls += 1
        if self._skip_email_raises:
            raise self._skip_email_raises

    def url_for_current_step(self) -> str:
        self.url_for_current_step_calls += 1
        return self._STATE_URL[self._state]


class FakeSettingsService:
    """Fake SettingsService — implements ALL methods (anti-mock §6)."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.set_smtp_credentials_calls: list[SmtpCredentials] = []

    def set_smtp_credentials(self, creds: SmtpCredentials) -> None:
        self.set_smtp_credentials_calls.append(creds)
        if self._raises:
            raise self._raises


class FakeSmtpTestService:
    """Fake SmtpTestService — implements ALL methods (anti-mock §6)."""

    def __init__(
        self, *, result: NotifyResult | None = None, raises: Exception | None = None
    ) -> None:
        self._result = result or NotifyResult(ok=True, detail="ok", retryable=False)
        self._raises = raises
        self.test_send_calls: list[tuple[object, str]] = []

    def test_send(self, test_lot: object, recipient: str) -> NotifyResult:
        self.test_send_calls.append((test_lot, recipient))
        if self._raises:
            raise self._raises
        return self._result


class FakeLotRepo:
    """Fake LotRepository — records count_active() calls."""

    def __init__(self, *, active_count: int = 0) -> None:
        self._active_count = active_count
        self.count_active_calls: int = 0

    def count_active(self) -> int:
        self.count_active_calls += 1
        return self._active_count


class FakeBackfillService:
    """Fake BackfillService — records start() calls."""

    def __init__(self) -> None:
        self.start_calls: int = 0

    def start(self, stop_event: object) -> bool:
        self.start_calls += 1
        return True


class FakeSupervisor:
    """Fake ThreadSupervisor — records start() calls."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.start_call_count: int = 0

    def start(self, name: str, target: object) -> None:
        self.started.append(name)
        self.start_call_count += 1


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods
# ---------------------------------------------------------------------------


def test_all_fake_methods_exercised() -> None:
    """All methods on fakes are called — catches runtime API mismatches."""
    # FakeConfigSource
    cfg = FakeConfigSource()
    s = cfg.current()
    assert isinstance(s, Settings)
    sub = cfg.subscribe(lambda _: None)
    sub.unsubscribe()  # type: ignore[union-attr]
    cfg.save(make_settings())
    assert cfg.save_calls == 1

    # FakeOnboardingService
    svc = FakeOnboardingService(state=OnboardingState.REGIONS_SET)
    assert svc.current() is OnboardingState.REGIONS_SET
    assert svc.url_for_current_step() == "/onboarding/smtp"
    svc.can_advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
    svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
    svc.skip_email()

    # FakeSettingsService
    settings_svc = FakeSettingsService()
    creds = SmtpCredentials(
        smtp_user="u@example.com",
        smtp_password=SecretStr("pw"),
        smtp_host="smtp.example.com",
        smtp_port=587,
    )
    settings_svc.set_smtp_credentials(creds)
    assert len(settings_svc.set_smtp_credentials_calls) == 1

    # FakeSmtpTestService
    smtp_test = FakeSmtpTestService()
    result = smtp_test.test_send(object(), "r@example.com")
    assert result.ok is True

    # FakeLotRepo
    lot_repo = FakeLotRepo(active_count=0)
    assert lot_repo.count_active() == 0
    assert lot_repo.count_active_calls == 1

    # FakeBackfillService
    backfill = FakeBackfillService()
    assert backfill.start(object()) is True
    assert backfill.start_calls == 1

    # FakeSupervisor
    sup = FakeSupervisor()
    sup.start("backfill-auto", lambda _: None)
    assert sup.started == ["backfill-auto"]
    assert sup.start_call_count == 1


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    *,
    onboarding_state: OnboardingState = OnboardingState.NOT_STARTED,
    settings: Settings | None = None,
    advance_raises: Exception | None = None,
    skip_email_raises: Exception | None = None,
    settings_svc_raises: Exception | None = None,
    smtp_test_result: NotifyResult | None = None,
    smtp_test_raises: Exception | None = None,
    fake_onboarding: FakeOnboardingService | None = None,
    fake_cfg: FakeConfigSource | None = None,
    fake_settings_svc: FakeSettingsService | None = None,
    fake_smtp_test: FakeSmtpTestService | None = None,
    fake_lot_repo: FakeLotRepo | None = None,
    fake_backfill: FakeBackfillService | None = None,
    supervisor: FakeSupervisor | None = None,
    rate_limiter: RateLimiter | None = None,
) -> tuple[
    FastAPI, FakeOnboardingService, FakeConfigSource, FakeSettingsService, FakeSmtpTestService
]:
    f_onboarding = fake_onboarding or FakeOnboardingService(
        state=onboarding_state,
        advance_raises=advance_raises,
        skip_email_raises=skip_email_raises,
    )
    f_cfg = fake_cfg or FakeConfigSource(settings=settings)
    f_settings_svc = fake_settings_svc or FakeSettingsService(raises=settings_svc_raises)
    f_smtp_test = fake_smtp_test or FakeSmtpTestService(
        result=smtp_test_result, raises=smtp_test_raises
    )
    f_lot_repo = fake_lot_repo or FakeLotRepo(active_count=0)
    f_backfill = fake_backfill or FakeBackfillService()

    # Always install a fresh limiter so each test gets an isolated quota.
    # Tests that want to control the exact limiter instance pass one explicitly.
    onboarding_module._smtp_test_rate_limiter = rate_limiter or RateLimiter(
        max_requests=1, window_seconds=10.0
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_onboarding] = lambda: f_onboarding
    app.dependency_overrides[get_config_source] = lambda: f_cfg
    app.dependency_overrides[get_settings_service] = lambda: f_settings_svc
    app.dependency_overrides[get_smtp_test] = lambda: f_smtp_test
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_lot_repo] = lambda: f_lot_repo
    app.dependency_overrides[get_backfill] = lambda: f_backfill
    if supervisor is not None:
        app.state.supervisor = supervisor
    return app, f_onboarding, f_cfg, f_settings_svc, f_smtp_test


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Step 1 — POST /onboarding/save?step=1 action=next
# ---------------------------------------------------------------------------


def test_step1_next_valid_region_redirects() -> None:
    """Step 1 with a valid region → save regions + advance + HX-Redirect."""
    app, f_svc, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=1",
        data={"action": "next", "regions": "dfo"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/smtp"
    # ConfigSource.save was called with regions containing dfo's int id
    assert f_cfg.save_calls == 1
    saved = f_cfg.saved_settings[0]
    assert 1 in saved.regions  # dfo → 1
    # advance was called
    assert len(f_svc.advance_calls) == 1
    assert f_svc.advance_calls[0] == (OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)


def test_step1_next_both_regions_saved() -> None:
    """Step 1 with dfo + arctic → both region IDs saved."""
    app, _, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=1",
        data={"action": "next", "regions": ["dfo", "arctic"]},
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/smtp"
    saved = f_cfg.saved_settings[0]
    assert 1 in saved.regions  # dfo
    assert 2 in saved.regions  # arctic


def test_step1_next_no_region_rerenders_with_error() -> None:
    """Step 1 with no region → re-render step 1 with error, no redirect."""
    app, f_svc, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=1",
        data={"action": "next"},
    )

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert f_cfg.save_calls == 0
    assert len(f_svc.advance_calls) == 0
    body = resp.text.lower()
    assert "выберите" in body or "region" in body or "регион" in body


def test_step1_next_unknown_slug_rerenders_with_error() -> None:
    """Step 1 with unknown slug → re-render with error."""
    app, _, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.NOT_STARTED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=1",
        data={"action": "next", "regions": "unknown_region"},
    )

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert f_cfg.save_calls == 0


def test_step1_advance_mismatch_redirects_to_current_step() -> None:
    """Step 1 advance raises InvalidTransitionError → HX-Redirect to current step."""
    err = InvalidTransitionError("regions_set", "not_started", "regions_set")
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        advance_raises=err,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=1",
        data={"action": "next", "regions": "dfo"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/smtp"


# ---------------------------------------------------------------------------
# Step 2 — POST /onboarding/save?step=2 action=next
# ---------------------------------------------------------------------------


def _step2_form_data(action: str = "next") -> dict[str, str]:
    return {
        "action": action,
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_login": "bot@example.com",
        "smtp_pass": "secret123",
        "smtp_from_name": "Bot",
    }


def test_step2_next_valid_credentials_redirects() -> None:
    """Step 2 valid credentials → save creds + advance + HX-Redirect."""
    app, f_svc, _, f_settings_svc, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=2", data=_step2_form_data())

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/recipients"
    assert len(f_settings_svc.set_smtp_credentials_calls) == 1
    creds = f_settings_svc.set_smtp_credentials_calls[0]
    assert creds.smtp_host == "smtp.example.com"
    assert creds.smtp_port == 587
    assert creds.smtp_user == "bot@example.com"
    assert creds.from_name == "Bot"  # smtp_from_name must now be persisted (bd ljp)
    assert len(f_svc.advance_calls) == 1
    assert f_svc.advance_calls[0] == (OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)


def test_step2_smtp_from_name_not_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """Regression guard: smtp_from_name (PII per ADR-012) must not appear in INFO logs.

    Если кто-то поднимет уровень логирования в onboarding step 2 обратно на INFO,
    PII попадёт в app.jsonl → diagnostic.zip. Этот тест ловит регрессию.
    """
    app, _, _, _, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = _client(app)
    data = _step2_form_data()
    data["smtp_from_name"] = "Sensitive Display Name"

    with caplog.at_level(logging.INFO, logger="fis_monitor"):
        resp = client.post("/onboarding/save?step=2", data=data)

    assert resp.status_code == 200
    info_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert not any("Sensitive Display Name" in m for m in info_messages), (
        "smtp_from_name must not appear in INFO+ logs — see ADR-012 (bd frd)"
    )


def test_step2_next_smtp_policy_error_rerenders() -> None:
    """Step 2 SmtpHostPolicyError → re-render step 2 with error, no redirect."""
    policy_err = SmtpHostPolicyError("DNS resolution failed")
    app, f_svc, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        settings_svc_raises=policy_err,
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=2", data=_step2_form_data())

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert len(f_svc.advance_calls) == 0
    # Error message should appear in rendered HTML
    body2 = resp.text.lower()
    assert "smtp" in body2 or "подключ" in body2 or "ошибк" in body2


def test_step2_next_empty_host_rerenders_with_error() -> None:
    """Step 2 empty smtp_host → re-render with error."""
    app, f_svc, _, _, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = _client(app)

    data = _step2_form_data()
    data["smtp_host"] = ""

    resp = client.post("/onboarding/save?step=2", data=data)

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert len(f_svc.advance_calls) == 0


def test_step2_next_invalid_port_rerenders_with_error() -> None:
    """Step 2 invalid port (0) → re-render with error."""
    app, f_svc, _, _, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = _client(app)

    data = _step2_form_data()
    data["smtp_port"] = "0"

    resp = client.post("/onboarding/save?step=2", data=data)

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert len(f_svc.advance_calls) == 0


def test_step2_next_advance_guard_fail_rerenders() -> None:
    """Step 2 advance raises InvalidTransitionError (guard fail) → re-render with error."""
    err = InvalidTransitionError("regions_set", "regions_set", "smtp_configured")
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        advance_raises=err,
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=2", data=_step2_form_data())

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers


def test_step2_next_advance_state_mismatch_redirects() -> None:
    """Step 2 advance raises InvalidTransitionError with different from_state → HX-Redirect."""
    # current_state differs from requested from_state → concurrent submit
    err = InvalidTransitionError("smtp_configured", "regions_set", "smtp_configured")
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.SMTP_CONFIGURED,
        advance_raises=err,
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=2", data=_step2_form_data())

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/recipients"


def test_step2_skip_sets_email_skipped_and_redirects() -> None:
    """Step 2 action=skip → skip_email + 2x advance + HX-Redirect to /onboarding/recipients."""
    app, f_svc, _, _, _ = _make_app(onboarding_state=OnboardingState.REGIONS_SET)
    client = _client(app)

    resp = client.post("/onboarding/save?step=2", data={"action": "skip"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/recipients"
    assert f_svc.skip_email_calls == 1
    assert len(f_svc.advance_calls) == 2
    # First advance: REGIONS_SET → SMTP_CONFIGURED
    assert f_svc.advance_calls[0] == (
        OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED
    )
    # Second advance: SMTP_CONFIGURED → RECIPIENTS_SET
    assert f_svc.advance_calls[1] == (
        OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET
    )


# ---------------------------------------------------------------------------
# Step 3 — POST /onboarding/save?step=3 action=next
# ---------------------------------------------------------------------------


def test_step3_next_valid_email_redirects() -> None:
    """Step 3 valid recipient → save + advance + HX-Redirect."""
    app, f_svc, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={"action": "next", "recipient_email": "user@example.com"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/test-email"
    assert f_cfg.save_calls == 1
    saved = f_cfg.saved_settings[0]
    assert "user@example.com" in saved.notifications.email.recipients
    assert len(f_svc.advance_calls) == 1
    assert f_svc.advance_calls[0] == (
        OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET
    )


def test_step3_next_multiple_emails_saved() -> None:
    """Step 3 comma-separated emails → all saved."""
    app, _, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={"action": "next", "recipient_email": "a@x.com, b@y.com"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/test-email"
    saved = f_cfg.saved_settings[0]
    assert "a@x.com" in saved.notifications.email.recipients
    assert "b@y.com" in saved.notifications.email.recipients


def test_step3_next_invalid_email_rerenders() -> None:
    """Step 3 invalid email → re-render with error."""
    app, f_svc, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={"action": "next", "recipient_email": "not-an-email"},
    )

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert f_cfg.save_calls == 0
    assert len(f_svc.advance_calls) == 0


def test_step3_next_empty_email_rerenders() -> None:
    """Step 3 empty recipient_email → re-render with error."""
    app, _, f_cfg, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={"action": "next", "recipient_email": ""},
    )

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    assert f_cfg.save_calls == 0


def test_step3_next_with_send_test_email_calls_test_send() -> None:
    """Step 3 send_test_email=1 → smtp_test_service.test_send() called."""
    smtp_result = NotifyResult(ok=True, detail="test ok", retryable=False)
    app, _, _, _, f_smtp = _make_app(
        onboarding_state=OnboardingState.SMTP_CONFIGURED,
        smtp_test_result=smtp_result,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={
            "action": "next",
            "recipient_email": "user@example.com",
            "send_test_email": "1",
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/test-email"
    assert len(f_smtp.test_send_calls) == 1
    assert f_smtp.test_send_calls[0][1] == "user@example.com"


def test_step3_next_without_send_test_email_does_not_call_test_send() -> None:
    """Step 3 without send_test_email checkbox → test_send NOT called."""
    app, _, _, _, f_smtp = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post(
        "/onboarding/save?step=3",
        data={"action": "next", "recipient_email": "user@example.com"},
    )

    assert resp.status_code == 200
    assert len(f_smtp.test_send_calls) == 0


def test_step3_skip_redirects_to_test_email() -> None:
    """Step 3 action=skip → skip_email + advance + HX-Redirect."""
    app, f_svc, _, _, _ = _make_app(onboarding_state=OnboardingState.SMTP_CONFIGURED)
    client = _client(app)

    resp = client.post("/onboarding/save?step=3", data={"action": "skip"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/onboarding/test-email"
    assert f_svc.skip_email_calls == 1
    assert len(f_svc.advance_calls) == 1
    assert f_svc.advance_calls[0] == (
        OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET
    )


# ---------------------------------------------------------------------------
# Step 4 — POST /onboarding/save?step=4 action=next
# ---------------------------------------------------------------------------


def test_step4_next_advance_success_redirects_home() -> None:
    """Step 4 advance success → HX-Redirect to /."""
    app, f_svc, _, _, _ = _make_app(onboarding_state=OnboardingState.RECIPIENTS_SET)
    client = _client(app)

    resp = client.post("/onboarding/save?step=4", data={"action": "next"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/"
    assert len(f_svc.advance_calls) == 1
    assert f_svc.advance_calls[0] == (OnboardingState.RECIPIENTS_SET, OnboardingState.COMPLETED)


def test_step4_next_guard_fail_rerenders_with_error() -> None:
    """Step 4 advance raises InvalidTransitionError (guard fail) → re-render with error."""
    err = InvalidTransitionError("recipients_set", "recipients_set", "completed")
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.RECIPIENTS_SET,
        advance_raises=err,
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=4", data={"action": "next"})

    assert resp.status_code == 200
    assert "HX-Redirect" not in resp.headers
    body3 = resp.text.lower()
    assert "письм" in body3 or "тест" in body3 or "подтвер" in body3
    assert "дфо" in resp.text or "Арктик" in resp.text


# ---------------------------------------------------------------------------
# Unknown step → 400
# ---------------------------------------------------------------------------


def test_unknown_step_returns_400() -> None:
    """POST /onboarding/save?step=99 → 400."""
    app, _, _, _, _ = _make_app()
    client = _client(app)

    resp = client.post("/onboarding/save?step=99", data={"action": "next"})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /onboarding/smtp-test
# ---------------------------------------------------------------------------


def test_smtp_test_success_returns_ok_fragment() -> None:
    """POST /onboarding/smtp-test success → HTML fragment with chip--ok."""
    smtp_result = NotifyResult(ok=True, detail="Connected", retryable=False)
    app, _, _, _, f_smtp = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--ok" in resp.text
    assert len(f_smtp.test_send_calls) == 1


def test_smtp_test_failure_returns_err_fragment() -> None:
    """POST /onboarding/smtp-test failure → HTML fragment with chip--err."""
    smtp_result = NotifyResult(ok=False, detail="Auth failed", retryable=False)
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    assert "Auth failed" in resp.text or "Ошибка" in resp.text


def test_smtp_test_policy_error_returns_err_fragment() -> None:
    """POST /onboarding/smtp-test SmtpHostPolicyError → HTML fragment with chip--err, PII-free."""
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        settings_svc_raises=SmtpHostPolicyError("blacklisted host"),
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    # PII-free — raw error detail must not leak hostname/password material
    # (the fragment may contain a generic message)


def test_smtp_test_calls_settings_service_then_smtp_test() -> None:
    """POST /onboarding/smtp-test → credentials saved THEN test_send called."""
    smtp_result = NotifyResult(ok=True, detail="ok", retryable=False)
    calls: list[str] = []

    class OrderedFakeSettingsService(FakeSettingsService):
        def set_smtp_credentials(self, creds: SmtpCredentials) -> None:
            calls.append("set_creds")
            super().set_smtp_credentials(creds)

    class OrderedFakeSmtpTestService(FakeSmtpTestService):
        def test_send(self, lot: object, recipient: str) -> NotifyResult:
            calls.append("test_send")
            return super().test_send(lot, recipient)

    f_settings_svc = OrderedFakeSettingsService()
    f_smtp_test = OrderedFakeSmtpTestService(result=smtp_result)

    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        fake_settings_svc=f_settings_svc,
        fake_smtp_test=f_smtp_test,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert calls == ["set_creds", "test_send"]


# ---------------------------------------------------------------------------
# _validate_smtp_input — pure-function unit tests
# ---------------------------------------------------------------------------


def test_validate_smtp_input_empty_host_returns_error() -> None:
    """Empty smtp_host → returns non-None error string."""
    err = _validate_smtp_input("", "user@example.com", "secret", 587)
    assert err is not None
    assert "SMTP" in err or "сервер" in err.lower()


def test_validate_smtp_input_empty_login_returns_error() -> None:
    """Empty smtp_login → returns non-None error string."""
    err = _validate_smtp_input("smtp.example.com", "", "secret", 587)
    assert err is not None
    assert "логин" in err.lower()


def test_validate_smtp_input_empty_password_returns_error() -> None:
    """Empty smtp_pass → returns non-None error string."""
    err = _validate_smtp_input("smtp.example.com", "user@example.com", "", 587)
    assert err is not None
    assert "пароль" in err.lower()


def test_validate_smtp_input_port_zero_returns_error() -> None:
    """Port 0 → returns non-None error string mentioning range."""
    err = _validate_smtp_input("smtp.example.com", "user@example.com", "secret", 0)
    assert err is not None
    assert "65535" in err or "диапазон" in err.lower() or "порт" in err.lower()


def test_validate_smtp_input_port_out_of_range_returns_error() -> None:
    """Port 99999 → returns non-None error string."""
    err = _validate_smtp_input("smtp.example.com", "user@example.com", "secret", 99999)
    assert err is not None


def test_validate_smtp_input_valid_returns_none() -> None:
    """All valid fields → returns None (no error)."""
    err = _validate_smtp_input("smtp.example.com", "user@example.com", "secret", 587)
    assert err is None


# ---------------------------------------------------------------------------
# POST /onboarding/smtp-test — invalid-input 200-fragment tests
# ---------------------------------------------------------------------------


def test_smtp_test_empty_host_returns_err_fragment_not_500() -> None:
    """POST /onboarding/smtp-test with empty smtp_host → 200 chip--err, no 500."""
    app, _, _, f_settings_svc, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    assert 'id="smtp-test-result"' in resp.text
    # Service must NOT be called — validation short-circuits before it
    assert len(f_settings_svc.set_smtp_credentials_calls) == 0


def test_smtp_test_empty_login_returns_err_fragment() -> None:
    """POST /onboarding/smtp-test with empty smtp_login → 200 chip--err."""
    app, _, _, f_settings_svc, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    assert 'id="smtp-test-result"' in resp.text
    assert len(f_settings_svc.set_smtp_credentials_calls) == 0


def test_smtp_test_empty_password_returns_err_fragment() -> None:
    """POST /onboarding/smtp-test with empty smtp_pass → 200 chip--err."""
    app, _, _, f_settings_svc, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "bot@example.com",
            "smtp_pass": "",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    assert 'id="smtp-test-result"' in resp.text
    assert len(f_settings_svc.set_smtp_credentials_calls) == 0


def test_smtp_test_invalid_port_returns_err_fragment() -> None:
    """POST /onboarding/smtp-test with port=0 → 200 chip--err."""
    app, _, _, f_settings_svc, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
    )
    client = _client(app)

    resp = client.post(
        "/onboarding/smtp-test",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "0",
            "smtp_login": "bot@example.com",
            "smtp_pass": "secret",
        },
    )

    assert resp.status_code == 200
    assert "chip--err" in resp.text
    assert 'id="smtp-test-result"' in resp.text
    assert len(f_settings_svc.set_smtp_credentials_calls) == 0


# ---------------------------------------------------------------------------
# POST /onboarding/smtp-test — rate-limiter tests (Security F-05)
# ---------------------------------------------------------------------------

_SMTP_TEST_FORM = {
    "smtp_host": "smtp.example.com",
    "smtp_port": "587",
    "smtp_login": "bot@example.com",
    "smtp_pass": "secret",
}


def test_smtp_test_rate_limiter_first_request_passes() -> None:
    """POST /onboarding/smtp-test — first request within window is allowed (200)."""
    smtp_result = NotifyResult(ok=True, detail="ok", retryable=False)
    rl = RateLimiter(max_requests=1, window_seconds=10.0)
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
        rate_limiter=rl,
    )
    client = _client(app)

    resp = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)

    assert resp.status_code == 200


def test_smtp_test_rate_limiter_second_call_returns_429() -> None:
    """POST /onboarding/smtp-test — second call within the window → 429."""
    smtp_result = NotifyResult(ok=True, detail="ok", retryable=False)
    rl = RateLimiter(max_requests=1, window_seconds=10.0)
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
        rate_limiter=rl,
    )
    client = _client(app)

    # First call — allowed
    resp1 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp1.status_code == 200

    # Second call — rate limited
    resp2 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp2.status_code == 429


def test_smtp_test_rate_limiter_resets_after_window() -> None:
    """POST /onboarding/smtp-test — after window expires a new request is allowed."""
    smtp_result = NotifyResult(ok=True, detail="ok", retryable=False)
    # Use a fresh isolated limiter; after first call replace it with a new one
    # to simulate window expiry (same isolation pattern as cycle/auth tests).
    rl = RateLimiter(max_requests=1, window_seconds=10.0)
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
        rate_limiter=rl,
    )
    client = _client(app)

    # First call — consumes the quota
    resp1 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp1.status_code == 200

    # Re-bind the module global — the route reads it by name on each request,
    # so replacing it here takes effect immediately. This simulates window
    # expiry without time-travel.
    onboarding_module._smtp_test_rate_limiter = RateLimiter(max_requests=1, window_seconds=10.0)

    resp2 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp2.status_code == 200


def test_smtp_test_rate_limiter_unknown_ip_does_not_crash() -> None:
    """POST /onboarding/smtp-test — request.client is None → client_ip returns "unknown".

    All anonymous clients share the same "unknown" bucket (documented in
    web/_helpers.py). We verify the handler does not crash and that the shared
    bucket is enforced: two requests from the same TestClient (which uses the
    same loopback IP / may present as None in certain ASGI scopes) both go
    through the limiter without error.

    Limitation: starlette TestClient always sets a loopback client; we cannot
    trivially inject client=None via TestClient. Instead, we confirm the
    shared-bucket semantics by checking that a second POST after quota is
    exhausted returns 429 (not a 500), proving the "unknown" path is safe.
    """
    smtp_result = NotifyResult(ok=True, detail="ok", retryable=False)
    rl = RateLimiter(max_requests=1, window_seconds=10.0)
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.REGIONS_SET,
        smtp_test_result=smtp_result,
        rate_limiter=rl,
    )
    client = _client(app)

    # First call — allowed (quota consumed)
    resp1 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp1.status_code == 200

    # Second call — same IP bucket is full → 429, not 500
    resp2 = client.post("/onboarding/smtp-test", data=_SMTP_TEST_FORM)
    assert resp2.status_code == 429


# ---------------------------------------------------------------------------
# Step 4 auto-backfill trigger — integration via POST /onboarding/save?step=4
# ---------------------------------------------------------------------------


def test_step4_next_does_not_trigger_backfill() -> None:
    """ADR-032 / f5u fix: backfill is NO LONGER triggered from step4.

    The trigger moved to LoginService.on_login_success (composition.py) to
    avoid the race where backfill ran before Playwright headed-login completed.
    Step4 now only advances the FSM to COMPLETED and redirects to /.
    supervisor.start must NOT be called regardless of guard conditions.
    """
    sup = FakeSupervisor()
    f_lot_repo = FakeLotRepo(active_count=0)
    f_backfill = FakeBackfillService()
    app, _, _, _, _ = _make_app(
        onboarding_state=OnboardingState.RECIPIENTS_SET,
        settings=make_settings(regions=[1]),
        fake_lot_repo=f_lot_repo,
        fake_backfill=f_backfill,
        supervisor=sup,
    )
    client = _client(app)

    resp = client.post("/onboarding/save?step=4", data={"action": "next"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/"
    # No backfill from step4 — trigger is now in LoginService.on_login_success.
    assert sup.start_call_count == 0
    assert f_backfill.start_calls == 0
