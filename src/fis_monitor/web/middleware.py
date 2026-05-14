"""CSRF / DNS-rebinding protection middleware.

Implements the policy from ADR-011 (docs/decisions/ADR-011-dns-rebinding-host-allowlist.md):

- Safe methods (GET, HEAD, OPTIONS) pass through unchanged.
- State-changing methods (POST, PUT, PATCH, DELETE):
    1. Host header must be in the explicit allow-list → 421 on mismatch.
    2. Origin header must be present and in the explicit whitelist → 421 otherwise.

Using a pure ASGI class instead of Starlette's BaseHTTPMiddleware avoids known
streaming-response issues (see Starlette #1012).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_MISDIRECTED: bytes = b"421 Misdirected Request"


class CsrfHostOriginMiddleware:
    """Middleware enforcing Host allow-list + Origin whitelist on
    state-changing requests (POST/PUT/PATCH/DELETE).

    Safe methods (GET/HEAD/OPTIONS) pass through unchanged.

    On Host mismatch → 421 Misdirected Request.
    On Origin mismatch → 421 Misdirected Request.
    Missing Origin on state-changing request → 421 (strict).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        host_allowlist: frozenset[str],
        origin_whitelist: frozenset[str],
        safe_methods: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"}),
    ) -> None:
        self._app = app
        # Normalise to lowercase at construction time — O(1) per request.
        self._host_allowlist = frozenset(h.lower() for h in host_allowlist)
        self._origin_whitelist = frozenset(o.lower() for o in origin_whitelist)
        self._safe_methods = frozenset(m.upper() for m in safe_methods)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()
        if method in self._safe_methods:
            await self._app(scope, receive, send)
            return

        headers: dict[bytes, bytes] = dict(scope.get("headers", []))

        host_raw = headers.get(b"host", b"")
        if host_raw.decode("latin-1").lower() not in self._host_allowlist:
            await self._send_421(send)
            return

        origin_raw = headers.get(b"origin")
        if origin_raw is None:
            await self._send_421(send)
            return

        if origin_raw.decode("latin-1").lower() not in self._origin_whitelist:
            await self._send_421(send)
            return

        await self._app(scope, receive, send)

    @staticmethod
    async def _send_421(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 421,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(_MISDIRECTED)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _MISDIRECTED,
                "more_body": False,
            }
        )


def loopback_csrf_config(*, port: int) -> tuple[frozenset[str], frozenset[str]]:
    """Return (host_allowlist, origin_whitelist) for loopback-only deployment.

    Single source of truth for the default values consumed by
    ``CsrfHostOriginMiddleware``. Useful for the lifespan hook
    (build_container's caller) to avoid hand-rolling these sets in every
    entrypoint.
    """
    host_allowlist = frozenset(
        {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
    )
    origin_whitelist = frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )
    return host_allowlist, origin_whitelist
