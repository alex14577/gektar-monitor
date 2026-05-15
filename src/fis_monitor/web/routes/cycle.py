"""FastAPI APIRouter for manual cycle-trigger endpoint.

Endpoints:
  POST /cycle/run — wake the scheduler for an immediate monitoring pass.

The button "Проверить сейчас" in base.html.jinja POSTs here.  The handler
translates the HTTP request into a ``MonitorCycleService.request_run_now()``
call and returns 202 Accepted.

Response content negotiation:
  - HTMX requests (``HX-Request: true`` header) → HTML fragment for
    ``hx-target="#cycle-result"`` swap.
  - Plain HTTP clients → JSON ``{"status": "queued"}``.

Rate limiting: 1 request per 10 seconds per client IP.  This prevents
accidental or deliberate rapid-fire clicks from hammering the scheduler.

DI: ``MonitorCycleService`` is injected via ``Depends(get_monitor_cycle)``.
    ``RateLimiter`` is a module-level singleton (application-scoped).

CSRF: POST endpoint passes through ``CsrfHostOriginMiddleware`` automatically —
no per-endpoint CSRF handling needed here.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from fis_monitor.services.monitor_cycle import MonitorCycleService
from fis_monitor.web._helpers import client_ip
from fis_monitor.web.deps import get_monitor_cycle
from fis_monitor.web.rate_limit import RateLimiter

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per 10 seconds per client IP.
# Module-level singleton: shared across all requests for the lifetime of the
# process.  Can be replaced in tests via app.dependency_overrides or by
# reassigning ``_cycle_rate_limiter`` before TestClient construction.
# ---------------------------------------------------------------------------

_cycle_rate_limiter = RateLimiter(max_requests=1, window_seconds=10.0)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/cycle", tags=["cycle"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


_HTML_OK = (
    '<span class="cycle-result cycle-result--ok">'
    "Запуск проверки запланирован"
    "</span>"
)

_HTML_RATE_LIMITED = (
    '<span class="cycle-result cycle-result--err">'
    "Повторите через 10 секунд"
    "</span>"
)


@router.post("/run", status_code=202, response_model=None)
def cycle_run(
    request: Request,
    svc: MonitorCycleService = Depends(get_monitor_cycle),
) -> JSONResponse | HTMLResponse:
    """Wake the scheduler for an immediate monitoring pass.

    Does NOT execute a cycle directly — instead signals the existing
    ``run_forever`` scheduler thread via an internal queue sentinel so the
    scheduler wakes early.  This preserves single-flight semantics: at most
    one cycle runs at a time (the scheduler is the only caller of ``run_cycle``).

    Content negotiation:
      HTMX (``HX-Request: true``) → HTML fragment for ``#cycle-result`` target.
      Plain HTTP                  → JSON ``{"status": "queued"}``.

    Returns:
        202 Accepted  — sentinel queued; scheduler will run next pass shortly.
        429 Too Many Requests — rate limit exceeded (1 req / 10 s per IP).
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    ip = client_ip(request)
    now = time.monotonic()
    if not _cycle_rate_limiter.acquire(ip, now=now):
        if is_htmx:
            return HTMLResponse(content=_HTML_RATE_LIMITED, status_code=429)
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again in 10 seconds",
        )

    svc.request_run_now()
    if is_htmx:
        return HTMLResponse(content=_HTML_OK, status_code=202)
    return JSONResponse(status_code=202, content={"status": "queued"})
