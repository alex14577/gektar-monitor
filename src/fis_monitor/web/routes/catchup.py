"""FastAPI APIRouter for catch-up banner dismissal.

Endpoints:
  POST /catchup/dismiss — dismiss the catch-up banner for 24 hours (MVP).
    Returns 204 No Content.

The catch-up banner notifies the user about lots added while they were
offline.  This endpoint lets the frontend dismiss it via HTMX hx-post.

DI: ``CatchupDismissService`` is injected via ``Depends(get_catchup_dismiss)``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.web.deps import get_catchup_dismiss

__all__ = ["router"]

router = APIRouter(prefix="/catchup", tags=["catchup"])


@router.post("/dismiss", status_code=204)
def catchup_dismiss(
    svc: CatchupDismissService = Depends(get_catchup_dismiss),
) -> Response:
    """Dismiss the catch-up banner for 24 hours.

    Records ``now + 24h`` as the dismissal deadline in the state KV store.
    The banner will not appear again until the window expires or the user
    goes offline again after that point.

    Returns:
        204 No Content — always; the client discards the banner via hx-swap.
    """
    from datetime import UTC, datetime

    svc.dismiss(now=datetime.now(UTC))
    return Response(status_code=204)
