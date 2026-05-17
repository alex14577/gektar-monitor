"""FastAPI APIRouter for catch-up banner dismissal.

Endpoints:
  POST /catchup/dismiss — dismiss the catch-up banner for 24 hours (MVP).
    Returns 204 No Content.

The catch-up banner notifies the user about lots added while they were
offline.  This endpoint lets the frontend dismiss it via HTMX hx-post.

DI: ``CatchupDismissService`` is injected via ``Depends(get_catchup_dismiss)``.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.web._helpers import client_ip
from fis_monitor.web.deps import get_catchup_dismiss
from fis_monitor.web.rate_limit import RateLimiter

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Rate limiter — 30 requests per 60 seconds per client IP.
# Module-level singleton; can be replaced in tests by reassigning before
# TestClient construction.
# ---------------------------------------------------------------------------

_catchup_rate_limiter = RateLimiter(max_requests=30, window_seconds=60)

router = APIRouter(prefix="/catchup", tags=["catchup"])


@router.post("/dismiss", status_code=204)
def catchup_dismiss(
    request: Request,
    svc: CatchupDismissService = Depends(get_catchup_dismiss),
) -> Response:
    """Dismiss the catch-up banner for 24 hours.

    Records ``now + 24h`` as the dismissal deadline in the state KV store.
    The banner will not appear again until the window expires or the user
    goes offline again after that point.

    Returns:
        204 No Content — always; the client discards the banner via hx-swap.
        429 Too Many Requests if rate limit exceeded (30 req / 60 s per IP).
    """
    from datetime import UTC, datetime

    ip = client_ip(request)
    if not _catchup_rate_limiter.acquire(ip, now=time.monotonic()):
        raise HTTPException(status_code=429, detail="Too many requests")
    svc.dismiss(now=datetime.now(UTC))
    return Response(status_code=204)
