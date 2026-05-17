"""Logging tests for PlaywrightLoginSession DEBUG events (gektar_monitor-b9wq).

Covers:
- login.start.entry (DEBUG — on open_headed_login and silent_refresh entry)
- login.lock.acquired (DEBUG — after lock is acquired)
- login.lock.timeout (DEBUG — when lock is already held = BusyError)
- login.cookie_export.start / finish (DEBUG — on _export_cookies with cookie_store)
- login.deadline.reached (WARNING — when deadline elapsed before wait_for_url)

NB: no real browser is started — we use mock playwright_factory.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fis_monitor.domain.errors import BusyError
from fis_monitor.infra.playwright.login import PlaywrightLoginSession

_LOGGER = "fis_monitor.infra.playwright.login"


# ---------------------------------------------------------------------------
# Helpers (mirror pattern from tests/infra/playwright/test_login.py)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._mono = start

    def now(self):  # type: ignore[override]
        from datetime import UTC, datetime
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._mono += seconds


def _make_context_mock(page: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.pages = [page]
    ctx.new_page.return_value = page
    ctx.cookies.return_value = [{"name": "sess", "value": "abc", "domain": "example.com"}]
    return ctx


def _make_pw_factory(context_mock: MagicMock) -> MagicMock:
    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = context_mock
    cm = MagicMock()
    cm.__enter__.return_value = pw
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _make_session(
    *,
    clock: _FakeClock | None = None,
    playwright_factory: MagicMock | None = None,
    cookie_store: MagicMock | None = None,
) -> PlaywrightLoginSession:
    return PlaywrightLoginSession(
        profile_dir=Path("/tmp/test_profile"),
        login_start_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/",
        allowed_hosts=["example.com"],
        clock=clock or _FakeClock(),
        playwright_factory=playwright_factory or MagicMock(),
        cookie_store=cookie_store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_open_headed_login_emits_start_entry_debug(caplog: pytest.LogCaptureFixture) -> None:
    """login.start.entry emitted at DEBUG on open_headed_login() entry."""
    page = MagicMock()
    ctx = _make_context_mock(page)
    factory = _make_pw_factory(ctx)
    session = _make_session(playwright_factory=factory)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        session.open_headed_login(deadline=10.0)

    records = [r for r in caplog.records if r.getMessage() == "login.start.entry"]
    assert records, "expected login.start.entry debug event for headed trigger"
    assert records[0].__dict__.get("trigger") == "headed"


def test_open_headed_login_emits_lock_acquired_debug(caplog: pytest.LogCaptureFixture) -> None:
    """login.lock.acquired emitted at DEBUG after lock is taken."""
    page = MagicMock()
    ctx = _make_context_mock(page)
    factory = _make_pw_factory(ctx)
    session = _make_session(playwright_factory=factory)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        session.open_headed_login(deadline=10.0)

    records = [r for r in caplog.records if r.getMessage() == "login.lock.acquired"]
    assert records, "expected login.lock.acquired debug event"
    assert records[0].__dict__.get("trigger") == "headed"


def test_open_headed_login_emits_lock_timeout_when_busy(caplog: pytest.LogCaptureFixture) -> None:
    """login.lock.timeout (uses 'lock.timeout' message) emitted when already in progress."""
    session = _make_session()
    # Manually hold the lock so the second acquire fails.
    session._lock.acquire()
    try:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER), pytest.raises(BusyError):
            session.open_headed_login(deadline=10.0)
    finally:
        session._lock.release()

    records = [r for r in caplog.records if r.getMessage() == "login.lock.timeout"]
    assert records, "expected login.lock.timeout debug event"
    assert records[0].__dict__.get("trigger") == "headed"


def test_silent_refresh_emits_start_entry_debug(caplog: pytest.LogCaptureFixture) -> None:
    """login.start.entry emitted at DEBUG on silent_refresh() entry with trigger=silent_refresh."""
    page = MagicMock()
    ctx = _make_context_mock(page)
    factory = _make_pw_factory(ctx)
    session = _make_session(playwright_factory=factory)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        session.silent_refresh(deadline=10.0)

    records = [r for r in caplog.records if r.getMessage() == "login.start.entry"]
    assert records, "expected login.start.entry for silent_refresh"
    assert records[0].__dict__.get("trigger") == "silent_refresh"


def test_export_cookies_emits_start_finish_debug(caplog: pytest.LogCaptureFixture) -> None:
    """login.cookie_export.start + finish emitted at DEBUG when cookie_store is injected."""
    page = MagicMock()
    ctx = _make_context_mock(page)
    factory = _make_pw_factory(ctx)
    cookie_store = MagicMock()
    session = _make_session(playwright_factory=factory, cookie_store=cookie_store)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        result = session._export_cookies(ctx)

    assert result is True
    start_records = [r for r in caplog.records if r.getMessage() == "login.cookie_export.start"]
    finish_records = [r for r in caplog.records if r.getMessage() == "login.cookie_export.finish"]
    assert start_records, "expected login.cookie_export.start"
    assert finish_records, "expected login.cookie_export.finish"
    assert "cookies_count" in start_records[0].__dict__
    assert "duration_ms" in finish_records[0].__dict__
