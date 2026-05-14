"""Unit tests for PlaywrightLoginSession.

All tests use a mock ``playwright_factory`` — no real browser is started.

Test matrix:
1. ``test_open_returns_outcome``          — successful login returns LoginOutcome(success=True).
2. ``test_single_flight_busy_error``      — second concurrent call raises BusyError.
3. ``test_cancel_releases_active``        — cancel() closes the active browser.
4. ``test_cancel_no_active_noop``         — cancel() with no active login is a no-op.
5. ``test_host_whitelist_route_registered`` — route handler aborts non-whitelisted hosts.
6. ``test_deadline_timeout``              — elapsed > deadline returns error="timeout".
7. ``test_headless_false``                — launch_persistent_context called with headless=False.
8. ``test_di_playwright_factory``         — custom playwright_factory is used (DI).
9. ``test_map_exception_missing_binary``  — _map_exception maps missing-binary message to correct hint.
10. ``test_map_exception_missing_deps``   — _map_exception maps missing-deps message to correct hint.
11. ``test_map_exception_unmapped_logs_error`` — unmapped exception triggers ERROR log.
12. ``test_initial_goto_called``           — page.goto() is invoked with the login start URL.
13. ``test_initial_goto_failure_mapped``   — goto() exceptions are mapped to LoginOutcome.error.
14. ``test_host_whitelist_suffix_match``   — entries starting with "." match subdomains.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fis_monitor.domain.errors import BusyError
from fis_monitor.domain.models import LoginOutcome
from fis_monitor.infra.playwright.login import PlaywrightLoginSession

# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable monotonic clock for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._mono = start

    def now(self):  # type: ignore[override]
        from datetime import UTC, datetime
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._mono += seconds


def _make_page_mock(*, wait_for_url_side_effect=None) -> MagicMock:
    page = MagicMock()
    if wait_for_url_side_effect is not None:
        page.wait_for_url.side_effect = wait_for_url_side_effect
    return page


def _make_context_mock(page: MagicMock) -> MagicMock:
    """Build a BrowserContext mock that has ``pages`` and ``new_page``."""
    context = MagicMock()
    context.pages = [page]
    context.new_page.return_value = page
    return context


def _make_pw_factory(context_mock: MagicMock) -> MagicMock:
    """Build a playwright_factory that returns ``context_mock`` from
    ``pw.chromium.launch_persistent_context``."""
    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = context_mock
    # Support context-manager protocol on the factory result.
    cm = MagicMock()
    cm.__enter__.return_value = pw
    cm.__exit__.return_value = False
    factory = MagicMock(return_value=cm)
    return factory


def _make_session(
    *,
    clock: FakeClock | None = None,
    allowed_hosts: list[str] | None = None,
    playwright_factory=None,
    profile_dir: Path | None = None,
) -> PlaywrightLoginSession:
    return PlaywrightLoginSession(
        profile_dir=profile_dir or Path("/tmp/profile"),
        allowed_hosts=allowed_hosts or ["fis.gosuslugi.ru"],
        clock=clock or FakeClock(),
        playwright_factory=playwright_factory or MagicMock(),
    )


# ---------------------------------------------------------------------------
# Test 1: successful login returns LoginOutcome(success=True)
# ---------------------------------------------------------------------------


def test_open_returns_outcome(tmp_path: Path) -> None:
    page = _make_page_mock()  # wait_for_url returns normally → success
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)
    clock = FakeClock(start=0.0)

    session = _make_session(clock=clock, playwright_factory=factory, profile_dir=tmp_path)
    outcome = session.open_headed_login(deadline=60.0)

    assert isinstance(outcome, LoginOutcome)
    assert outcome.success is True
    assert outcome.cookies_updated is True
    assert outcome.error is None


# ---------------------------------------------------------------------------
# Test 2: single-flight — second concurrent call raises BusyError
# ---------------------------------------------------------------------------


def test_single_flight_busy_error(tmp_path: Path) -> None:
    """First call blocks on wait_for_url; second call must raise BusyError."""
    ready = threading.Event()
    unblock = threading.Event()

    def _blocking_wait_for_url(*args, **kwargs):
        ready.set()       # signal that the first thread is inside wait_for_url
        unblock.wait(timeout=5.0)  # wait until test tells us to proceed

    page = _make_page_mock(wait_for_url_side_effect=_blocking_wait_for_url)
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)
    clock = FakeClock(start=0.0)

    session = _make_session(clock=clock, playwright_factory=factory, profile_dir=tmp_path)

    # Run first call in a background thread.
    first_result: list[LoginOutcome] = []
    first_exc: list[BaseException] = []

    def _first():
        try:
            first_result.append(session.open_headed_login(deadline=60.0))
        except Exception as exc:
            first_exc.append(exc)

    t = threading.Thread(target=_first, daemon=True)
    t.start()

    # Wait until the first thread is inside wait_for_url.
    assert ready.wait(timeout=5.0), "first thread did not reach wait_for_url"

    # Second call must raise BusyError immediately.
    with pytest.raises(BusyError):
        session.open_headed_login(deadline=60.0)

    # Unblock the first thread so it can finish.
    unblock.set()
    t.join(timeout=5.0)
    assert not t.is_alive(), "first thread did not complete"
    assert not first_exc, f"first thread raised: {first_exc}"


# ---------------------------------------------------------------------------
# Test 3: cancel() closes the active browser
# ---------------------------------------------------------------------------


def test_cancel_releases_active(tmp_path: Path) -> None:
    """cancel() must call browser.close() on the active browser."""
    ready = threading.Event()
    unblock = threading.Event()

    def _blocking_wait(_url, *, timeout):
        ready.set()
        unblock.wait(timeout=5.0)
        # Simulate TargetClosedError from browser.close()
        raise Exception("Target page, context or browser has been closed (TargetClosedError)")

    page = _make_page_mock(wait_for_url_side_effect=_blocking_wait)
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)
    clock = FakeClock(start=0.0)

    session = _make_session(clock=clock, playwright_factory=factory, profile_dir=tmp_path)

    outcomes: list[LoginOutcome] = []

    def _run():
        outcomes.append(session.open_headed_login(deadline=60.0))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert ready.wait(timeout=5.0), "worker thread did not start"

    # Now cancel() — must close the active browser and unblock the worker.
    # We call unblock first to let wait_for_url raise the target-closed exception.
    unblock.set()
    session.cancel()  # idempotent — browser may already be closed

    t.join(timeout=5.0)
    assert not t.is_alive()
    assert len(outcomes) == 1
    assert outcomes[0].success is False
    assert outcomes[0].error == "cancelled"


# ---------------------------------------------------------------------------
# Test 4: cancel() with no active login is a no-op
# ---------------------------------------------------------------------------


def test_cancel_no_active_noop() -> None:
    """cancel() when no login is in progress must not raise."""
    session = _make_session()
    session.cancel()  # must not raise


# ---------------------------------------------------------------------------
# Test 5: host-whitelist route handler aborts non-whitelisted hosts
# ---------------------------------------------------------------------------


def test_host_whitelist_route_registered(tmp_path: Path) -> None:
    """Route handler must abort non-whitelisted hosts and continue whitelisted ones."""
    page = _make_page_mock()
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)
    clock = FakeClock()

    session = _make_session(
        clock=clock,
        playwright_factory=factory,
        allowed_hosts=["fis.gosuslugi.ru"],
        profile_dir=tmp_path,
    )
    session.open_headed_login(deadline=60.0)

    # context.route must have been called exactly once with ("**/*", handler)
    assert context.route.call_count == 1
    route_glob, handler = context.route.call_args[0]
    assert route_glob == "**/*"

    # --- Test whitelisted host: continue() ---
    allowed_route = MagicMock()
    allowed_request = MagicMock()
    allowed_request.url = "https://fis.gosuslugi.ru/cabinet/profile"
    allowed_route.request = allowed_request
    handler(allowed_route)
    allowed_route.continue_.assert_called_once()
    allowed_route.abort.assert_not_called()

    # --- Test non-whitelisted host: abort() ---
    blocked_route = MagicMock()
    blocked_request = MagicMock()
    blocked_request.url = "https://tracker.evil.example.com/pixel.gif"
    blocked_route.request = blocked_request
    handler(blocked_route)
    blocked_route.abort.assert_called_once()
    blocked_route.continue_.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: deadline timeout — returns error="timeout"
# ---------------------------------------------------------------------------


def test_deadline_timeout(tmp_path: Path) -> None:
    """When deadline <= 0 ms remain, open_headed_login returns error='timeout'."""
    # Use a clock that starts at 1000s; deadline is 0.5s → remaining = 0 ms after overhead.
    clock = FakeClock(start=1000.0)

    # Patch monotonic to advance 5s between setup calls so remaining_ms → 0.
    call_count = 0

    def _advancing_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        return 1000.0 + call_count * 10.0  # each call advances 10s

    clock.monotonic = _advancing_monotonic  # type: ignore[method-assign]

    page = _make_page_mock()
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)

    session = _make_session(clock=clock, playwright_factory=factory, profile_dir=tmp_path)
    outcome = session.open_headed_login(deadline=0.001)  # effectively 0 remaining

    assert outcome.success is False
    assert outcome.error == "timeout"


# ---------------------------------------------------------------------------
# Test 7: launch_persistent_context called with headless=False
# ---------------------------------------------------------------------------


def test_headless_false(tmp_path: Path) -> None:
    """Playwright must be launched with headless=False (headed mode)."""
    page = _make_page_mock()
    context = _make_context_mock(page)

    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = context
    cm = MagicMock()
    cm.__enter__.return_value = pw
    cm.__exit__.return_value = False
    factory = MagicMock(return_value=cm)

    session = _make_session(playwright_factory=factory, profile_dir=tmp_path)
    session.open_headed_login(deadline=60.0)

    pw.chromium.launch_persistent_context.assert_called_once()
    _, kwargs = pw.chromium.launch_persistent_context.call_args
    assert kwargs.get("headless") is False, (
        f"Expected headless=False, got headless={kwargs.get('headless')!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: DI — custom playwright_factory is actually used
# ---------------------------------------------------------------------------


def test_di_playwright_factory(tmp_path: Path) -> None:
    """PlaywrightLoginSession must use the injected factory, not the default one."""
    page = _make_page_mock()
    context = _make_context_mock(page)
    custom_factory = _make_pw_factory(context)

    session = PlaywrightLoginSession(
        profile_dir=tmp_path,
        allowed_hosts=["example.com"],
        clock=FakeClock(),
        playwright_factory=custom_factory,
    )
    session.open_headed_login(deadline=60.0)

    assert custom_factory.called, "custom playwright_factory was not called"


# ---------------------------------------------------------------------------
# Test 9: _map_exception → playwright_missing_binary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "BrowserType.launch_persistent_context: Executable doesn't exist at /home/alex/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome",
        "Looks like Playwright was just installed... Please run: playwright install",
    ],
)
def test_map_exception_missing_binary(message: str) -> None:
    """Exceptions mentioning a missing executable map to playwright_missing_binary."""
    exc = RuntimeError(message)
    outcome, unmapped = PlaywrightLoginSession._map_exception(exc)
    assert outcome.error == "playwright_missing_binary"
    assert outcome.success is False
    assert unmapped is False


# ---------------------------------------------------------------------------
# Test 10: _map_exception → playwright_missing_deps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Host system is missing dependencies",
        "error while loading shared libraries: missing libnss3.so",
        "missing libatk-1.0.so.0 required by chromium",
        "could not load libgtk-3.so.0: missing",
    ],
)
def test_map_exception_missing_deps(message: str) -> None:
    """Exceptions mentioning missing system libraries map to playwright_missing_deps."""
    exc = RuntimeError(message)
    outcome, unmapped = PlaywrightLoginSession._map_exception(exc)
    assert outcome.error == "playwright_missing_deps"
    assert outcome.success is False
    assert unmapped is False


# ---------------------------------------------------------------------------
# Test 11: unmapped exception → ERROR log (via open_headed_login integration)
# ---------------------------------------------------------------------------


def test_map_exception_unmapped_logs_error(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """An exception that matches no known pattern must be logged at ERROR level."""
    import logging

    strange_exc = ValueError("something completely unknown and unrelated")

    def _raising_wait_for_url(*args, **kwargs):
        raise strange_exc

    page = _make_page_mock(wait_for_url_side_effect=_raising_wait_for_url)
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)

    session = _make_session(playwright_factory=factory, profile_dir=tmp_path)

    with caplog.at_level(logging.ERROR, logger="fis_monitor.infra.playwright.login"):
        outcome = session.open_headed_login(deadline=60.0)

    assert outcome.success is False
    assert outcome.error == "playwright_other"
    assert any(
        record.levelno == logging.ERROR
        for record in caplog.records
    ), "Expected at least one ERROR log record for unmapped exception"


# ---------------------------------------------------------------------------
# Test 12: page.goto() invoked with the login start URL
# ---------------------------------------------------------------------------


def test_initial_goto_called(tmp_path: Path) -> None:
    """page.goto() must be called with the гектар /cabinet/ URL before wait_for_url."""
    from fis_monitor.infra.playwright.login import _LOGIN_START_URL

    page = _make_page_mock()
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)

    session = _make_session(playwright_factory=factory, profile_dir=tmp_path)
    outcome = session.open_headed_login(deadline=60.0)

    assert outcome.success is True
    page.goto.assert_called_once()
    args, kwargs = page.goto.call_args
    # URL is the first positional arg.
    assert args[0] == _LOGIN_START_URL
    # We want wait_until="domcontentloaded" — not "load" (slow third-party
    # blocked-by-route assets) and not "networkidle" (never settles).
    assert kwargs.get("wait_until") == "domcontentloaded"
    assert isinstance(kwargs.get("timeout"), int)
    assert kwargs["timeout"] > 0
    # goto must happen BEFORE wait_for_url — Playwright's MagicMock preserves
    # call ordering on the parent page mock.
    method_calls = [c[0] for c in page.method_calls]
    assert method_calls.index("goto") < method_calls.index("wait_for_url")


# ---------------------------------------------------------------------------
# Test 13: goto() failure is mapped to LoginOutcome.error (failure-fast)
# ---------------------------------------------------------------------------


def test_initial_goto_failure_mapped(tmp_path: Path) -> None:
    """If page.goto() raises (DNS, network, etc.), return a structured LoginOutcome."""
    page = _make_page_mock()
    page.goto.side_effect = RuntimeError(
        "net::ERR_NAME_NOT_RESOLVED at https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/"
    )
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)

    session = _make_session(playwright_factory=factory, profile_dir=tmp_path)
    outcome = session.open_headed_login(deadline=60.0)

    assert outcome.success is False
    # Generic RuntimeError without "playwright"/"timeout" markers falls into
    # the unmapped bucket → playwright_other. wait_for_url must NOT be reached.
    assert outcome.error == "playwright_other"
    page.wait_for_url.assert_not_called()


# ---------------------------------------------------------------------------
# Test 14: host-whitelist suffix-match — ".gosuslugi.ru" matches subdomains
# ---------------------------------------------------------------------------


def test_host_whitelist_suffix_match(tmp_path: Path) -> None:
    """Entries beginning with '.' must match any matching-suffix subdomain;
    bare apex must NOT match subdomains (no accidental policy widening)."""
    page = _make_page_mock()
    context = _make_context_mock(page)
    factory = _make_pw_factory(context)

    session = _make_session(
        playwright_factory=factory,
        allowed_hosts=[
            "xn--80aaggvgieoeoa2bo7l.xn--p1ai",
            ".gosuslugi.ru",  # suffix-match
            "example.org",  # exact-only (NOT a suffix entry)
        ],
        profile_dir=tmp_path,
    )
    session.open_headed_login(deadline=60.0)
    _, handler = context.route.call_args[0]

    def _probe(url: str) -> str:
        r = MagicMock()
        r.request = MagicMock()
        r.request.url = url
        handler(r)
        if r.continue_.called:
            return "continue"
        if r.abort.called:
            return "abort"
        return "noop"

    # Suffix-match: subdomains of gosuslugi.ru pass.
    assert _probe("https://esia.gosuslugi.ru/login/") == "continue"
    assert _probe("https://id.gosuslugi.ru/oauth2") == "continue"
    assert _probe("https://static.gosuslugi.ru/styles.css") == "continue"

    # Exact-match: target apex passes.
    assert _probe("https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/") == "continue"

    # Exact-only entry does NOT match subdomains (no accidental widening).
    assert _probe("https://sub.example.org/x") == "abort"

    # Non-whitelisted hosts are aborted.
    assert _probe("https://tracker.evil.example.com/pixel.gif") == "abort"

    # Suffix attack: host that merely contains "gosuslugi.ru" inside (not as
    # suffix of hostname) MUST be aborted. urlparse.hostname returns the host
    # only, so "evil-gosuslugi.ru.attacker.com" would be hostname
    # "evil-gosuslugi.ru.attacker.com" — does NOT end with ".gosuslugi.ru".
    assert _probe("https://evil-gosuslugi.ru.attacker.com/x") == "abort"
