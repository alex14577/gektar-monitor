"""FastAPI APIRouter for settings endpoints.

Endpoints:
  GET  /settings           — return current Settings snapshot.
  POST /settings/smtp      — update SMTP credentials (DNS-outside-tx via SettingsService).
  POST /settings/smtp/test — send a test email to a given recipient.

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.

Out of scope (no service exists yet):
  - POST /settings/regions   — requires ConfigSource.save() Protocol (not yet defined).
  - POST /settings/recipients — same: no mutation seam on ConfigSource.
  These would be a follow-up bd issue once ConfigSource gains a save() method.
  Do NOT invent new Protocol methods here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import LotPublicDTO, SmtpCredentials
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService
from fis_monitor.web.deps import get_config_source, get_settings_service, get_smtp_test

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SmtpCredentialsBody(BaseModel):
    """JSON body for POST /settings/smtp."""

    smtp_user: str
    smtp_password: str
    smtp_host: str
    smtp_port: int = 587
    use_default: bool = True


class SmtpTestBody(BaseModel):
    """JSON body for POST /settings/smtp/test."""

    recipient: str


# ---------------------------------------------------------------------------
# Test fixture factory
# ---------------------------------------------------------------------------


def _test_lot_fixture() -> LotPublicDTO:
    """Return a deterministic minimal LotPublicDTO for SMTP send tests.

    Fields are hard-coded and PII-free — this is a synthetic lot used only
    to exercise the full SMTP path (SmtpEmailNotifier.send).
    """
    _now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    return LotPublicDTO(
        id=0,
        cadastral_no="00:00:0000000:0000",
        area_sqm=1000,
        region="Test region",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="Test",
        date_create=_now,
        date_update=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=_now,
        last_seen=_now,
        detail_fetched_at=None,
        enrichment_status=None,
        last_seen_at=None,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
        age_seconds=0,
        tier="match",
        freshness="hot",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
def get_settings(
    config_source: Any = Depends(get_config_source),
) -> JSONResponse:
    """Return the current Settings snapshot.

    Serialises via ``model_dump(mode="json")`` — SecretStr fields are
    excluded automatically by Pydantic's serialiser.
    """
    settings = config_source.current()
    return JSONResponse(content=settings.model_dump(mode="json"))


@router.post("/smtp", status_code=204)
def post_smtp(
    body: SmtpCredentialsBody,
    svc: SettingsService = Depends(get_settings_service),
) -> None:
    """Validate and persist SMTP credentials.

    Phase 1 — format check: ``SmtpCredentials`` construction validates port range.
    Phase 2 — DNS/policy check: ``SettingsService`` calls host_policy.resolve_and_check
              BEFORE any DB write (ADR-015 invariant).
    Phase 3 — persist via ``SmtpCredentialsRepository.save()``.

    Returns:
        204 No Content on success.
        422 on empty host or port out of range (ValueError).
        400 on DNS/policy failure (SmtpHostPolicyError).
    """
    try:
        creds = SmtpCredentials(
            smtp_user=body.smtp_user,
            smtp_password=body.smtp_password,
            smtp_host=body.smtp_host,
            smtp_port=body.smtp_port,
            use_default=body.use_default,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        svc.set_smtp_credentials(creds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SmtpHostPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/smtp/test")
def post_smtp_test(
    body: SmtpTestBody,
    svc: SmtpTestService = Depends(get_smtp_test),
) -> JSONResponse:
    """Send a test email and return the outcome.

    Constructs a deterministic synthetic ``LotPublicDTO`` fixture and passes it
    to ``SmtpTestService.test_send()``.  The full SMTP path (STARTTLS, DNS, auth)
    is exercised.

    Returns:
        200 with ``{"ok": bool, "detail": "..."}`` regardless of success/failure.
    """
    test_lot = _test_lot_fixture()
    result = svc.test_send(test_lot, body.recipient)
    return JSONResponse(content={"ok": result.ok, "detail": result.detail})
