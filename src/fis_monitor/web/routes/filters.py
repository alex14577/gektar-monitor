"""FastAPI APIRouter for view-filter endpoints.

View-filters are ephemeral session state — they live in a signed JSON cookie
``view_filters`` and are NEVER persisted to config.json or the database.

Endpoints:
  GET  /filters/subjects — partial HTML with subject checkboxes (modal/popover).
  POST /filters/view     — apply filters; 204 + Set-Cookie.
  POST /filters/clear    — reset filters; 204 + expiring Set-Cookie.

Cookie design:
  name      : view_filters
  value     : JSON-serialised ViewFilters (no HMAC in MVP — localhost-only per ADR-011)
  httponly  : True
  samesite  : lax
  path      : /
  max_age   : 30 days (clear sets max_age=0)

TODO(future-bd): add HMAC signing if the app ever exposes a non-loopback interface.

DI: ViewFiltersService is injected via get_view_filters_service() from deps.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from fis_monitor.services.view_filters import (
    PLACEHOLDER_SUBJECTS,
    ViewFilters,
    ViewFiltersService,
)
from fis_monitor.web.deps import get_templates, get_view_filters_service

__all__ = ["router"]

_log = logging.getLogger(__name__)

_COOKIE_NAME = "view_filters"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/filters", tags=["filters"])

# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class ViewFiltersBody(BaseModel):
    """JSON body for POST /filters/view.

    All fields are optional so a partial payload is accepted (partial update
    semantics are intentionally NOT supported here — each POST replaces the
    full filter state; omitted fields revert to defaults).
    """

    model_config = {"extra": "forbid"}

    subjects: list[str] = Field(default_factory=list)
    area_min: int | None = Field(default=None, ge=0)
    area_max: int | None = Field(default=None, ge=0)
    only_new: bool = False
    only_stars: bool = False


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def _set_filter_cookie(response: Response, value: str) -> None:
    """Attach a persistent view_filters cookie to *response*."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=value,
        max_age=_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def _clear_filter_cookie(response: Response) -> None:
    """Attach an expiring view_filters cookie to *response* (effectively deletes it)."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value="",
        max_age=0,
        path="/",
        httponly=True,
        samesite="lax",
    )


def _current_filters(request: Request, svc: ViewFiltersService) -> ViewFilters:
    """Read and parse the view_filters cookie; return defaults if absent/invalid."""
    raw = request.cookies.get(_COOKIE_NAME, "")
    return svc.deserialize(raw) or ViewFilters()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/subjects", include_in_schema=False, response_model=None)
def get_subjects(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
    svc: ViewFiltersService = Depends(get_view_filters_service),
) -> HTMLResponse:
    """Return partial HTML with subject checkboxes.

    Subject list: MVP uses PLACEHOLDER_SUBJECTS (3-5 items).
    TODO(future-bd): replace with SettingsService.list_subjects() once
    the full geo-data layer lands.

    The checkboxes carry ``form="filters"`` so they participate in the main
    #filters form submission even though they live in a popover outside it.
    """
    current = _current_filters(request, svc)
    ctx = {
        "subjects": PLACEHOLDER_SUBJECTS,
        "selected_subjects": current.subjects,
    }
    return templates.TemplateResponse(
        request, "partials/_filters_subjects.html.jinja", ctx
    )


@router.post("/view", status_code=204, response_model=None)
def post_view_filters(
    body: ViewFiltersBody,
    request: Request,
    svc: ViewFiltersService = Depends(get_view_filters_service),
) -> Response:
    """Apply view filters; persist state in cookie.

    Body is validated by Pydantic — invalid payloads return 422 automatically.

    Returns 204 No Content + Set-Cookie header.
    """
    filters = ViewFilters(
        subjects=body.subjects,
        area_min=body.area_min,
        area_max=body.area_max,
        only_new=body.only_new,
        only_stars=body.only_stars,
    )
    cookie_value = svc.serialize(filters)
    response = Response(status_code=204)
    _set_filter_cookie(response, cookie_value)
    _log.debug("view_filters applied: %r", cookie_value)
    return response


@router.post("/clear", status_code=204, response_model=None)
def post_clear_filters() -> Response:
    """Reset view filters by expiring the cookie.

    Returns 204 No Content + Set-Cookie with max_age=0 (browser deletes cookie).
    """
    response = Response(status_code=204)
    _clear_filter_cookie(response)
    _log.debug("view_filters cleared")
    return response
