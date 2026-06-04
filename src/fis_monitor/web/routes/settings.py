"""FastAPI APIRouter for settings endpoints.

Endpoints:
  GET  /settings              — return current Settings snapshot.
  GET  /settings/smtp/suggest — auto-suggest SMTP host/port by email domain (ADR-038).
  POST /settings/smtp         — update SMTP credentials (DNS-outside-tx via SettingsService).
  POST /settings/smtp/test    — send a test email to a given recipient.
  POST /settings/regions      — replace macro-region scope; truncates subjects + htmx partial.
  POST /settings/recipients   — replace ``Settings.notifications.email.recipients``.
  POST /settings/subjects     — replace ``Settings.filters.rf_subjects`` (notify scope, ADR-035);
                                htmx partial; empty list = notify-all.
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
from pydantic import BaseModel, EmailStr, Field, SecretStr

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.interfaces import Clock, LotRepository
from fis_monitor.domain.models import LotPublicDTO, SmtpCredentials
from fis_monitor.domain.regions import (
    REGION_BY_SLUG,
    REGION_TITLE_NOMINATIVE_BY_SLUG,
    SUBJECT_TITLE_BY_ID,
)
from fis_monitor.services.backfill import BackfillService
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService
from fis_monitor.web.deps import (
    get_backfill,
    get_clock,
    get_config_source,
    get_lot_repo,
    get_settings_service,
    get_smtp_provider_catalog,
    get_smtp_test,
    get_templates,
)
from fis_monitor.web.monitor_vm import build_monitor_vm

__all__ = ["router"]

_VALID_MACRO_IDS: frozenset[int] = frozenset(REGION_BY_SLUG.values())

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
    from_name: str | None = None


class SmtpTestBody(BaseModel):
    """JSON body for POST /settings/smtp/test."""

    recipient: str


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


def _scope_template_context(settings: Any) -> dict[str, Any]:
    """Build template variables for the scope+subjects partial.

    Encapsulates the derivation logic so GET /settings, POST /settings/regions
    and POST /settings/subjects all render the same shape.

    available_subjects is the FULL notify-scope catalog (ADR-035: independent
    of macro-regions); all 19 subjects are always shown.
    """
    available_subjects = sorted(SUBJECT_TITLE_BY_ID.items(), key=lambda t: t[1])
    all_macro_regions = [
        (REGION_BY_SLUG[slug], title)
        for slug, title in REGION_TITLE_NOMINATIVE_BY_SLUG.items()
    ]
    return {
        "available_subjects": available_subjects,
        "all_macro_regions": all_macro_regions,
    }


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


# ---------------------------------------------------------------------------
# SMTP provider catalog — email domain → pre-filled suggestion (ADR-038)
# ---------------------------------------------------------------------------

#: Valid email must contain exactly one ``@`` with a non-empty domain part.
_AT_RE = re.compile(r"^[^@]+@[^@]+$")


@router.get("/smtp/suggest")
def get_smtp_suggest(
    email: str,
    catalog: Any = Depends(get_smtp_provider_catalog),
) -> JSONResponse:
    """Return a pre-filled SMTP suggestion based on the email's domain.

    Performs a pure in-memory catalog lookup — no DNS, no network I/O.
    The suggestion is a UX helper only; ``POST /settings/smtp`` always
    re-validates via ``DefaultSmtpHostPolicy`` regardless (ADR-038 §4).

    Args:
        email: Full email address (e.g. ``user@gmail.com``).

    Returns:
        200 with ``{smtp_host, smtp_port, use_starttls, app_password_url,
        provider_label}`` populated if domain is known, all ``null`` if
        unknown.
        400 if *email* is empty or contains no ``@`` (malformed input —
        the UI should not send suggest requests for clearly invalid emails).
    """
    if not email or not _AT_RE.match(email):
        raise HTTPException(
            status_code=400,
            detail="email must be a non-empty string containing '@'",
        )
    suggestion = catalog.lookup(email)
    if suggestion is None:
        return JSONResponse(
            content={
                "smtp_host": None,
                "smtp_port": None,
                "use_starttls": None,
                "app_password_url": None,
                "provider_label": None,
            }
        )
    return JSONResponse(
        content={
            "smtp_host": suggestion.smtp_host,
            "smtp_port": suggestion.smtp_port,
            "use_starttls": suggestion.use_starttls,
            "app_password_url": suggestion.app_password_url,
            "provider_label": suggestion.provider_label,
        }
    )


@router.get("", response_model=None)
def get_settings(
    request: Request,
    config_source: Any = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
    lot_repo: LotRepository = Depends(get_lot_repo),
    clock: Clock = Depends(get_clock),
    backfill_svc: BackfillService = Depends(get_backfill),
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
            **_scope_template_context(settings),
            # Stubs required by base.html.jinja header/partial rendering.
            "dnd": SimpleNamespace(active=False, until_hhmm=""),
            "session": (_session_ctx := SimpleNamespace(
                expired=False, expires_soon=False, expires_at_hhmm="",
            )),
            "monitor": build_monitor_vm(
                settings=settings,
                session=_session_ctx,
                lot_repo=lot_repo,
                now=clock.now(),
                awaiting_backfill=not backfill_svc.is_done(),
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
            smtp_password=SecretStr(body.smtp_password),
            smtp_host=body.smtp_host,
            smtp_port=body.smtp_port,
            use_default=body.use_default,
            from_name=body.from_name,
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


@router.post("/regions", response_model=None)
def post_regions(
    request: Request,
    region_ids: Annotated[list[int], Form()] = [],  # noqa: B006 — FastAPI never mutates repeated-key default
    config_source: Any = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Replace the macro-regions (округа) scope.

    Accepts form-encoded repeated ``region_ids`` keys from the htmx chip form.
    Validation:
      - at least one region must be selected;
      - each id must be a known macro-region (currently {1=ДФО, 2=Арктика}).

    Returns:
        200 with _scope_and_subjects.html.jinja partial; htmx swaps the whole
        ``#scope-and-subjects`` container so the subject chips reflect the
        new macro selection immediately.
        On validation or persistence error: 200 with the same partial and
        ``scope_error`` set so the inline error message is rendered.
    """
    current = config_source.current()
    scope_ctx = _scope_template_context(current)

    unknown = [rid for rid in region_ids if rid not in _VALID_MACRO_IDS]
    if unknown:
        return templates.TemplateResponse(
            request,
            "partials/_scope_and_subjects.html.jinja",
            {
                "settings": current,
                **scope_ctx,
                "scope_error": (
                    f"Неизвестные id округов: {unknown}."
                    f" Допустимые: {sorted(_VALID_MACRO_IDS)}"
                ),
            },
        )
    if not region_ids:
        return templates.TemplateResponse(
            request,
            "partials/_scope_and_subjects.html.jinja",
            {
                "settings": current,
                **scope_ctx,
                "scope_error": "Необходимо выбрать хотя бы один округ.",
            },
        )

    # Deduplicate while preserving submission order.
    unique_regions = list(dict.fromkeys(region_ids))
    new_settings = current.model_copy(update={"regions": unique_regions})
    try:
        config_source.save(new_settings)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/_scope_and_subjects.html.jinja",
            {
                "settings": current,
                **scope_ctx,
                "scope_error": f"Ошибка сохранения: {exc}",
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/_scope_and_subjects.html.jinja",
        {
            "settings": new_settings,
            **_scope_template_context(new_settings),
            "scope_saved": "districts",
        },
    )


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


@router.post("/subjects", response_model=None)
def post_subjects(
    request: Request,
    rf_subjects: Annotated[list[int], Form()] = [],  # noqa: B006 — FastAPI never mutates repeated-key default
    config_source: Any = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    """Replace the notify-scope subject filter (writes to filters.rf_subjects per ADR-035).

    Accepts form-encoded repeated ``rf_subjects`` keys from the htmx chip form.
    Validation:
      - each id must be in the FULL SUBJECT_TITLE_BY_ID catalog (19 subjects);
        notify scope is independent of macro-regions (ADR-035 I4).
      - empty list is allowed (empty = notify-all per ADR-035 I4).

    Returns:
        200 with _scope_and_subjects.html.jinja partial; htmx swaps the
        ``#scope-and-subjects`` container.
        On validation or persistence error: 200 with the same partial and
        ``scope_error`` set so the inline error message is rendered.
    """
    current = config_source.current()
    scope_ctx = _scope_template_context(current)

    valid_ids = frozenset(SUBJECT_TITLE_BY_ID)
    unknown = [sid for sid in rf_subjects if sid not in valid_ids]
    if unknown:
        return templates.TemplateResponse(
            request,
            "partials/_scope_and_subjects.html.jinja",
            {
                "settings": current,
                **scope_ctx,
                "scope_error": f"Неизвестные id субъектов (не в каталоге): {unknown}",
            },
        )

    unique_subjects = list(dict.fromkeys(rf_subjects))
    new_filters = current.filters.model_copy(update={"rf_subjects": unique_subjects})
    new_settings = current.model_copy(update={"filters": new_filters})
    try:
        config_source.save(new_settings)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/_scope_and_subjects.html.jinja",
            {
                "settings": current,
                **scope_ctx,
                "scope_error": f"Ошибка сохранения: {exc}",
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/_scope_and_subjects.html.jinja",
        {
            "settings": new_settings,
            **_scope_template_context(new_settings),
            "scope_saved": "subjects",
        },
    )


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
