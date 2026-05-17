"""Unit tests for _derive_login_config (composition module).

Covers 5 invariants per the test-strategy Layer 3 plan:
1. prod base_url → prod allowed_hosts (includes gosuslugi.ru, not loopback).
2. prod base_url → login_start_url ends with /cabinet/ (one slash).
3. local 127.0.0.1 with port → loopback hosts only; port preserved in URL.
4. local localhost hostname → loopback hosts; no gosuslugi.ru.
5. base_url without trailing slash → no double slash before /cabinet/.
"""

from __future__ import annotations

from fis_monitor.composition import _derive_login_config

_PROD_BASE_URL = "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"


# ---------------------------------------------------------------------------
# Test 1: production base_url → production allowed hosts
# ---------------------------------------------------------------------------


def test_prod_base_url_returns_prod_hosts() -> None:
    """prod base_url → allowed_hosts contains torgi + gosuslugi chain, no loopback."""
    _, hosts = _derive_login_config(_PROD_BASE_URL)

    assert "xn--80aaggvgieoeoa2bo7l.xn--p1ai" in hosts
    assert ".gosuslugi.ru" in hosts
    assert "gosuslugi.ru" in hosts
    assert "надальнийвосток.рф" in hosts
    assert "127.0.0.1" not in hosts


# ---------------------------------------------------------------------------
# Test 2: production base_url → login_start_url has /cabinet/ suffix
# ---------------------------------------------------------------------------


def test_prod_base_url_login_start_url_has_cabinet_suffix() -> None:
    """prod base_url → login_start_url == '<base_url>/cabinet/' (one slash)."""
    login_url, _ = _derive_login_config(_PROD_BASE_URL)

    assert login_url == "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/"


# ---------------------------------------------------------------------------
# Test 3: local 127.0.0.1 with port → loopback hosts; port preserved in URL
# ---------------------------------------------------------------------------


def test_local_127_0_0_1_with_port() -> None:
    """_derive_login_config('http://127.0.0.1:8001') → loopback hosts only; port in URL."""
    login_url, hosts = _derive_login_config("http://127.0.0.1:8001")

    assert hosts == ("127.0.0.1", "localhost")
    assert ".gosuslugi.ru" not in hosts
    assert login_url == "http://127.0.0.1:8001/cabinet/"


# ---------------------------------------------------------------------------
# Test 4: local localhost hostname → loopback hosts; no gosuslugi
# ---------------------------------------------------------------------------


def test_local_localhost_hostname() -> None:
    """_derive_login_config('http://localhost:8001') → loopback only; no gosuslugi."""
    login_url, hosts = _derive_login_config("http://localhost:8001")

    assert "localhost" in hosts
    assert ".gosuslugi.ru" not in hosts
    assert login_url == "http://localhost:8001/cabinet/"


# ---------------------------------------------------------------------------
# Test 5: base_url without trailing slash → no double slash in login_start_url
# ---------------------------------------------------------------------------


def test_login_start_url_no_double_slash() -> None:
    """base_url without trailing slash → login_start_url has exactly one /cabinet/ segment."""
    # TargetConfig validator already strips trailing slash; confirm helper handles it.
    login_url, _ = _derive_login_config("https://example.com")

    assert login_url == "https://example.com/cabinet/"
    assert "//" not in login_url.replace("https://", "").replace("http://", "")
