"""Playwright-backed headed-login session — PlaywrightLoginSession.

Implements the ``LoginSession`` Protocol (domain/interfaces.py §3.4).

Design notes:
- **Headed-only** (``headless=False``): the user must confirm login manually
  via the browser window. No credentials are passed to Playwright.
- **Persistent context** (``profile_dir``): cookies / localStorage survive
  between invocations so the user does not re-login every startup.
- **Single-flight** via ``threading.Lock(blocking=False)`` acquire:
  a second call while one is in progress raises ``BusyError`` immediately.
- **Thread-safe cancel** (ADR-014 §phase-1.5): ``cancel()`` calls
  ``browser.close()`` from the shutdown thread; the active
  ``page.wait_for_url`` unwinds with ``TargetClosedError`` (~2-3s) and
  the worker returns ``LoginOutcome(success=False, error="cancelled")``.
- **Host-whitelist invariant**: ``context.route("**/*", handler)`` intercepts
  every request; non-whitelisted hosts are aborted, whitelisted ones continue.
- **Hard deadline** (``open_headed_login(deadline=300.0)``): 5-minute cap as
  a safety net against a user closing the tab without logging in. Returns
  ``LoginOutcome(success=False, error="timeout")``.

References:
  - ADR-014 (two-phase shutdown, phase 1.5)
  - docs/architecture/03-protocols.md §3.4
  - docs/architecture/07-concurrency.md §pw-login row
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Route, sync_playwright

from fis_monitor.domain.errors import BusyError
from fis_monitor.domain.interfaces import Clock
from fis_monitor.domain.models import LoginErrorHint, LoginOutcome

__all__ = ["PlaywrightLoginSession"]

_log = logging.getLogger(__name__)

# URL pattern that matches the FIS post-login landing page redirect.
# Playwright interprets this as a glob; we wait until the page URL matches.
_LOGIN_SUCCESS_URL_GLOB = "**/cabinet/**"

# Initial navigation target — the гектар cabinet page.
# Hitting /cabinet/ on an unauthenticated session triggers a full OAuth
# redirect chain to ЕСИА (esia.gosuslugi.ru). After successful auth the
# user is bounced back here, which both sets the Госуслуги session cookies
# AND establishes the гектар-side session cookies the monitor needs for
# scraping — going straight to esia.gosuslugi.ru would skip the latter.
_LOGIN_START_URL = "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/"

# Initial navigation timeout. Kept generous to absorb the multi-hop OAuth
# chain on slow networks; success/cancel still race the user-controlled
# wait_for_url(_LOGIN_SUCCESS_URL_GLOB) bounded by ``deadline``.
_INITIAL_GOTO_TIMEOUT_MS = 30_000

# Timeout for the silent-refresh wait_for_url check.  Kept short because
# a session with valid cookies reaches /cabinet/ in ≤2-3 s; a redirect to
# ЕСИА means we need manual login anyway so waiting longer wastes time.
_SILENT_REFRESH_WAIT_TIMEOUT_MS = 10_000


class PlaywrightLoginSession:
    """Headed-login via Playwright — ``LoginSession`` Protocol implementation.

    Args:
        profile_dir: Path to the persistent-context profile directory.
            Created automatically by Playwright if absent.
        allowed_hosts: Sequence of hostnames that are allowed to receive
            network requests during the login flow (e.g. the FIS domain and
            any required CDN/auth endpoints).  All other hosts are aborted.
        clock: Wall-clock / monotonic source (injected for testability).
        playwright_factory: Callable returning a ``sync_playwright()``
            context-manager.  Defaults to the real ``sync_playwright``; tests
            pass a mock factory here.
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        allowed_hosts: Sequence[str],
        clock: Clock,
        playwright_factory: Callable[[], Any] = sync_playwright,
    ) -> None:
        self._profile_dir = profile_dir
        self._allowed_hosts: frozenset[str] = frozenset(allowed_hosts)
        self._clock = clock
        self._playwright_factory = playwright_factory

        # Single-flight lock: acquired before starting, released in finally.
        self._lock = threading.Lock()

        # Protects _active_browser; separate from _lock so cancel() does not
        # need to wait for the full single-flight path.
        self._state_lock = threading.Lock()
        self._active_browser: Browser | None = None

    # ------------------------------------------------------------------
    # LoginSession Protocol
    # ------------------------------------------------------------------

    def open_headed_login(self, *, deadline: float) -> LoginOutcome:
        """Open a headed browser window and wait for the user to log in.

        Blocks the calling thread until one of:
        - login succeeds (URL matches the post-login glob),
        - ``deadline`` seconds elapse (returns ``error="timeout"``),
        - ``cancel()`` is called from another thread (returns ``error="cancelled"``).

        Raises:
            BusyError: if another login is already in progress.
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise BusyError(
                "PlaywrightLoginSession: open_headed_login is already in progress"
            )
        try:
            return self._run_login(deadline=deadline)
        finally:
            self._lock.release()

    def silent_refresh(self, *, deadline: float) -> LoginOutcome:
        """Navigate to /cabinet/ headlessly to renew session cookies.

        If the persistent-context profile holds valid ЕСИА cookies the
        cabinet loads without an OAuth redirect and new session cookies are
        persisted automatically by Playwright.  If the cookies are expired
        the server redirects to ЕСИА — we detect this by ``wait_for_url``
        timing out — and return ``error="needs_manual_login"``.

        Raises:
            BusyError: if another login/refresh is already in progress
                (shares the same ``_lock`` as ``open_headed_login``).
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise BusyError(
                "PlaywrightLoginSession: a login/refresh is already in progress"
            )
        try:
            return self._run_silent_refresh(deadline=deadline)
        finally:
            self._lock.release()

    def cancel(self) -> None:
        """Thread-safe external stop.

        Calls ``browser.close()`` from outside the worker thread.  Safe to
        call when no login is active (no-op).  The active ``page.wait_for_url``
        will raise ``TargetClosedError`` inside the worker and resolve to
        ``LoginOutcome(success=False, error="cancelled")``.
        """
        with self._state_lock:
            browser = self._active_browser
        if browser is not None:
            _log.info("PlaywrightLoginSession.cancel(): closing active browser")
            try:
                browser.close()
            except Exception:  # pragma: no cover — Playwright internals
                _log.debug("cancel(): browser.close() raised (already closed?)", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_silent_refresh(self, *, deadline: float) -> LoginOutcome:
        """Core silent-refresh flow — called with ``_lock`` already held.

        Identical to ``_run_login`` except:
        - ``headless=True`` (no visible window).
        - ``wait_for_url`` uses a short fixed timeout (``_SILENT_REFRESH_WAIT_TIMEOUT_MS``)
          instead of the full user-controlled deadline.
        - A timeout/redirect-to-ЕСИА is mapped to ``error="needs_manual_login"``
          instead of ``error="timeout"``.
        """
        start = self._clock.monotonic()
        remaining_ms = int((deadline - start) * 1000)

        try:
            with self._playwright_factory() as pw:
                browser = pw.chromium.launch_persistent_context(
                    str(self._profile_dir),
                    headless=True,
                    ignore_https_errors=True,
                )
                with self._state_lock:
                    self._active_browser = browser

                try:
                    return self._wait_for_silent_refresh(
                        context=browser,
                        remaining_ms=remaining_ms,
                        start=start,
                        deadline=deadline,
                    )
                finally:
                    with self._state_lock:
                        self._active_browser = None
                    try:
                        browser.close()
                    except Exception:  # pragma: no cover
                        _log.debug("_run_silent_refresh: browser.close() raised", exc_info=True)

        except Exception as exc:
            outcome, unmapped = self._map_exception(exc)
            if unmapped:
                _log.error(
                    "PlaywrightLoginSession._run_silent_refresh: unexpected exception type=%s",
                    type(exc).__name__,
                    exc_info=exc,
                )
            else:
                _log.debug(
                    "PlaywrightLoginSession._run_silent_refresh: mapped exception type=%s hint=%s",
                    type(exc).__name__,
                    outcome.error,
                )
            return outcome

    def _wait_for_silent_refresh(
        self,
        *,
        context: BrowserContext,
        remaining_ms: int,
        start: float,
        deadline: float,
    ) -> LoginOutcome:
        """Navigate to /cabinet/ and wait for the URL to confirm cookie validity."""
        context.route("**/*", self._make_route_handler())

        pages = context.pages
        page: Page = pages[0] if pages else context.new_page()

        try:
            page.goto(
                _LOGIN_START_URL,
                wait_until="domcontentloaded",
                timeout=_INITIAL_GOTO_TIMEOUT_MS,
            )
        except Exception as exc:
            outcome, unmapped = self._map_exception(exc)
            if unmapped:
                _log.error(
                    "PlaywrightLoginSession._wait_for_silent_refresh: goto failed type=%s url=%s",
                    type(exc).__name__,
                    _LOGIN_START_URL,
                    exc_info=exc,
                )
            else:
                _log.debug(
                    "PlaywrightLoginSession._wait_for_silent_refresh: goto failed type=%s hint=%s",
                    type(exc).__name__,
                    outcome.error,
                )
            return outcome

        # Check overall deadline hasn't elapsed before we start waiting.
        elapsed_ms = int((self._clock.monotonic() - start) * 1000)
        if remaining_ms - elapsed_ms <= 0:
            return LoginOutcome(
                success=False, cookies_updated=False, error="needs_manual_login"
            )

        # Short fixed timeout — either we land on /cabinet/ quickly or cookies
        # are expired.  We do NOT use the full deadline here so that a stale
        # session fails fast rather than hanging for 30 s.
        try:
            page.wait_for_url(_LOGIN_SUCCESS_URL_GLOB, timeout=_SILENT_REFRESH_WAIT_TIMEOUT_MS)
        except Exception as exc:
            # Any failure (timeout, redirect away from /cabinet/) means the
            # server didn't keep us on the cabinet page → manual login needed.
            exc_str = str(exc).lower()
            exc_type = type(exc).__name__
            if "targetclosed" in exc_type.lower() or "targetclosed" in exc_str:
                # Browser was cancelled externally.
                return LoginOutcome(success=False, cookies_updated=False, error="cancelled")
            _log.info(
                "PlaywrightLoginSession.silent_refresh: not on cabinet URL — needs manual login "
                "(exc_type=%s)",
                type(exc).__name__,
            )
            return LoginOutcome(
                success=False, cookies_updated=False, error="needs_manual_login"
            )

        _log.info("PlaywrightLoginSession: silent refresh succeeded")
        return LoginOutcome(success=True, cookies_updated=True, error=None)

    def _run_login(self, *, deadline: float) -> LoginOutcome:
        """Core login flow — called with ``_lock`` already held."""
        start = self._clock.monotonic()
        remaining_ms = int((deadline - start) * 1000)

        try:
            with self._playwright_factory() as pw:
                # ignore_https_errors=True: Russian government sites
                # (zakupki.gov.ru cyrillic punycode, gosuslugi.ru) are served
                # with certificates from the Russian Trusted Root CA (Минцифры),
                # which is NOT in Chromium's default trust store. Without this
                # flag goto() fails instantly with ERR_CERT_AUTHORITY_INVALID
                # and the browser window opens then immediately closes.
                # Risk is bounded by the host whitelist in _make_route_handler:
                # all non-whitelisted hosts are aborted before any TLS happens.
                browser = pw.chromium.launch_persistent_context(
                    str(self._profile_dir),
                    headless=False,
                    ignore_https_errors=True,
                )
                # Register the active browser so cancel() can reach it.
                with self._state_lock:
                    self._active_browser = browser

                try:
                    return self._wait_for_login(
                        context=browser,
                        remaining_ms=remaining_ms,
                        start=start,
                        deadline=deadline,
                    )
                finally:
                    with self._state_lock:
                        self._active_browser = None
                    try:
                        browser.close()
                    except Exception:  # pragma: no cover
                        _log.debug("_run_login: browser.close() raised", exc_info=True)

        except Exception as exc:
            outcome, unmapped = self._map_exception(exc)
            if unmapped:
                _log.error(
                    "PlaywrightLoginSession._run_login: unexpected exception type=%s",
                    type(exc).__name__,
                    exc_info=exc,
                )
            else:
                _log.debug(
                    "PlaywrightLoginSession._run_login: mapped exception type=%s hint=%s",
                    type(exc).__name__,
                    outcome.error,
                )
            return outcome

    def _wait_for_login(
        self,
        *,
        context: BrowserContext,
        remaining_ms: int,
        start: float,
        deadline: float,
    ) -> LoginOutcome:
        """Register route handler, get/open the page, then wait for redirect."""
        # Register host-whitelist route on the context.
        context.route("**/*", self._make_route_handler())

        # Use existing page if one is open, otherwise open a new one.
        pages = context.pages
        page: Page = pages[0] if pages else context.new_page()

        # Navigate to the login start URL. Without this the page stays on
        # about:blank and wait_for_url() below never resolves until timeout.
        # We use wait_until="domcontentloaded" rather than "load" so we don't
        # block on slow third-party assets — the OAuth redirect chain to ЕСИА
        # fires from <head> JS / Location headers either way.
        try:
            page.goto(
                _LOGIN_START_URL,
                wait_until="domcontentloaded",
                timeout=_INITIAL_GOTO_TIMEOUT_MS,
            )
        except Exception as exc:
            outcome, unmapped = self._map_exception(exc)
            if unmapped:
                _log.error(
                    "PlaywrightLoginSession._wait_for_login: goto failed type=%s url=%s",
                    type(exc).__name__,
                    _LOGIN_START_URL,
                    exc_info=exc,
                )
            else:
                _log.debug(
                    "PlaywrightLoginSession._wait_for_login: goto failed type=%s hint=%s",
                    type(exc).__name__,
                    outcome.error,
                )
            return outcome

        # Adjust remaining timeout accounting for setup time.
        elapsed_ms = int((self._clock.monotonic() - start) * 1000)
        timeout_ms = max(remaining_ms - elapsed_ms, 0)

        if timeout_ms <= 0:
            return LoginOutcome(success=False, cookies_updated=False, error="timeout")

        try:
            page.wait_for_url(_LOGIN_SUCCESS_URL_GLOB, timeout=timeout_ms)
        except Exception as exc:
            outcome, unmapped = self._map_exception(exc)
            if unmapped:
                _log.error(
                    "PlaywrightLoginSession._wait_for_login: unexpected exception type=%s",
                    type(exc).__name__,
                    exc_info=exc,
                )
            else:
                _log.debug(
                    "PlaywrightLoginSession._wait_for_login: mapped exception type=%s hint=%s",
                    type(exc).__name__,
                    outcome.error,
                )
            return outcome

        # Success: let Playwright persist the context (cookies saved to profile).
        _log.info("PlaywrightLoginSession: login succeeded")
        return LoginOutcome(success=True, cookies_updated=True, error=None)

    def _make_route_handler(self) -> Callable[[Route], None]:
        """Return a route handler that enforces the host-whitelist invariant.

        Whitelist matching rules:
        - Exact match: ``allowed_hosts`` entry equals the request hostname.
        - Suffix match: an entry beginning with ``"."`` (e.g. ``".gosuslugi.ru"``)
          matches any hostname ending with that suffix
          (``"esia.gosuslugi.ru"``, ``"id.gosuslugi.ru"``, …).
          This is an *explicit* convention — a bare ``"gosuslugi.ru"`` entry
          will NOT match subdomains, so config typos cannot accidentally
          widen the policy.
        """
        allowed_hosts = self._allowed_hosts
        exact_hosts = frozenset(h for h in allowed_hosts if not h.startswith("."))
        suffix_hosts = tuple(h for h in allowed_hosts if h.startswith("."))

        def _handler(route: Route) -> None:
            url = route.request.url
            try:
                from urllib.parse import urlparse
                host = urlparse(url).hostname or ""
            except Exception:
                route.abort()
                return
            allowed = host in exact_hosts or any(
                host.endswith(suffix) for suffix in suffix_hosts
            )
            if allowed:
                route.continue_()
            else:
                _log.debug("PlaywrightLoginSession: aborting non-whitelisted host %s", host)
                route.abort()

        return _handler

    @staticmethod
    def _map_exception(exc: BaseException) -> tuple[LoginOutcome, bool]:
        """Map a Playwright (or other) exception to a ``LoginOutcome`` error hint.

        Returns:
            A ``(outcome, unmapped)`` pair where ``unmapped`` is ``True`` when
            the exception did not match any known pattern (caller should log at
            ERROR level; known hints are logged at DEBUG by the caller).
        """
        exc_type = type(exc).__name__
        exc_str = str(exc).lower()

        hint: LoginErrorHint
        unmapped = False
        if "targetclosed" in exc_type.lower() or "targetclosed" in exc_str:
            hint = "cancelled"
        elif "timeout" in exc_type.lower() or "timeout" in exc_str:
            hint = "timeout"
        elif "disconnect" in exc_type.lower() or "disconnect" in exc_str:
            hint = "playwright_disconnect"
        elif "executable doesn't exist" in exc_str or "please run: playwright install" in exc_str:
            hint = "playwright_missing_binary"
        elif (
            "host system is missing dependencies" in exc_str
            or (
                "missing" in exc_str
                and any(lib in exc_str for lib in ("libnss", "libatk", "libgtk"))
            )
        ):
            hint = "playwright_missing_deps"
        elif "playwright" in exc_type.lower() or "playwright" in exc_str:
            hint = "playwright_other"
        else:
            hint = "playwright_other"
            unmapped = True

        return LoginOutcome(success=False, cookies_updated=False, error=hint), unmapped
