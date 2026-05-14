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

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.

See docs/onboarding.md for the FSM spec and 409 body shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from fis_monitor.domain.errors import InvalidTransitionError
from fis_monitor.domain.interfaces import ConfigSource
from fis_monitor.domain.models import OnboardingState, Settings
from fis_monitor.services.onboarding import OnboardingService
from fis_monitor.web.deps import get_config_source, get_onboarding, get_templates

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

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

    Note: the template checks ``r.id in data.regions`` where r.id is a string
    ('dfo', 'arctic'), but Settings.regions is list[int] (domain IDs). This is
    a known UI/domain model mismatch — the wizard will show no pre-selection
    until a dedicated mapping task resolves it.
    # TODO(53e): map Settings.regions (list[int]) to region-card string IDs
    #            ('dfo'/'arctic') before passing to step 1 template.
    """
    if svc.current() != OnboardingState.NOT_STARTED:
        return _mismatch_redirect(svc)
    settings = cfg.current()
    data: dict[str, object] = {"regions": settings.regions}
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
        "regions": settings.regions,
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
