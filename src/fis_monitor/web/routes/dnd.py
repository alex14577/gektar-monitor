"""FastAPI APIRouter for Do-Not-Disturb endpoints.

Endpoints:
  POST /dnd         — Activate DnD for N minutes. Returns 204.
  GET  /dnd/custom  — HTMX partial: form for entering a custom duration.

DI: ``DndService`` is injected via ``Depends(get_dnd_service)``.
    ``Clock`` is injected via ``Depends(get_clock)``.

Business logic lives entirely in ``DndService`` — the route only marshals
HTTP in/out and delegates.

CSRF: POST endpoint passes through ``CsrfHostOriginMiddleware`` automatically.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from fis_monitor.domain.interfaces import Clock
from fis_monitor.services.dnd import DndService
from fis_monitor.web.deps import get_clock, get_dnd_service, get_templates

__all__ = ["router"]

router = APIRouter(prefix="/dnd", tags=["dnd"])


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class DndRequest(BaseModel):
    """Body for POST /dnd."""

    minutes: int = Field(
        ...,
        ge=1,
        le=1440 * 7,
        description="Duration of the Do-Not-Disturb window in minutes (1..10080).",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=204)
def set_dnd(
    body: DndRequest,
    svc: DndService = Depends(get_dnd_service),
    clock: Clock = Depends(get_clock),
) -> None:
    """Activate Do-Not-Disturb for *body.minutes* minutes from now.

    Returns:
        204 No Content on success.
        422 Unprocessable Entity if ``minutes`` is out of range (Pydantic).
    """
    svc.set_dnd_until(clock.now(), body.minutes)


@router.get("/custom", response_class=HTMLResponse)
def dnd_custom_form(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Return the HTMX partial with the custom-duration form.

    Targeted by the "Своё время…" link in the DnD menu (base.html.jinja).
    The partial is appended to ``<body>`` via ``hx-swap="beforeend"``.

    Returns:
        200 HTML — Jinja2 partial ``partials/_dnd_custom.html.jinja``.
    """
    return templates.TemplateResponse(
        request=request,
        name="partials/_dnd_custom.html.jinja",
    )
