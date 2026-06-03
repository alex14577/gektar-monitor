"""FastAPI APIRouter for backfill endpoints.

Endpoints:
  POST /backfill/start   — single-flight start; 202 if started, 409 if running.
  GET  /backfill/status  — progress snapshot (status, running, current_region, …).
  POST /backfill/cancel  — cancel any running backfill; 204 always.

DI: ``BackfillService`` is injected via ``Depends(get_backfill)``.

CSRF: POST endpoints pass through ``CsrfHostOriginMiddleware`` automatically.

Rate limiting: 3 requests per 60 seconds per client IP, shared across /start
and /cancel (backfill is a heavy operation; start and cancel are logically
coupled — the same budget governs both).
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from fis_monitor.domain.interfaces import LotRepository
from fis_monitor.services.backfill import BackfillService, BackfillStatus
from fis_monitor.web._helpers import client_ip
from fis_monitor.web.deps import get_backfill, get_lot_repo
from fis_monitor.web.rate_limit import RateLimiter

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Rate limiter — 3 requests per 60 seconds per client IP, shared across
# /start and /cancel.  Module-level singleton; reassign in tests.
# ---------------------------------------------------------------------------

_backfill_rate_limiter = RateLimiter(max_requests=3, window_seconds=60.0)

router = APIRouter(prefix="/backfill", tags=["backfill"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start", status_code=202)
def backfill_start(
    request: Request,
    svc: BackfillService = Depends(get_backfill),
) -> JSONResponse:
    """Start a paginated backfill of the lot catalogue.

    Single-flight: if a backfill is already running, returns 409 Conflict.
    Thread spawning is done INSIDE ``BackfillService.start()`` (P1-5); the
    route calls ``start()`` synchronously and uses its bool return value as
    the race-free single-flight gate — no TOCTOU ``is_running()`` pre-check.

    Returns:
        202 Accepted  — backfill started.
        409 Conflict  — another backfill is already running.
        429 Too Many Requests — rate limit exceeded (3 req / 60 s per IP,
            shared with /cancel).
    """
    ip = client_ip(request)
    if not _backfill_rate_limiter.acquire(ip, now=time.monotonic()):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again later",
        )
    # A never-set external stop event; shutdown is coordinated via
    # BackfillService.cancel() from the lifespan (P0-4).
    external_stop = threading.Event()
    started = svc.start(external_stop)
    if not started:
        raise HTTPException(
            status_code=409,
            detail="Backfill already running",
        )
    return JSONResponse(status_code=202, content={"status": "started"})


@router.get("/status")
def backfill_status(
    svc: BackfillService = Depends(get_backfill),
    lot_repo: LotRepository = Depends(get_lot_repo),
) -> dict:
    """Return a JSON snapshot of the current backfill progress.

    Always 200; ``running=false`` when no backfill is active.
    """
    snap: BackfillStatus = svc.status()
    return {
        "status": snap.status,
        "running": snap.running,
        "current_region": snap.current_region,
        "current_page": snap.current_page,
        "regions_total": snap.regions_total,
        "started_at": snap.started_at,
        "updated_at": snap.updated_at,
        "active_lot_count": lot_repo.count_active(),
    }


@router.post("/cancel", status_code=204)
def backfill_cancel(
    request: Request,
    svc: BackfillService = Depends(get_backfill),
) -> Response:
    """Cancel any running backfill.

    Idempotent — safe to call when no backfill is active.

    Returns:
        204 No Content — cancelled (or was already idle).
        429 Too Many Requests — rate limit exceeded (3 req / 60 s per IP,
            shared with /start).
    """
    ip = client_ip(request)
    if not _backfill_rate_limiter.acquire(ip, now=time.monotonic()):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again later",
        )
    svc.cancel()
    return Response(status_code=204)
