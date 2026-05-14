"""FastAPI APIRouter for onboarding FSM endpoints.

Endpoints:
  GET  /onboarding/state   — return current FSM state and UI URL.
  POST /onboarding/advance — attempt a state transition.
  POST /onboarding/skip-email — set email_skipped flag.

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.

See docs/onboarding.md for the FSM spec and 409 body shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fis_monitor.domain.errors import InvalidTransitionError
from fis_monitor.domain.models import OnboardingState
from fis_monitor.services.onboarding import OnboardingService
from fis_monitor.web.deps import get_onboarding

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


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
