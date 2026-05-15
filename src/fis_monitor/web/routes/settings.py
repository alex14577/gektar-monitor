"""FastAPI APIRouter for settings endpoints.

Endpoints:
  GET  /settings              — return current Settings snapshot.
  POST /settings/smtp         — update SMTP credentials (DNS-outside-tx via SettingsService).
  POST /settings/smtp/test    — send a test email to a given recipient.
  POST /settings/regions      — replace ``Settings.regions`` list; triggers hot-reload.
  POST /settings/recipients   — replace ``Settings.notifications.email.recipients``.
  POST /settings/subjects     — replace ``Settings.subject_site_ids`` (fetch-scope, ADR-031).
  POST /settings/schedule     — replace monitoring schedule fields (ADR-033).

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import LotPublicDTO, SmtpCredentials
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID, subjects_for_macros
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


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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
        scoped_ids = subjects_for_macros(settings.regions)
        available_subjects = [(sid, SUBJECT_TITLE_BY_ID[sid]) for sid in scoped_ids]
        ctx: dict[str, Any] = {
            "settings": settings,
            # Subjects scoped to current macro-regions (ADR-031).
            "available_subjects": available_subjects,
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


@router.post("/subjects", status_code=204)
def post_subjects(
    subject_site_ids: Annotated[list[int], Form()] = [],  # noqa: B006 — FastAPI never mutates repeated-key default
    config_source: Any = Depends(get_config_source),
) -> Response:
    """Replace the fetch-scope subject filter (ADR-031).

    Accepts form-encoded repeated ``subject_site_ids`` keys (htmx-friendly).
    Each id must belong to ``subjects_for_macros(settings.regions)``; any
    out-of-scope id → 422.  An empty list is valid (fetch all subjects).

    Returns:
        204 No Content on success.
        422 if any id is outside the macro-scoped subject set.
    """
    current = config_source.current()
    valid_ids = set(subjects_for_macros(current.regions))
    out_of_scope = [sid for sid in subject_site_ids if sid not in valid_ids]
    if out_of_scope:
        raise HTTPException(
            status_code=422,
            detail=f"subject_site_ids out of scope for current regions: {out_of_scope}",
        )
    new_settings = current.model_copy(update={"subject_site_ids": list(subject_site_ids)})
    config_source.save(new_settings)
    return Response(status_code=204)


@router.post("/schedule", response_model=None)
def post_schedule(
    request: Request,
    interval_minutes: Annotated[str, Form()],
    full_scan_time: Annotated[str, Form()],
    config_source: Any = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Replace monitoring schedule settings (ADR-033).

    Accepts application/x-www-form-urlencoded from the htmx form in
    settings.html.jinja (no hx-ext="json-enc" required).  Both fields are
    updated atomically to prevent compute-and-replace races.
    Hot-reload is automatic: MonitorCycleService and FullScanService read
    config_source.current() on every iteration.

    Returns:
        200 with _schedule_section.html.jinja partial; htmx swaps
        ``#schedule-section`` outerHTML so the user sees the saved values.
        422 on validation failure (out-of-range or bad HH:MM format).
    """
    # Validate interval_minutes
    try:
        interval_int = int(interval_minutes)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"interval_minutes must be an integer, got {interval_minutes!r}",
        ) from None
    if not (0 <= interval_int <= 60):
        raise HTTPException(
            status_code=422,
            detail=f"interval_minutes must be 0–60, got {interval_int}",
        )

    # Validate full_scan_time
    if not _HHMM_RE.match(full_scan_time):
        raise HTTPException(
            status_code=422,
            detail=f"full_scan_time must match HH:MM (00:00–23:59), got {full_scan_time!r}",
        )

    current = config_source.current()
    new_monitoring = current.monitoring.model_copy(
        update={"full_scan_time": full_scan_time}
    )
    new_settings = current.model_copy(
        update={
            "interval_minutes": interval_int,
            "monitoring": new_monitoring,
        }
    )
    config_source.save(new_settings)
    return templates.TemplateResponse(
        request,
        "partials/_schedule_section.html.jinja",
        {"settings": new_settings},
    )
