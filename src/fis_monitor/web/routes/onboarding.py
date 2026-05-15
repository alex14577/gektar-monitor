"""FastAPI APIRouter for onboarding FSM endpoints.

Endpoints (JSON API):
  GET  /onboarding/state   — return current FSM state and UI URL.
  POST /onboarding/advance — attempt a state transition.
  POST /onboarding/skip-email — set email_skipped flag.

Endpoints (Wizard UI — HTML):
  GET  /onboarding          — bare entry; 302 to url_for_current_step().
  GET  /onboarding/regions  — step 1 (requires NOT_STARTED state).
  GET  /onboarding/smtp     — step 2 (requires REGIONS_SET state).
  GET  /onboarding/recipients — step 3 (requires SMTP_CONFIGURED state).
  GET  /onboarding/test-email — step 4 (requires RECIPIENTS_SET state).

Endpoints (Wizard POST — HTML/htmx):
  POST /onboarding/save?step=N  — wizard form submission dispatcher.
  POST /onboarding/smtp-test    — htmx fragment: validate credentials + test SMTP.

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.

See docs/onboarding.md for the FSM spec and 409 body shape.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import LotRepository
    from fis_monitor.services.backfill import BackfillService

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, TypeAdapter, ValidationError

from fis_monitor.domain.errors import InvalidTransitionError, SmtpHostPolicyError
from fis_monitor.domain.interfaces import ConfigSource
from fis_monitor.domain.models import LotPublicDTO, OnboardingState, Settings, SmtpCredentials
from fis_monitor.domain.regions import REGION_TITLE_BY_SLUG, ids_to_slugs, slug_to_id
from fis_monitor.services.onboarding import OnboardingService
from fis_monitor.services.settings import SettingsService
from fis_monitor.services.smtp_test import SmtpTestService
from fis_monitor.web._helpers import client_ip
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

__all__ = ["router"]

_log = logging.getLogger(__name__)

# Module-level TypeAdapter for EmailStr validation (pydantic v2 public API,
# replaces deprecated EmailStr._validate — see 0vn reviewer M2).
_EMAIL_VALIDATOR: TypeAdapter[EmailStr] = TypeAdapter(EmailStr)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per 10 seconds per client IP (Security F-05).
# Module-level singleton: shared across all requests for the lifetime of the
# process.  Can be replaced in tests via app.dependency_overrides or by
# reassigning ``_smtp_test_rate_limiter`` before TestClient construction.
# ---------------------------------------------------------------------------

_smtp_test_rate_limiter = RateLimiter(max_requests=1, window_seconds=10.0)

# ---------------------------------------------------------------------------
# Wizard step mapping — extension point for future steps (OCP).
# key: URL slug → (required FSM state to render, step number for template)
# ---------------------------------------------------------------------------

_STEP_FOR_URL: dict[str, tuple[OnboardingState, int]] = {
    "regions": (OnboardingState.NOT_STARTED, 1),
    "smtp": (OnboardingState.REGIONS_SET, 2),
    "recipients": (OnboardingState.SMTP_CONFIGURED, 3),
    "test-email": (OnboardingState.RECIPIENTS_SET, 4),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_smtp_input(
    smtp_host: str,
    smtp_login: str,
    smtp_pass: str,
    smtp_port: int,
) -> str | None:
    """Validate SMTP form fields; return a human-readable error string or None.

    Pure function — no side effects, easily unit-testable.
    Covers the same invariants enforced by SettingsService.set_smtp_credentials()
    so that the web layer can return a structured 200 fragment instead of letting
    a ValueError propagate into a 500.
    """
    if not smtp_host:
        return "Укажите SMTP-сервер."
    if not smtp_login:
        return "Укажите логин."
    if not smtp_pass:
        return "Укажите пароль."
    if not (1 <= smtp_port <= 65535):
        return f"Порт вне диапазона 1-65535: {smtp_port!r}."
    return None


def _mismatch_redirect(svc: OnboardingService) -> RedirectResponse:
    """Return a 302 redirect to the current wizard step with no-store cache."""
    return RedirectResponse(
        url=svc.url_for_current_step(),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _wizard_response(
    request: Request,
    templates: Jinja2Templates,
    step: int,
    data: dict[str, object],
    settings: Settings,
) -> HTMLResponse:
    """Render the wizard template with the given step and data context.

    ``settings`` is always required: base.html.jinja references
    ``settings.font_size_px`` unconditionally. Uses the Starlette 1.0
    TemplateResponse(request, name, context) signature.
    """
    ctx: dict[str, object] = {"step": step, "data": data, "settings": settings}
    return templates.TemplateResponse(request, "onboarding/wizard.html.jinja", ctx)


# ---------------------------------------------------------------------------
# Wizard UI routes (GET HTML)
# ---------------------------------------------------------------------------


@router.get("", include_in_schema=False, response_model=None)
def get_onboarding_entry(
    svc: OnboardingService = Depends(get_onboarding),
) -> RedirectResponse:
    """Bare entry GET /onboarding → 302 to url_for_current_step().

    Allows bookmarking /onboarding without knowing the current FSM step.
    """
    return RedirectResponse(
        url=svc.url_for_current_step(),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/regions", include_in_schema=False, response_model=None)
def get_onboarding_regions(
    request: Request,
    svc: OnboardingService = Depends(get_onboarding),
    cfg: ConfigSource = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    """Step 1 — region selection.

    Renders when state == NOT_STARTED; otherwise 302 to current step.
    Passes ``region_slugs`` (list of slug strings) derived from the stored
    integer IDs so the template can pre-select cards without a type mismatch.
    """
    if svc.current() != OnboardingState.NOT_STARTED:
        return _mismatch_redirect(svc)
    settings = cfg.current()
    data: dict[str, object] = {"region_slugs": ids_to_slugs(settings.regions)}
    return _wizard_response(request, templates, step=1, data=data, settings=settings)


@router.get("/smtp", include_in_schema=False, response_model=None)
def get_onboarding_smtp(
    request: Request,
    svc: OnboardingService = Depends(get_onboarding),
    cfg: ConfigSource = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    """Step 2 — bot mailbox (SMTP) configuration.

    Renders when state == REGIONS_SET; otherwise 302 to current step.

    smtp_login and smtp_from_name are not stored in config.json (credentials
    live in state.db via SmtpCredentials — ADR-020). We pass empty strings so
    the form renders with empty fields, ready for first-time entry.
    Password is deliberately NOT passed (never in config.json).
    """
    if svc.current() != OnboardingState.REGIONS_SET:
        return _mismatch_redirect(svc)
    settings = cfg.current()
    email = settings.notifications.email
    data: dict[str, object] = {
        "smtp_host": email.smtp_host or "",
        "smtp_port": email.smtp_port,
        # smtp_login / smtp_from_name live in state.db (SmtpCredentials), not config.json
        "smtp_login": "",
        "smtp_from_name": "",
    }
    return _wizard_response(request, templates, step=2, data=data, settings=settings)


@router.get("/recipients", include_in_schema=False, response_model=None)
def get_onboarding_recipients(
    request: Request,
    svc: OnboardingService = Depends(get_onboarding),
    cfg: ConfigSource = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    """Step 3 — recipient email address.

    Renders when state == SMTP_CONFIGURED; otherwise 302 to current step.
    """
    if svc.current() != OnboardingState.SMTP_CONFIGURED:
        return _mismatch_redirect(svc)
    settings = cfg.current()
    recipients = settings.notifications.email.recipients
    data: dict[str, object] = {
        "recipient_email": ", ".join(str(r) for r in recipients),
        "send_test_email": True,
    }
    return _wizard_response(request, templates, step=3, data=data, settings=settings)


@router.get("/test-email", include_in_schema=False, response_model=None)
def get_onboarding_test_email(
    request: Request,
    svc: OnboardingService = Depends(get_onboarding),
    cfg: ConfigSource = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse | RedirectResponse:
    """Step 4 — monitoring is running (completion screen).

    Renders when state == RECIPIENTS_SET; otherwise 302 to current step.
    Passes ``settings`` directly so the template can access
    ``{{ settings.interval_minutes }}``.
    """
    if svc.current() != OnboardingState.RECIPIENTS_SET:
        return _mismatch_redirect(svc)
    settings = cfg.current()
    email = settings.notifications.email
    recipients = email.recipients
    data: dict[str, object] = {
        "region_slugs": ids_to_slugs(settings.regions),
        "region_title_by_slug": REGION_TITLE_BY_SLUG,
        "smtp_login": "",  # credentials live in state.db, not config.json
        "recipient_email": ", ".join(str(r) for r in recipients) if recipients else "",
    }
    return _wizard_response(request, templates, step=4, data=data, settings=settings)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class AdvanceBody(BaseModel):
    """JSON body for POST /onboarding/advance."""

    from_state: str
    to_state: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/state")
def get_onboarding_state(
    svc: OnboardingService = Depends(get_onboarding),
) -> JSONResponse:
    """Return current onboarding state and the UI URL for that step.

    Returns:
        200 with ``{"state": "<state_value>", "url": "<url_for_current_step>"}``.
    """
    state = svc.current()
    url = svc.url_for_current_step()
    return JSONResponse(content={"state": state.value, "url": url})


@router.post("/advance", status_code=204)
def post_advance(
    body: AdvanceBody,
    svc: OnboardingService = Depends(get_onboarding),
) -> None:
    """Attempt a state transition from_state → to_state.

    Parses the string values to ``OnboardingState`` enum members.
    Delegates guard-checking and persistence to ``OnboardingService.advance()``.

    Returns:
        204 No Content on success.
        422 if from_state or to_state strings are invalid enum values.
        409 if the transition is illegal (guard unsatisfied or state mismatch).
    """
    try:
        from_state = OnboardingState(body.from_state)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid from_state: {body.from_state!r}",
        ) from exc

    try:
        to_state = OnboardingState(body.to_state)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid to_state: {body.to_state!r}",
        ) from exc

    try:
        svc.advance(from_state, to_state)
    except InvalidTransitionError as exc:
        # 409 body shape per docs/onboarding.md:
        # {"error": "invalid_transition", "current_state": "<curr>", "redirect_to": "/onboarding"}
        raise HTTPException(
            status_code=409,
            detail={
                "error": "invalid_transition",
                "current_state": exc.current_state,
                "redirect_to": "/onboarding",
            },
        ) from exc


@router.post("/skip-email", status_code=204)
def post_skip_email(
    svc: OnboardingService = Depends(get_onboarding),
) -> None:
    """Set the email_skipped flag.

    Only valid in ``smtp_configured`` or ``recipients_set`` states.

    Returns:
        204 No Content on success.
        409 if the current state does not permit skip-email.
    """
    try:
        svc.skip_email()
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "invalid_transition",
                "current_state": exc.current_state,
                "redirect_to": "/onboarding",
            },
        ) from exc


# ---------------------------------------------------------------------------
# POST Wizard endpoints (htmx form submission)
# ---------------------------------------------------------------------------


def _hx_redirect(url: str) -> HTMLResponse:
    """Return 200 with HX-Redirect header so htmx performs client-side navigation."""
    return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": url})


def _hx_redirect_to_step(svc: OnboardingService) -> HTMLResponse:
    """HX-Redirect to the current wizard step (used for concurrent-submit mismatch)."""
    return _hx_redirect(svc.url_for_current_step())


def _rerender(
    request: Request,
    templates: Jinja2Templates,
    cfg: ConfigSource,
    step: int,
    error: str,
    extra_data: dict[str, Any] | None = None,
) -> HTMLResponse:
    """Re-render the wizard at the given step with an error message.

    Step 1 always gets ``region_slugs`` injected so the region-card template
    can pre-select the current state even after a form error.
    """
    settings = cfg.current()
    data: dict[str, Any] = {"error": error}
    if step == 1:
        data["region_slugs"] = ids_to_slugs(settings.regions)
    elif step == 4:
        data["region_slugs"] = ids_to_slugs(settings.regions)
        data["region_title_by_slug"] = REGION_TITLE_BY_SLUG
    if extra_data:
        data.update(extra_data)
    return _wizard_response(request, templates, step=step, data=data, settings=settings)


def _is_state_mismatch(exc: InvalidTransitionError, expected_from: OnboardingState) -> bool:
    """Return True when the current state differs from the expected from_state.

    This indicates a concurrent submit or already-advanced state — the right
    response is to redirect the user to the current step rather than show an
    error.  When current_state == from_state the guard failed (e.g. test email
    not confirmed), so we re-render with an explanation.
    """
    return exc.current_state != expected_from.value


def _is_concurrent_advance_race(exc: InvalidTransitionError) -> bool:
    """True for InvalidTransitionError caused by concurrent advance (skip handlers).

    Skip-handlers выполняют цепочку из 2-3 service-calls (skip_email + 2x advance).
    При race condition (двойной submit) state может оказаться past expected
    from-state на любой из этих операций. Все эти случаи характеризуются
    одним признаком: exc.current_state — это валидный OnboardingState, дальше
    в FSM от expected от-state'а. Распознаём по тому что текущий state — не
    тот что requested и не not_started (regression невозможен в этой FSM).

    Применяется ТОЛЬКО в skip-хендлерах с цепочкой переходов, где невозможно
    использовать узкий ``_is_state_mismatch`` (тот рассчитан на одиночный advance).
    """
    # not_started — единственный state в котором skip-handler НЕ должен оказаться;
    # любой другой = пользователь уже прошёл часть FSM, redirect на актуальный step.
    return exc.current_state != OnboardingState.NOT_STARTED.value


def _test_lot_fixture() -> LotPublicDTO:
    """Return a deterministic synthetic LotPublicDTO for SMTP send tests."""
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
# Step handlers (OCP: new steps added without touching the dispatcher)
# ---------------------------------------------------------------------------


def _handle_step1_next(
    request: Request,
    form: dict[str, Any],
    svc: OnboardingService,
    cfg: ConfigSource,
    templates: Jinja2Templates,
) -> HTMLResponse:
    """Handle step 1 action=next: parse regions, save, advance."""
    raw_slugs = form.getlist("regions") if hasattr(form, "getlist") else form.get("regions", [])
    if isinstance(raw_slugs, str):
        raw_slugs = [raw_slugs]

    region_ids: list[int] = []
    for slug in raw_slugs:
        try:
            rid = slug_to_id(slug)
        except KeyError:
            return _rerender(
                request, templates, cfg, step=1,
                error="Выберите хотя бы один регион из списка.",
            )
        region_ids.append(rid)

    if not region_ids:
        return _rerender(
            request, templates, cfg, step=1,
            error="Выберите хотя бы один регион.",
        )

    # Persist regions via ConfigSource (pattern from settings.py post_regions)
    current = cfg.current()
    new_settings = current.model_copy(update={"regions": region_ids})
    cfg.save(new_settings)

    try:
        svc.advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
    except InvalidTransitionError:
        return _hx_redirect_to_step(svc)

    return _hx_redirect("/onboarding/smtp")


def _handle_step2_next(
    request: Request,
    form: dict[str, Any],
    svc: OnboardingService,
    cfg: ConfigSource,
    settings_svc: SettingsService,
    templates: Jinja2Templates,
) -> HTMLResponse:
    """Handle step 2 action=next: validate SMTP creds, save, advance."""
    smtp_host = (form.get("smtp_host") or "").strip()
    smtp_login = (form.get("smtp_login") or "").strip()
    smtp_pass = form.get("smtp_pass") or ""
    # TODO(bd ljp): smtp_from_name parsed but NOT persisted — SmtpCredentials
    # domain model has no from_name field. Either add to model (+ migration) or
    # remove field from _step2.html.jinja. Tracked separately.
    smtp_from_name = (form.get("smtp_from_name") or "").strip()
    if smtp_from_name:
        _log.debug(
            "onboarding step 2: smtp_from_name=%r submitted but not persisted (bd ljp)",
            smtp_from_name,
        )

    try:
        smtp_port = int(form.get("smtp_port") or 0)
    except (ValueError, TypeError):
        smtp_port = 0

    _input_err = _validate_smtp_input(smtp_host, smtp_login, smtp_pass, smtp_port)
    if _input_err:
        return _rerender(
            request, templates, cfg, step=2,
            error=_input_err,
            extra_data={"smtp_host": smtp_host, "smtp_port": smtp_port,
                        "smtp_login": smtp_login, "smtp_from_name": smtp_from_name},
        )

    try:
        creds = SmtpCredentials(
            smtp_user=smtp_login,
            smtp_password=smtp_pass,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
        settings_svc.set_smtp_credentials(creds)
    except SmtpHostPolicyError:
        # Не рендерим str(exc) — он содержит submitted hostname (PII per reviewer B1).
        return _rerender(
            request, templates, cfg, step=2,
            error="SMTP-хост недоступен или заблокирован политикой безопасности.",
            extra_data={"smtp_host": smtp_host, "smtp_port": smtp_port,
                        "smtp_login": smtp_login, "smtp_from_name": smtp_from_name},
        )
    except ValidationError as exc:
        # Pydantic-level rejection (e.g. smtp_port out of range, password empty).
        # Show first error message — full repr is too verbose for UI.
        first = exc.errors()[0] if exc.errors() else {"msg": "Некорректные данные"}
        return _rerender(
            request, templates, cfg, step=2,
            error=f"Ошибка ввода: {first.get('msg', 'неизвестная ошибка')}",
            extra_data={"smtp_host": smtp_host, "smtp_port": smtp_port,
                        "smtp_login": smtp_login, "smtp_from_name": smtp_from_name},
        )
    # NB: бизнес-исключения ловятся выше. Любая другая Exception — баг,
    # пробрасываем чтобы её увидеть в логах / monitoring, не маскируем под UI-error.

    try:
        svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
    except InvalidTransitionError as exc:
        if _is_state_mismatch(exc, OnboardingState.REGIONS_SET):
            return _hx_redirect_to_step(svc)
        # Guard fail: smtp_test_last_result_ok not set
        return _rerender(
            request, templates, cfg, step=2,
            error='Сначала нажмите "Проверить подключение" и дождитесь успешного результата.',
            extra_data={"smtp_host": smtp_host, "smtp_port": smtp_port,
                        "smtp_login": smtp_login, "smtp_from_name": smtp_from_name},
        )

    return _hx_redirect("/onboarding/recipients")


def _handle_step2_skip(svc: OnboardingService) -> HTMLResponse:
    """Handle step 2 action=skip: skip email, advance twice, redirect.

    Idempotency: при concurrent submit'е (двойной клик в двух вкладках) первый
    запрос меняет state, второй приходит уже к next state — ловим
    InvalidTransitionError ТОЛЬКО при mismatch by-from-state и redirect'имся на
    текущий step (idempotent повтор). Любой другой InvalidTransitionError
    (guard-fail из реального бага) пробрасывается — bug должен быть видим.

    UI-слой защищён hx-disabled-elt="this" — кнопка disabled во время request,
    минимизирует race-окно. Server-слой — последний рубеж.
    """
    try:
        svc.skip_email()
        svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
        svc.advance(OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET)
    except InvalidTransitionError as exc:
        # Concurrent submit или повторный клик — state уже past where we wanted.
        # Любой mismatch by from-state в этой цепочке означает: кто-то уже сделал
        # этот переход (или ещё дальше) — отдаём актуальный URL.
        if _is_concurrent_advance_race(exc):
            return _hx_redirect_to_step(svc)
        raise
    return _hx_redirect("/onboarding/recipients")


def _handle_step3_next(
    request: Request,
    form: dict[str, Any],
    svc: OnboardingService,
    cfg: ConfigSource,
    smtp_test_svc: SmtpTestService,
    templates: Jinja2Templates,
) -> HTMLResponse:
    """Handle step 3 action=next: validate recipients, save, optionally test, advance."""
    raw_email = (form.get("recipient_email") or "").strip()
    send_test = form.get("send_test_email") == "1"

    # Parse comma-separated emails
    if not raw_email:
        return _rerender(
            request, templates, cfg, step=3,
            error="Введите хотя бы один email-адрес получателя.",
            extra_data={"recipient_email": raw_email},
        )

    raw_parts = [p.strip() for p in raw_email.split(",") if p.strip()]
    validated_emails: list[str] = []
    for part in raw_parts:
        try:
            # Use pydantic v2 TypeAdapter (public API)
            validated = _EMAIL_VALIDATOR.validate_python(part)
            validated_emails.append(str(validated))
        except ValidationError:
            return _rerender(
                request, templates, cfg, step=3,
                error=f"Некорректный email-адрес: {part!r}",
                extra_data={"recipient_email": raw_email},
            )

    if not validated_emails:
        return _rerender(
            request, templates, cfg, step=3,
            error="Введите хотя бы один email-адрес получателя.",
            extra_data={"recipient_email": raw_email},
        )

    # Persist recipients via ConfigSource
    current = cfg.current()
    new_email = current.notifications.email.model_copy(
        update={"recipients": validated_emails}
    )
    new_notifications = current.notifications.model_copy(update={"email": new_email})
    new_settings = current.model_copy(update={"notifications": new_notifications})
    cfg.save(new_settings)

    # Optionally send test email (fire-and-forget; ok/fail surfaced on step 4)
    if send_test and validated_emails:
        test_lot = _test_lot_fixture()
        with contextlib.suppress(Exception):
            smtp_test_svc.test_send(test_lot, validated_emails[0])

    try:
        svc.advance(OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET)
    except InvalidTransitionError as exc:
        if _is_state_mismatch(exc, OnboardingState.SMTP_CONFIGURED):
            return _hx_redirect_to_step(svc)
        return _rerender(
            request, templates, cfg, step=3,
            error="Невозможно перейти к следующему шагу. Проверьте настройки SMTP.",
            extra_data={"recipient_email": raw_email},
        )

    return _hx_redirect("/onboarding/test-email")


def _handle_step3_skip(svc: OnboardingService) -> HTMLResponse:
    """Handle step 3 action=skip: skip email, advance, redirect.

    См. docstring _handle_step2_skip про idempotency-стратегию.
    """
    try:
        svc.skip_email()
        svc.advance(OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET)
    except InvalidTransitionError as exc:
        if _is_concurrent_advance_race(exc):
            return _hx_redirect_to_step(svc)
        raise
    return _hx_redirect("/onboarding/test-email")


def _should_trigger_backfill(
    lot_repo: LotRepository,
    settings: Settings,
) -> bool:
    """Return True if all guard conditions for auto-backfill are met (ADR-032).

    Three guards must all be true:
    1. No lots in the catalogue yet — avoid duplicate backfill if user re-onboards.
    2. At least one region configured — backfilling with an empty scope is a no-op.
    3. (Single-flight) BackfillService.start() is its own idempotency gate; no
       duplicate guard needed here.
    """
    if lot_repo.count_active() != 0:
        return False
    return bool(settings.regions)


def _handle_step4_next(
    request: Request,
    form: dict[str, Any],
    svc: OnboardingService,
    cfg: ConfigSource,
    templates: Jinja2Templates,
    lot_repo: LotRepository | None = None,
    backfill: BackfillService | None = None,
) -> HTMLResponse:
    """Handle step 4 action=next: advance to COMPLETED, then maybe auto-backfill."""
    try:
        svc.advance(OnboardingState.RECIPIENTS_SET, OnboardingState.COMPLETED)
    except InvalidTransitionError as exc:
        if _is_state_mismatch(exc, OnboardingState.RECIPIENTS_SET):
            return _hx_redirect_to_step(svc)
        return _rerender(
            request, templates, cfg, step=4,
            error=(
                "Тестовое письмо не подтверждено."
                " Отправьте тестовое письмо или выберите «Пропустить»."
            ),
        )

    # Trigger auto-backfill after successful onboarding completion (ADR-032).
    # Placed here (orchestration layer) to avoid coupling OnboardingService to
    # BackfillService (domain must not depend on infra services).
    supervisor = getattr(getattr(request.app, "state", None), "supervisor", None)
    if (
        supervisor is not None
        and lot_repo is not None
        and backfill is not None
        and _should_trigger_backfill(lot_repo, cfg.current())
    ):
        supervisor.start("backfill-auto", lambda stop: backfill.start(stop))
        _log.info("onboarding: completed → auto-backfill scheduled")

    return _hx_redirect("/")


# NB: dispatcher uses explicit if/elif on (step, action) — OCP-dict-dispatch
# был запланирован но не нужен пока шагов 4 (`_HANDLERS` removed per 0vn-fix2).
# Если шагов станет >6 — рефакторить в dict-dispatch.


@router.post("/save", include_in_schema=False, response_model=None)
async def post_onboarding_save(
    request: Request,
    step: int = 0,
    svc: OnboardingService = Depends(get_onboarding),
    cfg: ConfigSource = Depends(get_config_source),
    settings_svc: SettingsService = Depends(get_settings_service),
    smtp_test_svc: SmtpTestService = Depends(get_smtp_test),
    templates: Jinja2Templates = Depends(get_templates),
    lot_repo: LotRepository = Depends(get_lot_repo),
    backfill: BackfillService = Depends(get_backfill),
) -> HTMLResponse:
    """Dispatcher for all wizard POST submissions.

    Reads ``step`` from query-param and ``action`` from form body.
    Delegates to the appropriate step handler via explicit if/elif chain.

    On success: returns 200 with ``HX-Redirect`` header.
    On validation error: returns 200 with re-rendered wizard fragment.
    On unknown step: returns 400.
    """
    form = await request.form()
    action = form.get("action") or "next"

    key: tuple[int, str] = (step, str(action))

    # Step 2 handlers need settings_svc; step 3 needs smtp_test_svc.
    # We pass all context and each handler uses only what it needs.
    if key == (1, "next"):
        return _handle_step1_next(request, form, svc, cfg, templates)  # type: ignore[arg-type]
    elif key == (2, "next"):
        return _handle_step2_next(request, form, svc, cfg, settings_svc, templates)  # type: ignore[arg-type]
    elif key == (2, "skip"):
        return _handle_step2_skip(svc)
    elif key == (3, "next"):
        return _handle_step3_next(request, form, svc, cfg, smtp_test_svc, templates)  # type: ignore[arg-type]
    elif key == (3, "skip"):
        return _handle_step3_skip(svc)
    elif key == (4, "next"):
        return _handle_step4_next(request, form, svc, cfg, templates, lot_repo, backfill)  # type: ignore[arg-type]
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown step/action: step={step}, action={action!r}",
        )


@router.post("/smtp-test", include_in_schema=False, response_model=None)
async def post_onboarding_smtp_test(
    request: Request,
    settings_svc: SettingsService = Depends(get_settings_service),
    smtp_test_svc: SmtpTestService = Depends(get_smtp_test),
) -> HTMLResponse:
    """htmx endpoint: validate SMTP credentials and send a test email.

    Accepts form data: smtp_host, smtp_port, smtp_login, smtp_pass.

    Flow:
      1. Build SmtpCredentials and call SettingsService.set_smtp_credentials()
         (validates via SmtpHostPolicy.resolve_and_check + persists).
      2. Call SmtpTestService.test_send() with a synthetic test lot.
      3. Return an HTML fragment ``<span id="smtp-test-result" ...>`` for
         htmx outerHTML swap.

    All errors return 200 (not HTTP errors) — they are user-facing validation
    messages rendered as the fragment.  PII-free: no host/password in fragment.
    """
    ip = client_ip(request)
    now = time.monotonic()
    if not _smtp_test_rate_limiter.acquire(ip, now=now):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again in 10 seconds",
        )

    form = await request.form()
    smtp_host = (form.get("smtp_host") or "").strip()
    smtp_login = (form.get("smtp_login") or "").strip()
    smtp_pass = form.get("smtp_pass") or ""
    try:
        smtp_port = int(form.get("smtp_port") or 0)
    except (ValueError, TypeError):
        smtp_port = 0

    # Step 1: validate input fields before touching the service layer.
    # _validate_smtp_input is a pure function shared with _handle_step2_next (DRY).
    _input_err = _validate_smtp_input(smtp_host, smtp_login, smtp_pass, smtp_port)
    if _input_err:
        return HTMLResponse(
            content=(
                f'<span class="chip chip--err" id="smtp-test-result">'
                f"{_input_err}</span>"
            ),
            status_code=200,
        )

    # Step 1b: persist credentials (may raise SmtpHostPolicyError / ValidationError)
    try:
        creds = SmtpCredentials(
            smtp_user=smtp_login,
            smtp_password=smtp_pass,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
        settings_svc.set_smtp_credentials(creds)
    except SmtpHostPolicyError:
        _policy_err_msg = (
            '<span class="chip chip--err" id="smtp-test-result">'
            "Ошибка: хост недоступен или заблокирован политикой"
            "</span>"
        )
        return HTMLResponse(content=_policy_err_msg, status_code=200)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": "некорректные данные"}
        _val_err_msg = (
            f'<span class="chip chip--err" id="smtp-test-result">'
            f"Ошибка ввода: {first.get('msg', 'неизвестная ошибка')}</span>"
        )
        return HTMLResponse(content=_val_err_msg, status_code=200)
    # NB: остальные Exception (баги) пробрасываем — должны быть видимы в логах.

    # Step 2: send test email
    test_lot = _test_lot_fixture()
    result = smtp_test_svc.test_send(test_lot, smtp_login or "test@localhost")

    if result.ok:
        _ok_msg = (
            '<span class="chip chip--ok" id="smtp-test-result">'
            "ОК — письмо отправлено"
            "</span>"
        )
        return HTMLResponse(content=_ok_msg, status_code=200)
    else:
        # detail is PII-safe per NotifyResult contract (no host/recipient/password)
        detail_safe = result.detail[:200] if result.detail else "Неизвестная ошибка"
        _err_msg = (
            f'<span class="chip chip--err" id="smtp-test-result">'
            f"Ошибка: {detail_safe}</span>"
        )
        return HTMLResponse(content=_err_msg, status_code=200)
