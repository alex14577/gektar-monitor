"""Unit tests for CsrfHostOriginMiddleware and loopback_csrf_config.

Coverage:
 1. Safe methods (GET/HEAD/OPTIONS) pass through without Host/Origin.
 2. POST with valid Host + valid Origin → 200.
 3. POST with valid Host + invalid Origin → 421.
 4. POST with invalid Host → 421.
 5. POST without Origin header → 421.
 6. PUT/PATCH/DELETE behave identically to POST.
 7. Host matching is case-insensitive.
 8. Origin matching is case-insensitive.
 9. loopback_csrf_config(port=8000) returns correct frozensets.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.web.middleware import CsrfHostOriginMiddleware, loopback_csrf_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST_ALLOWLIST: frozenset[str] = frozenset({"localhost:8000", "127.0.0.1:8000"})
_ORIGIN_WHITELIST: frozenset[str] = frozenset(
    {"http://localhost:8000", "http://127.0.0.1:8000"}
)
_VALID_HOST = "localhost:8000"
_VALID_ORIGIN = "http://localhost:8000"


def _build_app() -> FastAPI:
    """Minimal FastAPI app with the CSRF middleware and a test route."""
    app = FastAPI()

    @app.get("/x")
    @app.post("/x")
    @app.put("/x")
    @app.patch("/x")
    @app.delete("/x")
    async def _handler() -> dict[str, str]:  # type: ignore[return]
        return {"ok": "true"}

    app.add_middleware(
        CsrfHostOriginMiddleware,
        host_allowlist=_HOST_ALLOWLIST,
        origin_whitelist=_ORIGIN_WHITELIST,
    )
    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# 1. Safe methods pass through without any Host / Origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_pass_through(client: TestClient, method: str) -> None:
    resp = client.request(method, "/x")
    # GET → 200, HEAD → 200 (no body), OPTIONS → 405 (method not in router but not 421)
    assert resp.status_code != 421


# ---------------------------------------------------------------------------
# 2. POST valid Host + valid Origin → 200
# ---------------------------------------------------------------------------


def test_post_valid_host_valid_origin(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": _VALID_HOST, "Origin": _VALID_ORIGIN},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. POST valid Host + invalid Origin → 421
# ---------------------------------------------------------------------------


def test_post_valid_host_invalid_origin(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": _VALID_HOST, "Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# 4. POST invalid Host → 421
# ---------------------------------------------------------------------------


def test_post_invalid_host(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": "attacker.example.com", "Origin": _VALID_ORIGIN},
    )
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# 5. POST without Origin → 421
# ---------------------------------------------------------------------------


def test_post_missing_origin(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": _VALID_HOST},
    )
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# 6. PUT / PATCH / DELETE behave like POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_mutating_methods_valid_passes(client: TestClient, method: str) -> None:
    resp = client.request(
        method,
        "/x",
        headers={"Host": _VALID_HOST, "Origin": _VALID_ORIGIN},
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_mutating_methods_invalid_host(client: TestClient, method: str) -> None:
    resp = client.request(
        method,
        "/x",
        headers={"Host": "attacker.example.com", "Origin": _VALID_ORIGIN},
    )
    assert resp.status_code == 421


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_mutating_methods_missing_origin(client: TestClient, method: str) -> None:
    resp = client.request(
        method,
        "/x",
        headers={"Host": _VALID_HOST},
    )
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# 7. Host matching is case-insensitive
# ---------------------------------------------------------------------------


def test_host_case_insensitive(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": "LOCALHOST:8000", "Origin": _VALID_ORIGIN},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 8. Origin matching is case-insensitive
# ---------------------------------------------------------------------------


def test_origin_case_insensitive(client: TestClient) -> None:
    resp = client.post(
        "/x",
        headers={"Host": _VALID_HOST, "Origin": "HTTP://LOCALHOST:8000"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 9. loopback_csrf_config returns correct sets
# ---------------------------------------------------------------------------


def test_loopback_csrf_config_contents() -> None:
    host_al, origin_wl = loopback_csrf_config(port=8000)

    assert isinstance(host_al, frozenset)
    assert isinstance(origin_wl, frozenset)

    assert "127.0.0.1:8000" in host_al
    assert "localhost:8000" in host_al
    assert "[::1]:8000" in host_al

    assert "http://127.0.0.1:8000" in origin_wl
    assert "http://localhost:8000" in origin_wl
    assert "http://[::1]:8000" in origin_wl


def test_loopback_csrf_config_different_port() -> None:
    host_al, origin_wl = loopback_csrf_config(port=9090)

    assert "127.0.0.1:9090" in host_al
    assert "http://localhost:9090" in origin_wl
