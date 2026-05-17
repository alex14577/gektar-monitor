"""FastAPI APIRouter for authentication endpoints.

Endpoints:
  POST /auth/start   — start a headed-login job (single-flight, rate-limited).
  POST /auth/refresh — silent cookie refresh (headless, no visible window).
  GET  /auth/status  — return current login status.
  POST /auth/cancel  — cancel the active login job (idempotent).

Rate limiting:
  /auth/start   — 1 request per 60 seconds per client IP.
  /auth/refresh — 1 request per 60 seconds per client IP (independent quota).
Single-flight: a second POST /auth/start or /auth/refresh while a job is
  running → 409 Conflict.

DI: ``LoginService`` is injected via ``Depends(get_login)``.
    ``RateLimiter`` singletons are module-level (application-scoped).

CSRF: POST endpoints pass through ``CsrfHostOriginMiddleware`` automatically —
no per-endpoint CSRF handling needed here.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fis_monitor.domain.errors import BrowserUnavailableError
from fis_monitor.services.login import LoginBusyError, LoginService, LoginStatus
from fis_monitor.web._helpers import client_ip
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

# Separate rate-limiter for /auth/refresh — 1 request per 60 seconds per IP.
# Kept as a distinct object so /auth/start and /auth/refresh consume independent
# quotas (a forced-refresh attempt does not block a legitimate manual login).
_refresh_rate_limiter = RateLimiter(max_requests=1, window_seconds=60.0)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


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
    # bd 2hi2: cheap availability probes BEFORE rate-limit acquire — 503-class
    # failures (no Chromium, no executor) must not consume a rate-limit slot,
    # otherwise an operator who fixes the environment and immediately retries
    # gets 429 instead of the success they expect.
    if not svc.is_browser_available():
        raise HTTPException(
            status_code=503,
            detail="Playwright browser is not installed on the server — "
                   "run `playwright install chromium` to enable login.",
        )
    if not svc.is_executor_bound():
        raise HTTPException(
            status_code=503,
            detail="Login service not initialized — startup not yet complete",
        )

    ip = client_ip(request)
    now = time.monotonic()
    if not _auth_rate_limiter.acquire(ip, now=now):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again in 60 seconds",
        )

    try:
        svc.start_login()
    except BrowserUnavailableError as exc:
        # Race: probe passed, then mark_browser_unavailable() fired concurrently.
        # Translate to 503 but the slot is already consumed — acceptable since
        # this is a TOCTOU edge, not the operator-error path 2hi2 fixes.
        raise HTTPException(
            status_code=503,
            detail="Playwright browser is not installed on the server — "
                   "run `playwright install chromium` to enable login.",
        ) from exc
    except LoginBusyError as exc:
        raise HTTPException(status_code=409, detail="Login already in progress") from exc
    except RuntimeError as exc:
        # Same TOCTOU edge for executor unbinding — rare.
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


@router.post("/refresh", status_code=202)
def auth_refresh(
    request: Request,
    svc: LoginService = Depends(get_login),
) -> JSONResponse:
    """Start a silent-refresh job (no visible browser window).

    Called from the UI when the session is expiring soon (``expires_soon``
    banner).  Navigates to /cabinet/ headlessly using the persistent-context
    profile; if ЕСИА cookies are still valid the server lands on /cabinet/
    and new session cookies are persisted automatically.

    Clients should poll ``GET /auth/status`` to learn the outcome.  A
    ``last_outcome.error == "needs_manual_login"`` means the user must
    trigger ``POST /auth/start`` for a full headed login.

    Returns:
        202 Accepted  — silent-refresh job started.
        409 Conflict  — a login or refresh job is already running.
        429 Too Many Requests — rate limit exceeded (1 req / 60 s per IP).
        503 Service Unavailable — executor not bound (startup incomplete).
    """
    # bd 2hi2: availability probes precede rate-limit so 503 doesn't burn a slot.
    if not svc.is_browser_available():
        raise HTTPException(
            status_code=503,
            detail="Playwright browser is not installed on the server — "
                   "run `playwright install chromium` to enable login.",
        )
    if not svc.is_executor_bound():
        raise HTTPException(
            status_code=503,
            detail="Login service not initialized — startup not yet complete",
        )

    ip = client_ip(request)
    now = time.monotonic()
    if not _refresh_rate_limiter.acquire(ip, now=now):
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again in 60 seconds",
        )

    try:
        svc.start_refresh()
    except BrowserUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Playwright browser is not installed on the server — "
                   "run `playwright install chromium` to enable login.",
        ) from exc
    except LoginBusyError as exc:
        raise HTTPException(status_code=409, detail="Login already in progress") from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Login service not initialized — startup not yet complete",
        ) from exc

    return JSONResponse(
        status_code=202,
        content={"status": "refreshing"},
    )


@router.post("/cancel")
def auth_cancel(
    svc: LoginService = Depends(get_login),
) -> Response:
    """Cancel the active login job.

    Idempotent: returns 204 даже если задача не активна.
    Возвращаем явный Response(status_code=204) без content-type заголовка —
    RFC 9110 запрещает body на 204 (Evidence Collector BUG-2).
    """
    svc.cancel_active_job()
    return Response(status_code=204)
