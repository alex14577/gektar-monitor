"""Unit tests for GET /settings/smtp/suggest (Layer 4 — web routing contract).

Coverage (per ADR-038 §Тесты по слоям Layer 4):
  - Known email → 200 JSON with smtp_host/smtp_port/use_starttls/app_password_url/provider_label.
  - Unknown email domain → 200 JSON with all fields null.
  - Invalid email (no @, empty) → 400.
  - Response shape contains exactly the expected fields — no extra/secret fields.

Invariants tested:
  - HTTP contract: URL, query-param name, status codes.
  - Response shape is stable (no unexpected keys).
  - Catalog is injected via DI (dependency_overrides) — no real I/O in tests.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import ProviderSuggestion
from fis_monitor.web.deps import get_smtp_provider_catalog
from fis_monitor.web.routes.settings import router

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_GMAIL_SUGGESTION = ProviderSuggestion(
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    use_starttls=True,
    app_password_url="https://support.google.com/accounts/answer/185833",
    provider_label="Gmail",
)


class _FakeCatalogKnown:
    """Returns a fixed suggestion for any email whose domain is 'gmail.com'."""

    def lookup(self, email: str) -> ProviderSuggestion | None:
        if "@" in email and email.split("@")[-1].lower() == "gmail.com":
            return _GMAIL_SUGGESTION
        return None


class _FakeCatalogUnknown:
    """Always returns None — simulates unknown/corporate domain."""

    def lookup(self, email: str) -> ProviderSuggestion | None:
        return None


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = frozenset(
    {"smtp_host", "smtp_port", "use_starttls", "app_password_url", "provider_label"}
)


def _make_app(catalog: Any) -> FastAPI:
    """Minimal FastAPI app with settings router and catalog override."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_smtp_provider_catalog] = lambda: catalog
    return app


# ---------------------------------------------------------------------------
# Tests: known email
# ---------------------------------------------------------------------------


def test_returns_suggestion_for_known_email() -> None:
    """GET ?email=user@gmail.com → 200 with populated suggestion fields."""
    app = _make_app(_FakeCatalogKnown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings/smtp/suggest", params={"email": "user@gmail.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["smtp_host"] == "smtp.gmail.com"
    assert body["smtp_port"] == 587
    assert body["use_starttls"] is True
    assert body["app_password_url"] == "https://support.google.com/accounts/answer/185833"
    assert body["provider_label"] == "Gmail"


def test_response_shape_has_exactly_expected_keys() -> None:
    """Response must not include extra or secret fields."""
    app = _make_app(_FakeCatalogKnown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings/smtp/suggest", params={"email": "user@gmail.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == _EXPECTED_KEYS


# ---------------------------------------------------------------------------
# Tests: unknown email
# ---------------------------------------------------------------------------


def test_returns_null_fields_for_unknown_email() -> None:
    """GET ?email=user@unknown.example → 200 with all null values (not 404)."""
    app = _make_app(_FakeCatalogUnknown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get(
            "/settings/smtp/suggest", params={"email": "user@unknown.example"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["smtp_host"] is None
    assert body["smtp_port"] is None
    assert body["use_starttls"] is None
    assert body["app_password_url"] is None
    assert body["provider_label"] is None


def test_unknown_email_response_shape_is_stable() -> None:
    """Null response must also have exactly the expected keys."""
    app = _make_app(_FakeCatalogUnknown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get(
            "/settings/smtp/suggest", params={"email": "user@unknown.example"}
        )
    assert set(resp.json().keys()) == _EXPECTED_KEYS


# ---------------------------------------------------------------------------
# Tests: invalid email → 400
# ---------------------------------------------------------------------------


def test_empty_email_returns_400() -> None:
    """GET ?email= (empty string) → 400 Bad Request."""
    app = _make_app(_FakeCatalogUnknown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings/smtp/suggest", params={"email": ""})
    assert resp.status_code == 400


def test_no_at_email_returns_400() -> None:
    """GET ?email=notanemail (no @) → 400 Bad Request."""
    app = _make_app(_FakeCatalogUnknown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings/smtp/suggest", params={"email": "notanemail"})
    assert resp.status_code == 400


def test_missing_email_param_returns_422() -> None:
    """GET /settings/smtp/suggest (no email param) → 422 Unprocessable Entity."""
    app = _make_app(_FakeCatalogUnknown())
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/settings/smtp/suggest")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: catalog is not called for invalid inputs (endpoint returns early)
# ---------------------------------------------------------------------------


def test_invalid_email_does_not_reach_catalog() -> None:
    """Endpoint validates email format before consulting catalog."""

    class _RecordingCatalog:
        called = False

        def lookup(self, email: str) -> ProviderSuggestion | None:
            _RecordingCatalog.called = True
            return None

    app = _make_app(_RecordingCatalog())
    with TestClient(app, raise_server_exceptions=True) as client:
        client.get("/settings/smtp/suggest", params={"email": "garbage"})
    assert not _RecordingCatalog.called
