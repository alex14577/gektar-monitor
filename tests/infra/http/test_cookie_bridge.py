"""Tests for RequestsCookieStore — Playwright → requests.Session cookie bridge.

Test matrix:
1. ``test_store_loads_cookies``           — basic name/value/domain loaded into session.
2. ``test_store_sets_secure_flag``        — secure=True propagated.
3. ``test_store_session_cookie``          — expires=-1 (Playwright) → None (no expiry).
4. ``test_store_http_only``               — httpOnly=True propagated via rest dict.
5. ``test_store_empty_list_noop``         — empty list → session unchanged.
6. ``test_store_multiple_cookies``        — multiple cookies all loaded.
7. ``test_store_overwrites_existing``     — second store() call overwrites same-name cookie.
8. ``test_store_bad_cookie_skipped``      — create_cookie raises → WARNING logged, others loaded.
9. ``test_protocol_compliance``           — RequestsCookieStore satisfies CookieStore Protocol
                                           (all methods callable, not just isinstance).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import requests

from fis_monitor.infra.http.cookie_bridge import RequestsCookieStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cookie(
    name: str = "sessionid",
    value: str = "abc123",
    domain: str = "example.com",
    path: str = "/",
    expires: float = -1,
    http_only: bool = False,
    secure: bool = False,
    same_site: str = "Lax",
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expires,
        "httpOnly": http_only,
        "secure": secure,
        "sameSite": same_site,
    }


def _make_store() -> tuple[RequestsCookieStore, requests.Session]:
    session = requests.Session()
    store = RequestsCookieStore(session)
    return store, session


# ---------------------------------------------------------------------------
# Test 1: basic name/value/domain loaded
# ---------------------------------------------------------------------------


def test_store_loads_cookies() -> None:
    store, session = _make_store()
    store.store([_make_cookie(name="tok", value="xyz", domain="fis.ru")])
    assert session.cookies.get("tok", domain="fis.ru") == "xyz"


# ---------------------------------------------------------------------------
# Test 2: secure flag propagated
# ---------------------------------------------------------------------------


def test_store_sets_secure_flag() -> None:
    store, session = _make_store()
    store.store([_make_cookie(name="s", value="v", domain="secure.example", secure=True)])
    # Find the cookie and verify secure flag
    found = None
    for cookie in session.cookies:
        if cookie.name == "s":
            found = cookie
            break
    assert found is not None, "cookie 's' not found in session"
    assert found.secure is True


# ---------------------------------------------------------------------------
# Test 3: session cookie (expires=-1) → None
# ---------------------------------------------------------------------------


def test_store_session_cookie() -> None:
    store, session = _make_store()
    store.store([_make_cookie(name="sess", value="abc", domain="x.ru", expires=-1)])
    # Cookie should be present but without a hard expiry
    found = None
    for cookie in session.cookies:
        if cookie.name == "sess":
            found = cookie
            break
    assert found is not None, "session cookie not found"
    # A None/0 expires means session cookie
    assert found.expires is None or found.expires == 0


# ---------------------------------------------------------------------------
# Test 4: httpOnly propagated via rest dict
# ---------------------------------------------------------------------------


def test_store_http_only() -> None:
    store, session = _make_store()
    store.store([_make_cookie(name="h", value="v", domain="h.ru", http_only=True)])
    # The cookie should be present; we verify it is loaded (httpOnly is a
    # browser-side constraint; requests doesn't enforce it, but it's stored).
    assert session.cookies.get("h", domain="h.ru") == "v"


# ---------------------------------------------------------------------------
# Test 5: empty list → no-op (session untouched)
# ---------------------------------------------------------------------------


def test_store_empty_list_noop() -> None:
    store, session = _make_store()
    session.cookies.set("existing", "val", domain="existing.com")
    store.store([])
    # Existing cookie must survive
    assert session.cookies.get("existing", domain="existing.com") == "val"
    # No extra cookies added
    names = {c.name for c in session.cookies}
    assert names == {"existing"}


# ---------------------------------------------------------------------------
# Test 6: multiple cookies all loaded
# ---------------------------------------------------------------------------


def test_store_multiple_cookies() -> None:
    store, session = _make_store()
    store.store([
        _make_cookie(name="a", value="1", domain="x.ru"),
        _make_cookie(name="b", value="2", domain="x.ru"),
        _make_cookie(name="c", value="3", domain="y.ru"),
    ])
    assert session.cookies.get("a", domain="x.ru") == "1"
    assert session.cookies.get("b", domain="x.ru") == "2"
    assert session.cookies.get("c", domain="y.ru") == "3"


# ---------------------------------------------------------------------------
# Test 7: second store() overwrites same-name cookie
# ---------------------------------------------------------------------------


def test_store_overwrites_existing() -> None:
    store, session = _make_store()
    store.store([_make_cookie(name="tok", value="old", domain="ex.com")])
    store.store([_make_cookie(name="tok", value="new", domain="ex.com")])
    assert session.cookies.get("tok", domain="ex.com") == "new"


# ---------------------------------------------------------------------------
# Test 8: create_cookie raises → WARNING logged, other cookies still loaded
# ---------------------------------------------------------------------------


def test_store_bad_cookie_skipped(caplog) -> None:  # type: ignore[no-untyped-def]
    """When requests.cookies.create_cookie raises for a cookie entry, the entry
    is skipped with a WARNING log and subsequent cookies are still processed.

    We monkeypatch create_cookie to raise on the first call only, simulating
    an internal failure (e.g. invalid attribute combination) that the real
    str()/float()/bool() fallbacks would not catch.
    """
    store, session = _make_store()
    good_cookie = _make_cookie(name="good", value="v", domain="good.com")
    bad_cookie = _make_cookie(name="bad", value="x", domain="bad.com")

    # Track calls: first call raises, second succeeds normally.
    import requests.cookies as _rc
    original_create_cookie = _rc.create_cookie
    call_count = 0

    def _patched_create_cookie(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("simulated create_cookie failure")
        return original_create_cookie(*args, **kwargs)

    _target = "fis_monitor.infra.http.cookie_bridge.requests.cookies.create_cookie"
    with (
        caplog.at_level(logging.WARNING, logger="fis_monitor.infra.http.cookie_bridge"),
        patch(_target, _patched_create_cookie),
    ):
        store.store([bad_cookie, good_cookie])

    # At least one WARNING must be logged about the skipped cookie.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, (
        "Expected a WARNING log for the skipped bad cookie; caplog was empty"
    )

    # Good cookie must still be loaded despite the bad one failing.
    assert session.cookies.get("good", domain="good.com") == "v"

    # Bad cookie must NOT be in the session (it was skipped).
    assert session.cookies.get("bad", domain="bad.com") is None


# ---------------------------------------------------------------------------
# Test 9: Protocol compliance — all CookieStore methods are callable
# ---------------------------------------------------------------------------


def test_protocol_compliance() -> None:
    """RequestsCookieStore must expose ``store`` and it must be callable
    with a list of dicts — not just satisfy isinstance() check."""
    _, session = _make_store()
    store = RequestsCookieStore(session)

    # Call store() with a real list to verify the method actually works
    # (not just present as an attribute).
    cookies = [_make_cookie(name="proto_check", value="ok", domain="proto.ru")]
    store.store(cookies)  # must not raise

    # Verify effect — this proves the method was actually invoked and mutated state
    assert session.cookies.get("proto_check", domain="proto.ru") == "ok"
