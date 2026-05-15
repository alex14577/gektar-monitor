"""FastAPI APIRouter for lot catalog endpoints.

Endpoints:
  GET  /lots                   — filtered, paginated lot catalog via LotQueryService.search()
  GET  /lots/{lot_id}          — single lot via LotQueryService.get_by_id()
  GET  /lots/{lot_id}/redirect — 302 redirect to canonical lot page on torgi.gov.ru
  GET  /lots/{lot_id}/details  — HTMX partial: lot detail card + user state
  POST /lots/{lot_id}/star     — toggle starred flag (204)
  POST /lots/{lot_id}/archive  — toggle archived flag (204)
  POST /lots/{lot_id}/note     — set free-text note, max 4096 chars (204)

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from fis_monitor.domain.models import LotUserDTO
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.services.lot_query import LotFilters, LotQueryService, Page
from fis_monitor.services.lot_user_state import LotNotFoundError, LotUserStateService
from fis_monitor.web.deps import get_lot_query, get_lot_user_state_service, get_templates

# Canonical upstream base URL — domain constant, not user-configurable (ADR-024).
_TORGI_URL_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/lots", tags=["lots"])


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _page_to_dict(page: Page[LotUserDTO]) -> dict:
    """Serialise Page[LotUserDTO] to a plain dict for JSONResponse.

    Pydantic is used internally by LotUserDTO.model_dump(); this helper
    prevents raw_json leakage (the model serialiser strips it).
    """
    return {
        "items": [item.model_dump(mode="json") for item in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
def list_lots(
    regions: Annotated[list[int] | None, Query()] = None,
    area_sqm_min: Decimal | None = None,
    area_sqm_max: Decimal | None = None,
    status: str | None = None,
    cursor: str | None = None,
    page_size: int = Query(default=50, ge=1, le=200),
    svc: LotQueryService = Depends(get_lot_query),
) -> JSONResponse:
    """Return a filtered, cursor-paginated page of active lots."""
    try:
        filters = LotFilters(
            regions=tuple(regions or []),
            area_sqm_min=area_sqm_min,
            area_sqm_max=area_sqm_max,
            status=status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        page = svc.search(filters, page_size=page_size, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse(content=_page_to_dict(page))


@router.get("/{lot_id}")
def get_lot(
    lot_id: int,
    svc: LotQueryService = Depends(get_lot_query),
) -> JSONResponse:
    """Return a single lot by ID, or 404 if not found."""
    lot = svc.get_by_id(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail=f"Lot {lot_id} not found")
    return JSONResponse(content=lot.model_dump(mode="json"))


@router.get("/{lot_id}/redirect")
def redirect_to_torgi(
    lot_id: int,
    svc: LotQueryService = Depends(get_lot_query),
) -> RedirectResponse:
    """Redirect to the canonical lot page on torgi.gov.ru.

    Returns 302 to the upstream detail URL when the lot exists in the local DB.
    Returns 404 when the lot is not found — prevents open redirects to arbitrary IDs.
    """
    lot = svc.get_by_id(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail=f"Lot {lot_id} not found")
    external_url = _TORGI_URL_BUILDER.lot_detail_url(lot_id=lot_id)
    return RedirectResponse(url=external_url, status_code=302)


# ---------------------------------------------------------------------------
# User-state endpoints
# ---------------------------------------------------------------------------


@router.get("/{lot_id}/details")
def get_lot_details(
    lot_id: int,
    request: Request,
    svc: LotUserStateService = Depends(get_lot_user_state_service),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Return an HTMX partial with the lot detail card + user state.

    Returns 200 HTML fragment suitable for hx-swap on the caller side.
    Returns 404 when the lot does not exist.
    """
    result = svc.details(lot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Lot {lot_id} not found")
    lot, user_state = result
    return templates.TemplateResponse(
        request=request,
        name="partials/_lot_details.html.jinja",
        context={"lot": lot, "user_state": user_state},
    )


@router.post("/{lot_id}/star", status_code=204)
def toggle_star(
    lot_id: int,
    svc: LotUserStateService = Depends(get_lot_user_state_service),
) -> Response:
    """Toggle the starred flag for a lot. Returns 204 on success, 404 if lot missing."""
    try:
        svc.toggle_star(lot_id)
    except LotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/{lot_id}/archive", status_code=204)
def toggle_archive(
    lot_id: int,
    svc: LotUserStateService = Depends(get_lot_user_state_service),
) -> Response:
    """Toggle the archived flag for a lot. Returns 204 on success, 404 if lot missing."""
    try:
        svc.toggle_archive(lot_id)
    except LotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


class _NoteBody(BaseModel):
    """Request body for POST /lots/{lot_id}/note."""

    note: str = Field(max_length=4096)


@router.post("/{lot_id}/note", status_code=204)
def set_note(
    lot_id: int,
    body: _NoteBody,
    svc: LotUserStateService = Depends(get_lot_user_state_service),
) -> Response:
    """Persist a free-text note for a lot.

    Accepts JSON body: ``{"note": "..."}`` (max 4096 chars).
    Returns 204 on success, 400 if note too long, 404 if lot missing.
    """
    try:
        svc.set_note(lot_id, body.note)
    except LotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)
