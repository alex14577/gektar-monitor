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


class CspMiddleware:
    """Add Content-Security-Policy header to every HTTP response.

    Policy is defined at construction time and injected verbatim into the
    ``Content-Security-Policy`` response header.  Follows the same pure-ASGI
    class pattern as ``CsrfHostOriginMiddleware`` to avoid Starlette streaming
    issues (see Starlette #1012).

    Usage::

        app.add_middleware(CspMiddleware)
        # or with a custom policy:
        app.add_middleware(CspMiddleware, policy=MY_POLICY)
    """

    DEFAULT_POLICY: str = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: str = DEFAULT_POLICY,
    ) -> None:
        self._app = app
        self._policy_bytes: bytes = policy.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_csp(message: object) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((b"content-security-policy", self._policy_bytes))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_csp)


def loopback_csrf_config(*, port: int) -> tuple[frozenset[str], frozenset[str]]:
    """Return (host_allowlist, origin_whitelist) for loopback-only deployment.

    Single source of truth for the default values consumed by
    ``CsrfHostOriginMiddleware``. Useful for the lifespan hook
    (build_container's caller) to avoid hand-rolling these sets in every
    entrypoint.

    .. deprecated::
        Prefer ``csrf_config_for_bind(host="127.0.0.1", port=port)`` which
        supports non-loopback binds (e.g. ``0.0.0.0`` for WSL→Windows access).
        Kept for backwards compatibility; delegates to the new helper.
    """
    return csrf_config_for_bind(host="127.0.0.1", port=port)


def _get_local_ipv4s() -> list[str]:
    """Return non-loopback local IPv4 addresses via best-effort socket lookup.

    Returns an empty list on any failure — callers must tolerate absence.
    """
    import socket

    try:
        _, _, addr_list = socket.gethostbyname_ex(socket.gethostname())
        return [
            addr
            for addr in addr_list
            if addr and not addr.startswith("127.") and addr != "::1"
        ]
    except OSError:
        return []


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def csrf_config_for_bind(
    *,
    host: str,
    port: int,
    _local_ipv4s: list[str] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return (host_allowlist, origin_whitelist) for the given bind address.

    For loopback binds (``127.0.0.1``, ``localhost``, ``::1``) the result is
    identical to the original ``loopback_csrf_config``.

    For ``0.0.0.0`` the loopback set is extended with the machine's detected
    non-loopback IPv4 addresses plus ``0.0.0.0:<port>`` itself.

    For any other specific non-loopback IP exactly that address is added.

    The ``_local_ipv4s`` parameter is a DI seam for unit tests — pass a list
    of fake IPs to avoid real ``socket`` calls.  ``None`` means "detect live".

    Policy: ADR-011 + ADR-043 (non-loopback bind for WSL dev access).
    """
    loopback_hosts = frozenset(
        {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
    )
    loopback_origins = frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )

    if host in _LOOPBACK_HOSTS:
        return loopback_hosts, loopback_origins

    # Non-loopback bind: start from loopback set and extend with the explicit
    # bind address. Note: 0.0.0.0 here lands in the allowlist for completeness
    # only — browsers never send `Host: 0.0.0.0:<port>`, so this entry is inert
    # in practice; real Host headers will match the detected NIC IPs added below.
    extra_hosts: set[str] = {f"{host}:{port}"}
    extra_origins: set[str] = {f"http://{host}:{port}"}

    if host == "0.0.0.0":
        detected = _local_ipv4s if _local_ipv4s is not None else _get_local_ipv4s()
        for ip in detected:
            extra_hosts.add(f"{ip}:{port}")
            extra_origins.add(f"http://{ip}:{port}")

    return loopback_hosts | frozenset(extra_hosts), loopback_origins | frozenset(extra_origins)
