"""Shared web-layer helpers.

Internal to ``fis_monitor.web`` — not part of the public package API.
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
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
