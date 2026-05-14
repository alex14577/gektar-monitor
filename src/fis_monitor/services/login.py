"""LoginService — thin orchestrator over LoginSession.

Implements the single-flight login workflow per ADR-014 (two-phase shutdown,
phase 1.5). ``LoginService`` holds a ``threading.Lock`` to enforce one active
login job at a time; a second ``start_login()`` call raises ``LoginBusyError``
immediately.

Design:
  - Single-flight: only one ``open_headed_login`` job may run at a time.
  - Executor binding: ``bind_executor(executor)`` called in lifespan phase 1.5
    so the service can submit blocking I/O to a dedicated thread pool.
  - ``cancel_active_job()``: idempotent, calls ``login_session.cancel()``.
  - ``status()``: returns ``LoginStatus(running, last_outcome)`` without blocking.

Thread-safety:
  - ``_lock`` guards the single-flight invariant.
  - ``_state_lock`` guards ``_active_future`` and ``_last_outcome``.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from fis_monitor.domain.errors import DomainError
from fis_monitor.domain.interfaces import Clock, LoginSession
from fis_monitor.domain.models import LoginOutcome

__all__ = [
    "LoginBusyError",
    "LoginJobHandle",
    "LoginService",
    "LoginStatus",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

# Default deadline for a single headed-login flow (5 minutes).
_DEFAULT_DEADLINE_SECONDS: float = 300.0

# Default deadline for a silent-refresh flow (30 seconds).
# Kept short because silent refresh either succeeds quickly (valid cookies)
# or fails fast (redirect to ЕСИА).  A 5-minute cap would be wasted wait time.
_DEFAULT_REFRESH_DEADLINE_SECONDS: float = 30.0


class LoginBusyError(DomainError):
    """Raised by ``LoginService.start_login()`` when a login job is already running.

    Callers should surface this as "login already in progress" and NOT retry.
    """


@dataclass(frozen=True)
class LoginJobHandle:
    """Opaque handle returned by ``LoginService.start_login()``.

    The caller may inspect ``future`` for the eventual ``LoginOutcome``, but
    MUST NOT block on it from the request-handler thread — doing so would
    starve the event loop.
    """

    future: Future[LoginOutcome]


@dataclass(frozen=True)
class LoginStatus:
    """Snapshot of the login service state, returned by ``LoginService.status()``."""

    running: bool
    last_outcome: LoginOutcome | None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LoginService:
    """Thin orchestrator over ``LoginSession`` — single-flight, executor-bound.

    Args:
        login_session: ``LoginSession`` Protocol implementation (Playwright-backed
            in production; fake in tests).
        clock: Wall-clock / monotonic time source.
        executor: Optional ``ThreadPoolExecutor`` for running the blocking
            ``open_headed_login`` call. Bound via ``bind_executor()`` in lifespan.
    """

    def __init__(
        self,
        *,
        login_session: LoginSession,
        clock: Clock,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._session = login_session
        self._clock = clock
        self._executor: ThreadPoolExecutor | None = executor

        # Single-flight lock: held for the duration of the login job.
        self._lock = threading.Lock()

        # Protects _active_future and _last_outcome from data races.
        self._state_lock = threading.Lock()
        self._active_future: Future[LoginOutcome] | None = None
        self._last_outcome: LoginOutcome | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_login(self) -> LoginJobHandle:
        """Start a headed-login job in the thread executor.

        Single-flight: raises ``LoginBusyError`` if a job is already running.

        Returns:
            ``LoginJobHandle`` wrapping the ``Future[LoginOutcome]``.

        Raises:
            LoginBusyError: if another login is already in progress.
            RuntimeError: if no executor has been bound yet (``bind_executor``
                has not been called — only possible in misconfigured startup).
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise LoginBusyError("Login already in progress")

        # Lock acquired — schedule the job.  On any failure, release the lock
        # before re-raising so a subsequent call is not permanently blocked.
        if self._executor is None:
            self._lock.release()
            raise RuntimeError(
                "LoginService: no executor bound — call bind_executor() first"
            )

        try:
            deadline = self._clock.monotonic() + _DEFAULT_DEADLINE_SECONDS
            future: Future[LoginOutcome] = self._executor.submit(
                self._run_login, deadline
            )

            with self._state_lock:
                self._active_future = future

            # Attach a callback to release the lock and record the outcome.
            future.add_done_callback(self._on_done)

        except Exception:
            self._lock.release()
            raise

        return LoginJobHandle(future=future)

    def start_refresh(self) -> LoginJobHandle:
        """Start a silent-refresh job in the thread executor.

        Navigates to /cabinet/ headlessly using the persistent-context profile
        to renew session cookies without opening a visible browser window.
        If the existing ЕСИА cookies are valid, the cabinet loads and new
        session cookies are persisted.  If cookies are expired the job resolves
        with ``error="needs_manual_login"`` — the caller should surface a
        prompt to use ``/auth/start`` for a full headed login.

        Single-flight: shares ``_lock`` with ``start_login()`` — raises
        ``LoginBusyError`` if a headed login OR another refresh is already
        running.

        Returns:
            ``LoginJobHandle`` wrapping the ``Future[LoginOutcome]``.

        Raises:
            LoginBusyError: if another login/refresh is already in progress.
            RuntimeError: if no executor has been bound yet.
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise LoginBusyError("Login/refresh already in progress")

        if self._executor is None:
            self._lock.release()
            raise RuntimeError(
                "LoginService: no executor bound — call bind_executor() first"
            )

        try:
            deadline = self._clock.monotonic() + _DEFAULT_REFRESH_DEADLINE_SECONDS
            future: Future[LoginOutcome] = self._executor.submit(
                self._run_refresh, deadline
            )

            with self._state_lock:
                self._active_future = future

            future.add_done_callback(self._on_done)

        except Exception:
            self._lock.release()
            raise

        return LoginJobHandle(future=future)

    def cancel_active_job(self) -> None:
        """Cancel the active login job by closing the browser.

        Idempotent: safe to call when no job is running (``cancel()`` on
        ``PlaywrightLoginSession`` is a no-op in that case).
        """
        _log.info("LoginService.cancel_active_job(): requesting cancel")
        self._session.cancel()

    def status(self) -> LoginStatus:
        """Return a snapshot of the current login state without blocking."""
        with self._state_lock:
            running = (
                self._active_future is not None
                and not self._active_future.done()
            )
            last_outcome = self._last_outcome
        return LoginStatus(running=running, last_outcome=last_outcome)

    def bind_executor(self, executor: ThreadPoolExecutor) -> None:
        """Bind the executor pool (called in lifespan phase 1.5, ADR-014).

        Safe to call before or after construction. Replaces any previously
        bound executor.
        """
        self._executor = executor

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_login(self, deadline: float) -> LoginOutcome:
        """Blocking worker submitted to the executor thread pool.

        Exceptions propagate into the Future so ``_on_done`` is the single
        error-mapping point (single source of truth for exception → LoginOutcome).
        """
        return self._session.open_headed_login(deadline=deadline)

    def _run_refresh(self, deadline: float) -> LoginOutcome:
        """Blocking worker for silent refresh submitted to the executor thread pool."""
        return self._session.silent_refresh(deadline=deadline)

    def _on_done(self, future: Future[LoginOutcome]) -> None:
        """Future completion callback — runs on the executor thread.

        Single source of truth for mapping exceptions to LoginOutcome.
        """
        try:
            outcome = future.result()
        except Exception:
            _log.exception("LoginService: unexpected exception in login worker")
            outcome = LoginOutcome(success=False, cookies_updated=False, error="playwright_other")

        with self._state_lock:
            self._last_outcome = outcome
            self._active_future = None

        # Release the single-flight lock so a new job can start.
        try:
            self._lock.release()
        except RuntimeError:
            # Lock was not held — shouldn't happen, but guard defensively.
            _log.warning("LoginService._on_done: lock.release() raised RuntimeError")
