"""FastAPI APIRouter for authentication endpoints.

Endpoints:
  POST /auth/start  — start a headed-login job (single-flight, rate-limited).
  GET  /auth/status — return current login status.
  POST /auth/cancel — cancel the active login job (idempotent).

Rate limiting: /auth/start is limited to 1 request per 60 seconds per client IP.
Single-flight: a second POST /auth/start while a job is running → 409 Conflict.

DI: ``LoginService`` is injected via ``Depends(get_login)``.
    ``RateLimiter`` is provided as a module-level singleton (application-scoped).

CSRF: POST endpoints pass through ``CsrfHostOriginMiddleware`` automatically —
no per-endpoint CSRF handling needed here.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from fis_monitor.services.login import LoginBusyError, LoginService, LoginStatus
from fis_monitor.web.deps import get_login
from fis_monitor.web.rate_limit import RateLimiter

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per 60 seconds per client IP.
# Module-level singleton: shared across all requests for the lifetime of the
# process.  Can be replaced in tests via app.dependency_overrides or by
# reassigning ``_auth_rate_limiter`` before TestClient construction.
# ---------------------------------------------------------------------------

_auth_rate_limiter = RateLimiter(max_requests=1, window_seconds=60.0)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Extract the client IP from the request.

    Falls back to ``"unknown"`` if the ASGI scope does not carry client info
    (e.g., in unit tests using ``TestClient`` without an explicit client).

    Note: multiple clients that lack ``request.client`` will share the same
    ``"unknown"`` rate-limit bucket — they will compete for the same quota.

    Loopback-only deployment per ADR-011. If a reverse-proxy is introduced,
    switch to X-Forwarded-For with trust-gating (do NOT enable blindly — it is
    a security pitfall without explicit trusted-proxy configuration).
    """
    if request.client is not None:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start", status_code=202)
def auth_start(
    request: Request,
    svc: LoginService = Depends(get_login),
) -> JSONResponse:
    """Start a headed-login job.

    Returns:
        202 Accepted  — job started successfully.
        409 Conflict  — a login job is already running.
        429 Too Many Requests — rate limit exceeded (1 req / 60 s per IP).
        503 Service Unavailable — executor not bound (startup incomplete).
    """
    ip = _client_ip(request)
    now = time.monotonic()
    if not _auth_rate_limiter.acquire(ip, now=now):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again in 60 seconds",
        )

    try:
        svc.start_login()
    except LoginBusyError as exc:
        raise HTTPException(status_code=409, detail="Login already in progress") from exc
    except RuntimeError as exc:
        # Executor not yet bound — lifespan phase 1.5 not completed (tracked: gektar_monitor-j19).
        # Return 503 instead of 500 so clients know to retry after startup completes.
        raise HTTPException(
            status_code=503,
            detail="Login service not initialized — startup not yet complete",
        ) from exc

    return JSONResponse(
        status_code=202,
        content={"status": "started"},
    )


@router.get("/status")
def auth_status(
    svc: LoginService = Depends(get_login),
) -> JSONResponse:
    """Return the current login status.

    Returns:
        200 OK with JSON body ``{"running": bool, "last_outcome": ...}``.
    """
    status: LoginStatus = svc.status()
    last = status.last_outcome
    return JSONResponse(
        content={
            "running": status.running,
            "last_outcome": (
                {
                    "success": last.success,
                    "cookies_updated": last.cookies_updated,
                    # ``error`` is a controlled enum value from LoginOutcome
                    # (e.g. "playwright_other"), NOT a raw exception message.
                    # Do NOT expose raw exceptions here — keep this mapping explicit.
                    "error": last.error,
                }
                if last is not None
                else None
            ),
        }
    )


@router.post("/cancel", status_code=204)
def auth_cancel(
    svc: LoginService = Depends(get_login),
) -> None:
    """Cancel the active login job.

    Idempotent: returns 204 even if no job is running.
    """
    svc.cancel_active_job()
