"""Unit tests for /settings routes.

Tests use TestClient + app.dependency_overrides with fake services.
Anti-mock pattern: all fake classes implement ALL public methods and have
a dedicated all-methods test (orchestrator-playbook §6).

Coverage:
  1. GET  /settings happy path → 200 with serialised Settings.
  2. POST /settings/smtp happy path → 204.
  3. POST /settings/smtp empty host → 422.
  4. POST /settings/smtp SmtpHostPolicyError → 400.
  5. POST /settings/smtp/test → 200 with ok=True.
  6. DNS-outside-tx ordering: host_policy.resolve_and_check called before repo.save.
  7. All fake methods exercised (anti-mock §6).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import NotifyResult, Settings, SmtpCredentials
from fis_monitor.services.settings import SettingsService
from fis_monitor.web.deps import get_config_source, get_settings_service, get_smtp_test
from fis_monitor.web.routes.settings import router

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSource:
    """Fake ConfigSource — implements ALL public methods."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.current_calls: int = 0
        self.subscribe_calls: int = 0

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: Any) -> object:
        self.subscribe_calls += 1
        return object()  # stub subscription handle


class FakeSettingsService:
    """Fake SettingsService — implements ALL public methods.

    Configurable to raise ValueError (bad input) or SmtpHostPolicyError (DNS fail).
    """

    def __init__(
        self,
        *,
        raise_value_error: str | None = None,
        raise_host_policy_error: str | None = None,
    ) -> None:
        self._raise_value_error = raise_value_error
        self._raise_host_policy_error = raise_host_policy_error
        self.set_smtp_credentials_calls: list[SmtpCredentials] = []

    def set_smtp_credentials(self, creds: SmtpCredentials) -> None:
        if self._raise_value_error:
            raise ValueError(self._raise_value_error)
        if self._raise_host_policy_error:
            raise SmtpHostPolicyError(self._raise_host_policy_error)
        self.set_smtp_credentials_calls.append(creds)


class FakeSmtpTestService:
    """Fake SmtpTestService — implements ALL public methods."""

    def __init__(self, *, ok: bool = True, detail: str = "ok") -> None:
        self._result = NotifyResult(ok=ok, detail=detail, retryable=False)
        self.test_send_calls: list[tuple[Any, str]] = []

    def test_send(self, test_lot: Any, recipient: str) -> NotifyResult:
        self.test_send_calls.append((test_lot, recipient))
        return self._result


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    *,
    fake_config: FakeConfigSource | None = None,
    fake_settings: FakeSettingsService | None = None,
    fake_smtp_test: FakeSmtpTestService | None = None,
) -> tuple[FastAPI, FakeConfigSource, FakeSettingsService, FakeSmtpTestService]:
    """Build a minimal FastAPI app with settings router and injected fakes."""
    fc = fake_config or FakeConfigSource()
    fs = fake_settings or FakeSettingsService()
    ft = fake_smtp_test or FakeSmtpTestService()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fc
    app.dependency_overrides[get_settings_service] = lambda: fs
    app.dependency_overrides[get_smtp_test] = lambda: ft
    return app, fc, fs, ft


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL methods on all fakes
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Invoke ALL methods on each fake to catch runtime API mismatches (§6)."""
    # FakeConfigSource
    fcs = FakeConfigSource()
    s = fcs.current()
    assert isinstance(s, Settings)
    fcs.subscribe(lambda x: None)
    assert fcs.current_calls == 1
    assert fcs.subscribe_calls == 1

    # FakeSettingsService
    fss = FakeSettingsService()
    creds = SmtpCredentials(
        smtp_user="u",
        smtp_password="p",
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_default=True,
    )
    fss.set_smtp_credentials(creds)
    assert len(fss.set_smtp_credentials_calls) == 1

    # FakeSmtpTestService
    fst = FakeSmtpTestService()
    result = fst.test_send(object(), "a@b.com")
    assert isinstance(result, NotifyResult)
    assert len(fst.test_send_calls) == 1


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


def test_get_settings_returns_200() -> None:
    """GET /settings returns 200 with serialised Settings from ConfigSource."""
    settings = Settings()
    app, fc, _, _ = _make_app(fake_config=FakeConfigSource(settings=settings))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    # Top-level keys from Settings model
    assert "mode" in body
    assert "interval_minutes" in body
    assert "regions" in body
    assert fc.current_calls == 1


def test_get_settings_no_secret_leak() -> None:
    """The /settings response payload must contain no password-like field.

    Settings (config.json model) intentionally has no SecretStr fields —
    SMTP credentials live in a separate repository (ADR-020). This test
    pins that invariant: if anyone ever adds a SecretStr / password field
    to Settings, the response body would surface it (Pydantic serialises
    SecretStr as ``"**********"`` in ``mode="json"`` — still a signal that
    a secret-shaped field was leaked into the GET payload).
    """
    app, _, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    raw_text = resp.text.lower()
    assert "password" not in raw_text, (
        f"GET /settings leaked a password-shaped field: {resp.text!r}"
    )
    assert "secret" not in raw_text, (
        f"GET /settings leaked a secret-shaped field: {resp.text!r}"
    )
    assert "**********" not in raw_text, (
        f"GET /settings leaked a SecretStr placeholder (secret was serialised): {resp.text!r}"
    )


# ---------------------------------------------------------------------------
# POST /settings/smtp
# ---------------------------------------------------------------------------


def test_post_smtp_happy_path_204() -> None:
    """POST /settings/smtp returns 204 on valid credentials."""
    app, _, fs, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/settings/smtp",
            json={
                "smtp_user": "user@example.com",
                "smtp_password": "secret",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "use_default": True,
            },
        )
    assert resp.status_code == 204
    assert len(fs.set_smtp_credentials_calls) == 1
    saved = fs.set_smtp_credentials_calls[0]
    assert saved.smtp_host == "smtp.example.com"
    assert saved.smtp_port == 587


def test_post_smtp_empty_host_422() -> None:
    """POST /settings/smtp with empty host → 422 (ValueError from service)."""
    fake_svc = FakeSettingsService(raise_value_error="smtp_host must not be empty")
    app, _, _, _ = _make_app(fake_settings=fake_svc)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/settings/smtp",
            json={
                "smtp_user": "u",
                "smtp_password": "p",
                "smtp_host": "   ",
                "smtp_port": 587,
            },
        )
    assert resp.status_code == 422
    assert "smtp_host must not be empty" in resp.json()["detail"]


def test_post_smtp_host_policy_error_400() -> None:
    """POST /settings/smtp when DNS check fails → 400 (SmtpHostPolicyError)."""
    fake_svc = FakeSettingsService(raise_host_policy_error="private IP blocked")
    app, _, _, _ = _make_app(fake_settings=fake_svc)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/settings/smtp",
            json={
                "smtp_user": "u",
                "smtp_password": "p",
                "smtp_host": "192.168.0.1",
                "smtp_port": 587,
            },
        )
    assert resp.status_code == 400
    assert "private IP blocked" in resp.json()["detail"]


def test_post_smtp_invalid_port_422() -> None:
    """POST /settings/smtp with port=0 → SmtpCredentials Pydantic validation → 422."""
    app, _, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/settings/smtp",
            json={
                "smtp_user": "u",
                "smtp_password": "p",
                "smtp_host": "smtp.example.com",
                "smtp_port": 0,
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /settings/smtp/test
# ---------------------------------------------------------------------------


def test_post_smtp_test_returns_ok_true() -> None:
    """POST /settings/smtp/test → 200 with ok=true when notifier succeeds."""
    fake_smtp = FakeSmtpTestService(ok=True, detail="sent")
    app, _, _, ft = _make_app(fake_smtp_test=fake_smtp)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/settings/smtp/test", json={"recipient": "test@example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["detail"] == "sent"
    assert len(ft.test_send_calls) == 1
    _, recipient = ft.test_send_calls[0]
    assert recipient == "test@example.com"


def test_post_smtp_test_returns_ok_false_on_failure() -> None:
    """POST /settings/smtp/test → 200 with ok=false on SMTP error."""
    fake_smtp = FakeSmtpTestService(ok=False, detail="connection refused")
    app, _, _, _ = _make_app(fake_smtp_test=fake_smtp)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/settings/smtp/test", json={"recipient": "test@example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "connection refused" in body["detail"]


# ---------------------------------------------------------------------------
# Acceptance criterion #3 — DNS-outside-tx ordering
# ---------------------------------------------------------------------------


def test_smtp_save_ordering_resolve_before_save() -> None:
    """AC#3: host_policy.resolve_and_check is called BEFORE smtp_creds_repo.save.

    We test this at the SettingsService level by constructing a real service
    with a fake repo and fake policy that track ordering via a shared log.
    The route delegates ordering to SettingsService — this test pins that the
    service enforces the invariant (resolve → save, never save → resolve).
    """
    import socket

    from fis_monitor.domain.models import ResolvedSmtpEndpoint

    call_log: list[str] = []

    class OrderingFakePolicy:
        """Records 'resolve' in call_log then succeeds."""

        def resolve_and_check(
            self, host: str, port: int
        ) -> ResolvedSmtpEndpoint:
            call_log.append("resolve")
            return ResolvedSmtpEndpoint(
                ip="1.2.3.4",
                family=socket.AF_INET,
                port=port,
                original_host=host,
            )

    class OrderingFakeRepo:
        """Asserts 'resolve' was logged before recording 'save'."""

        def load(self) -> SmtpCredentials | None:
            return None

        def save(self, creds: SmtpCredentials) -> None:
            assert "resolve" in call_log, (
                "save() called BEFORE resolve_and_check() — DNS-outside-tx invariant violated!"
            )
            call_log.append("save")

    svc = SettingsService(
        smtp_creds_repo=OrderingFakeRepo(),  # type: ignore[arg-type]
        host_policy=OrderingFakePolicy(),    # type: ignore[arg-type]
    )
    creds = SmtpCredentials(
        smtp_user="u",
        smtp_password="p",
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_default=True,
    )
    svc.set_smtp_credentials(creds)
    assert call_log == ["resolve", "save"], (
        f"Expected ['resolve', 'save'], got {call_log}"
    )
