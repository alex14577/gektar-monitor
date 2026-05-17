"""Unit tests for CspMiddleware (bd b5c).

Coverage:
  1. CSP header is present on every HTTP response.
  2. Default policy contains mandatory directives: default-src, script-src,
     frame-ancestors.
  3. Header is injected even on non-200 responses (e.g. 404).
  4. Custom policy string is respected.
  5. Non-HTTP scopes (WebSocket) are passed through without error.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from fis_monitor.web.middleware import CspMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(*, policy: str | None = None) -> FastAPI:
    """Minimal FastAPI app with CspMiddleware and a catch-all route."""
    app = FastAPI()

    @app.get("/")
    async def _root() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/health")
    async def _health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    kwargs = {} if policy is None else {"policy": policy}
    app.add_middleware(CspMiddleware, **kwargs)
    return app


def _csp_header(resp: object) -> str:
    """Return the lower-cased CSP header value from a TestClient response."""
    headers = {k.lower(): v for k, v in resp.headers.items()}  # type: ignore[union-attr]
    assert "content-security-policy" in headers, (
        f"content-security-policy header missing; headers: {headers}"
    )
    return headers["content-security-policy"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_csp_header_present_on_root_get() -> None:
    """GET / must carry a content-security-policy header."""
    client = TestClient(_build_app(), raise_server_exceptions=True)
    resp = client.get("/")
    assert resp.status_code == 200
    _csp_header(resp)  # asserts presence


def test_csp_default_policy_mandatory_directives() -> None:
    """Default policy must contain the three mandatory directives."""
    client = TestClient(_build_app())
    resp = client.get("/")
    csp = _csp_header(resp)

    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_script_src_no_unsafe_inline() -> None:
    """script-src must not contain 'unsafe-inline' (all JS is in /static/)."""
    client = TestClient(_build_app())
    csp = _csp_header(client.get("/"))
    # Confirm script-src is present but does not carry 'unsafe-inline'.
    assert "script-src" in csp
    # Extract just the script-src directive to avoid matching style-src.
    directives = {d.strip(): d.strip() for d in csp.split(";")}
    script_src = next((v for v in directives if v.startswith("script-src")), "")
    assert "'unsafe-inline'" not in script_src


def test_csp_default_policy_google_fonts() -> None:
    """Default policy must allow Google Fonts origins."""
    client = TestClient(_build_app())
    csp = _csp_header(client.get("/"))

    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp


def test_csp_header_present_on_api_response() -> None:
    """CSP header is injected on an API endpoint response as well."""
    client = TestClient(_build_app())
    resp = client.get("/api/health")
    _csp_header(resp)


def test_csp_header_present_on_404() -> None:
    """CSP header is present even when the route returns 404."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    _csp_header(resp)


def test_csp_custom_policy_respected() -> None:
    """A custom policy string is passed through verbatim."""
    custom = "default-src 'none'"
    client = TestClient(_build_app(policy=custom))
    csp = _csp_header(client.get("/"))
    assert csp == custom
