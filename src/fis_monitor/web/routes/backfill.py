"""FastAPI APIRouter for backfill endpoints.

Endpoints:
  POST /backfill/start   — single-flight start; 202 if started, 409 if running.
  GET  /backfill/status  — progress snapshot (running, lots_seen, …).
  POST /backfill/cancel  — cancel any running backfill; 204 always.

DI: ``BackfillService`` is injected via ``Depends(get_backfill)``.

CSRF: POST endpoints pass through ``CsrfHostOriginMiddleware`` automatically.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from fis_monitor.services.backfill import BackfillService, BackfillStatus
from fis_monitor.web.deps import get_backfill

__all__ = ["router"]

router = APIRouter(prefix="/backfill", tags=["backfill"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start", status_code=202)
def backfill_start(
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
    """
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
        "lots_seen": snap.lots_seen,
        "regions_done": snap.regions_done,
        "regions_total": snap.regions_total,
        "total_pages_seen": snap.total_pages_seen,
        "started_at": snap.started_at,
        "updated_at": snap.updated_at,
    }


@router.post("/cancel", status_code=204)
def backfill_cancel(
    svc: BackfillService = Depends(get_backfill),
) -> Response:
    """Cancel any running backfill.

    Idempotent — safe to call when no backfill is active.

    Returns:
        204 No Content — always.
    """
    svc.cancel()
    return Response(status_code=204)
