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
    The backfill runs in a daemon thread so the response is returned
    immediately (202 Accepted).

    Returns:
        202 Accepted  — backfill started.
        409 Conflict  — another backfill is already running.
    """
    if svc.is_running():
        raise HTTPException(
            status_code=409,
            detail="Backfill already running",
        )

    # Launch in a daemon thread with a never-set external stop event.
    # Shutdown is coordinated via BackfillService.cancel() from the lifespan.
    external_stop = threading.Event()

    def _run() -> None:
        svc.start(external_stop)

    t = threading.Thread(target=_run, daemon=True, name="backfill-worker")
    t.start()

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
        "running": snap.running,
        "current_region": snap.current_region,
        "current_page": snap.current_page,
        "lots_seen": snap.lots_seen,
        "regions_done": snap.regions_done,
        "regions_total": snap.regions_total,
        "started_at": snap.started_at,
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
