"""FastAPI APIRouter for lot catalog endpoints.

Endpoints:
  GET /lots          — filtered, paginated lot catalog via LotQueryService.search()
  GET /lots/{lot_id} — single lot via LotRepository.get()

DI: all dependencies are injected via Depends(); routes are decoupled from
Container and testable via app.dependency_overrides.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from fis_monitor.domain.models import LotUserDTO
from fis_monitor.services.lot_query import LotFilters, LotQueryService, Page
from fis_monitor.web.deps import get_lot_query

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
