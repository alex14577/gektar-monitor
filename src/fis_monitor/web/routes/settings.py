"""FastAPI APIRouter for settings endpoints.

Endpoints:
  GET  /settings              — return current Settings snapshot.
  POST /settings/smtp         — update SMTP credentials (DNS-outside-tx via SettingsService).
  POST /settings/smtp/test    — send a test email to a given recipient.
  POST /settings/regions      — replace ``Settings.regions`` list; triggers hot-reload.
  POST /settings/recipients   — replace ``Settings.notifications.email.recipients``.

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import LotPublicDTO, SmtpCredentials
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService
from fis_monitor.web.deps import (
    get_config_source,
    get_settings_service,
    get_smtp_test,
    get_templates,
)

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


class RegionsBody(BaseModel):
    """JSON body for POST /settings/regions.

    ``regions`` must be a non-empty list of valid Russian cadastral region codes
    (1-80).  Constraints mirror ``Settings.regions`` field semantics.
    """

    regions: Annotated[
        list[Annotated[int, Field(ge=1, le=80)]],
        Field(min_length=1),
    ]


class RecipientsBody(BaseModel):
    """JSON body for POST /settings/recipients.

    ``recipients`` is a list of RFC-5321 email addresses.  An empty list is
    accepted (disables email notifications).
    """

    recipients: list[EmailStr] = Field(default_factory=list)


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


def _prefers_html(accept: str) -> bool:
    """Return True if the Accept header prefers text/html over application/json.

    MVP implementation: checks for 'text/html' substring presence without a
    full quality-value parser.  Sufficient for browser vs API-client distinction.
    """
    if not accept:
        return False
    # Browsers always include 'text/html'; pure API clients typically send
    # 'application/json' or omit the header entirely.
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


@router.get("", response_model=None)
def get_settings(
    request: Request,
    config_source: Any = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Return the current Settings snapshot.

    Content-negotiation:
    - ``Accept: text/html`` → HTML settings page (settings.html.jinja).
    - Otherwise → JSON (original behaviour, default for API clients).

    JSON serialises via ``model_dump(mode="json")`` — SecretStr fields are
    excluded automatically by Pydantic's serialiser.
    """
    settings = config_source.current()
    accept = request.headers.get("accept", "")
    if _prefers_html(accept):
        ctx: dict[str, Any] = {
            "settings": settings,
            # Stubs required by base.html.jinja header/partial rendering.
            "dnd": SimpleNamespace(active=False, until_hhmm=""),
            "session": SimpleNamespace(expired=False),
            "monitor": SimpleNamespace(
                state="active",
                expires_at_hhmm="",
                interval_minutes=settings.interval_minutes,
                next_cycle_mmss="—",
                last_new_human="—",
            ),
        }
        return templates.TemplateResponse(request, "settings.html.jinja", ctx)
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


@router.post("/regions")
def post_regions(
    body: RegionsBody,
    config_source: Any = Depends(get_config_source),
) -> JSONResponse:
    """Replace the monitored regions list.

    Pattern: ``current() → model_copy(update=...) → save()``.
    The atomic file-replace triggers a watchdog reload on the same subscriber
    bus as manual config edits (ADR-023).

    Returns:
        200 with ``{"ok": true}`` on success.
        422 on validation failure (empty list, region out of 1-80 range).
    """
    current = config_source.current()
    new_settings = current.model_copy(update={"regions": list(body.regions)})
    config_source.save(new_settings)
    return JSONResponse(content={"ok": True})


@router.post("/recipients")
def post_recipients(
    body: RecipientsBody,
    config_source: Any = Depends(get_config_source),
) -> JSONResponse:
    """Replace the email recipients list.

    Pattern: ``current() → model_copy(update=...) → save()``.
    Mutates ``Settings.notifications.email.recipients`` via nested ``model_copy``.

    Returns:
        200 with ``{"ok": true}`` on success.
        422 on validation failure (invalid email address).
    """
    current = config_source.current()
    new_email = current.notifications.email.model_copy(
        update={"recipients": [str(r) for r in body.recipients]}
    )
    new_notifications = current.notifications.model_copy(update={"email": new_email})
    new_settings = current.model_copy(update={"notifications": new_notifications})
    config_source.save(new_settings)
    return JSONResponse(content={"ok": True})
