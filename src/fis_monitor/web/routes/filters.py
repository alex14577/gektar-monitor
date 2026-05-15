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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID, subjects_for_macros
from fis_monitor.services.view_filters import (
    ViewFilters,
    ViewFiltersService,
)
from fis_monitor.web.deps import get_config_source, get_templates, get_view_filters_service

__all__ = ["router"]

_log = logging.getLogger(__name__)

_COOKIE_NAME = "view_filters"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/filters", tags=["filters"])

# ---------------------------------------------------------------------------
# Form-parsing helpers
# ---------------------------------------------------------------------------


def _parse_int_or_none(v: str | None) -> int | None:
    """Convert a form string to int, treating empty string as None.

    Raises HTTPException(422) on non-numeric or negative input so callers
    get a consistent 422 rather than an unhandled ValueError.
    """
    if v is None or v == "":
        return None
    try:
        result = int(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid integer value: {v!r}") from None
    if result < 0:
        raise HTTPException(
            status_code=422, detail=f"Value must be >= 0, got {result}"
        )
    return result


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
    config_source: Any = Depends(get_config_source),
) -> HTMLResponse:
    """Return partial HTML with subject checkboxes scoped to current macro-regions.

    Subject list is derived from subjects_for_macros(settings.regions) per ADR-031.
    Labels are human-readable names from SUBJECT_TITLE_BY_ID; values are site-ids.

    The checkboxes carry ``form="filters"`` so they participate in the main
    #filters form submission even though they live in a popover outside it.
    """
    settings = config_source.current()
    scoped_ids = subjects_for_macros(settings.regions)
    subjects = [(sid, SUBJECT_TITLE_BY_ID[sid]) for sid in scoped_ids]
    current = _current_filters(request, svc)
    ctx = {
        "subjects": subjects,
        "selected_subjects": current.subjects,
    }
    return templates.TemplateResponse(
        request, "partials/_filters_subjects.html.jinja", ctx
    )


@router.post("/view", status_code=204, response_model=None)
def post_view_filters(
    request: Request,
    svc: ViewFiltersService = Depends(get_view_filters_service),
    subjects: Annotated[list[str], Form()] = [],  # noqa: B006 — FastAPI never mutates this default; reassigning to None would break repeated-key parsing
    area_min: Annotated[str | None, Form()] = None,
    area_max: Annotated[str | None, Form()] = None,
    only_new: Annotated[str | None, Form()] = None,
    only_stars: Annotated[str | None, Form()] = None,
) -> Response:
    """Apply view filters submitted as application/x-www-form-urlencoded by htmx.

    Unknown form fields are silently ignored — form-data has no equivalent of
    Pydantic extra='forbid'. This is acceptable because only parsed values are
    written to the cookie; unknown fields are never propagated.

    Cross-field validation (area_min <= area_max) is intentionally deferred to
    gektar_monitor-gho — matches pre-existing behaviour of the JSON variant.

    Returns 204 No Content + Set-Cookie header.
    """
    if len(subjects) > 50:
        raise HTTPException(status_code=422, detail="subjects: at most 50 items allowed")
    for s in subjects:
        if len(s) > 128:
            raise HTTPException(
                status_code=422, detail="subjects: each item must be <= 128 characters"
            )

    filters = ViewFilters(
        subjects=subjects,
        area_min=_parse_int_or_none(area_min),
        area_max=_parse_int_or_none(area_max),
        # Checkbox unchecked → key absent → None → False.
        # Checked → key present with any value (typically "on") → True.
        # only_new="" (empty value, key present) also → True — non-browser edge case, intentional.
        only_new=only_new is not None,
        only_stars=only_stars is not None,
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
