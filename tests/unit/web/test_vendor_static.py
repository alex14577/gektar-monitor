"""Smoke tests for vendored htmx static assets (bd: gektar_monitor-mi8).

Verifies:
1. Vendor directory structure exists.
2. Both JS files are present and non-empty.
3. base.html.jinja no longer references unpkg.com for htmx.
4. base.html.jinja references the local vendor paths for both scripts.
"""
from __future__ import annotations

import re

from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR

_VENDOR_DIR = STATIC_DIR / "vendor" / "htmx-1.9.12"
_HTMX_JS = _VENDOR_DIR / "htmx.min.js"
_SSE_JS = _VENDOR_DIR / "ext" / "sse.js"
_BASE_TEMPLATE = TEMPLATES_DIR / "base.html.jinja"

# Minimum expected sizes (bytes) — guards against empty/truncated downloads.
_MIN_HTMX_BYTES = 40_000   # minified htmx core is ~48 KB
_MIN_SSE_BYTES = 5_000     # sse.js is ~10 KB


def test_vendor_directory_exists() -> None:
    assert _VENDOR_DIR.is_dir(), f"Vendor dir not found: {_VENDOR_DIR}"


def test_htmx_min_js_exists_and_is_nonempty() -> None:
    assert _HTMX_JS.is_file(), f"Missing vendor file: {_HTMX_JS}"
    size = _HTMX_JS.stat().st_size
    assert size >= _MIN_HTMX_BYTES, (
        f"htmx.min.js looks truncated: {size} bytes (expected >= {_MIN_HTMX_BYTES})"
    )


def test_sse_js_exists_and_is_nonempty() -> None:
    assert _SSE_JS.is_file(), f"Missing vendor file: {_SSE_JS}"
    size = _SSE_JS.stat().st_size
    assert size >= _MIN_SSE_BYTES, (
        f"sse.js looks truncated: {size} bytes (expected >= {_MIN_SSE_BYTES})"
    )


def test_base_template_does_not_load_htmx_from_unpkg() -> None:
    """base.html.jinja must not contain any unpkg.com reference for htmx."""
    content = _BASE_TEMPLATE.read_text(encoding="utf-8")
    assert "unpkg.com/htmx" not in content, (
        "base.html.jinja still references htmx from unpkg.com — "
        "supply-chain mitigation ADR-029 not applied"
    )


def test_base_template_loads_htmx_from_vendor() -> None:
    """base.html.jinja must use url_for() pointing at the local vendor path."""
    content = _BASE_TEMPLATE.read_text(encoding="utf-8")
    assert "/vendor/htmx-1.9.12/htmx.min.js" in content, (
        "base.html.jinja missing local vendor reference for htmx.min.js"
    )
    assert "/vendor/htmx-1.9.12/ext/sse.js" in content, (
        "base.html.jinja missing local vendor reference for ext/sse.js"
    )


def test_base_template_vendor_scripts_use_url_for() -> None:
    """Vendor script tags must use Jinja2 url_for(), not bare paths."""
    content = _BASE_TEMPLATE.read_text(encoding="utf-8")
    # Both vendor paths should appear inside url_for(...) calls.
    assert re.search(r"url_for\([^)]*vendor/htmx-1\.9\.12/htmx\.min\.js", content), (
        "htmx.min.js vendor reference should use url_for()"
    )
    assert re.search(r"url_for\([^)]*vendor/htmx-1\.9\.12/ext/sse\.js", content), (
        "ext/sse.js vendor reference should use url_for()"
    )
