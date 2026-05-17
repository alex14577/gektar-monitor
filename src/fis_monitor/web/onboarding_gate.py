"""Onboarding-gate middleware (ADR-018).

Enforces the server-side onboarding FSM: any non-whitelisted URL is
redirected to the current onboarding step until the FSM reaches COMPLETED.

Design choices:
- Pure ASGI class (mirrors CsrfHostOriginMiddleware) — avoids Starlette
  BaseHTTPMiddleware streaming-response issues.
- OnboardingService is injected via constructor (DI) — no Container reference.
- Whitelist is checked first; whitelisted paths and OPTIONS pass through
  without touching the DB.
- Target URL always derived from server state (OnboardingService.url_for_current_step()),
  never from query-params — enforces ADR-018 acceptance #19.
- Cache-Control: no-store on redirect to prevent browsers caching across
  state-changes.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Protocol — narrow seam for testing / future substitution
# ---------------------------------------------------------------------------


class OnboardingQuery(Protocol):
    """Minimal read-only interface used by the gate middleware."""

    def current(self) -> object:  # returns OnboardingState
        ...

    def url_for_current_step(self) -> str:
        ...


# ---------------------------------------------------------------------------
# Sentinel for the completed state value (avoids importing OnboardingState
# into the middleware, keeping coupling low).
# ---------------------------------------------------------------------------
_COMPLETED_VALUE = "completed"

# Prefixes that are always passed through, regardless of onboarding state.
_WHITELIST_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/events",
    "/api/health",
    "/onboarding/",
    "/auth/",
    "/settings/smtp/suggest",
)

_REDIRECT_BODY = b""
_CACHE_NO_STORE = b"no-store"


class OnboardingGateMiddleware:
    """ASGI middleware that gates non-whitelisted routes behind onboarding completion.

    Routes matching ``_WHITELIST_PREFIXES`` (or ``OPTIONS`` requests) pass
    through unchanged. All other routes are redirected (302) to the current
    onboarding step as long as ``OnboardingService.current()`` is not
    ``COMPLETED``.

    ``svc`` is injected — must implement ``OnboardingQuery``
    (i.e. ``current()`` and ``url_for_current_step()``).
    """

    _CACHE_TTL = 1.0  # seconds

    def __init__(self, app: ASGIApp, *, svc: OnboardingQuery) -> None:
        self._app = app
        self._svc = svc
        self._cache: tuple[object, str, float] | None = None  # (state, url, expires_at)

    async def _get_state_and_url(self) -> tuple[object, str]:
        """Return (state, url_for_current_step) — both offloaded via asyncio.to_thread.

        Race note: cache miss is intentionally lock-free — concurrent misses
        issue parallel to_thread reads and last-writer-wins on ``_cache``.
        Values are idempotent (same FSM state), so the race is harmless;
        the extra DB hits are bounded by the TTL window. Trade simplicity
        over a lock that would serialize all cold-path requests.
        """
        now = time.monotonic()
        if self._cache is not None and now < self._cache[2]:
            return self._cache[0], self._cache[1]

        def _read() -> tuple[object, str]:
            return self._svc.current(), self._svc.url_for_current_step()

        state, url = await asyncio.to_thread(_read)
        self._cache = (state, url, now + self._CACHE_TTL)
        return state, url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()

        # OPTIONS passes through (CORS preflight — never gated).
        if method == "OPTIONS":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "/")

        # Whitelisted prefixes — pass through without reading the DB.
        # Bare "/onboarding" (no trailing slash) is also whitelisted so the
        # router's bookmark-friendly entry route is reachable in production;
        # otherwise the middleware would intercept it before the router could
        # render its own server-derived redirect (M1 fix from 53e review).
        if path == "/onboarding" or any(
            path.startswith(prefix) for prefix in _WHITELIST_PREFIXES
        ):
            await self._app(scope, receive, send)
            return

        # Read onboarding state + redirect URL (TTL-cached; I/O offloaded to thread-pool).
        state, redirect_url = await self._get_state_and_url()
        if str(state) != _COMPLETED_VALUE:
            await self._send_redirect(send, redirect_url)
            return

        await self._app(scope, receive, send)

    @staticmethod
    async def _send_redirect(send: Send, location: str) -> None:
        location_bytes = location.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"location", location_bytes),
                    (b"cache-control", _CACHE_NO_STORE),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _REDIRECT_BODY,
                "more_body": False,
            }
        )
