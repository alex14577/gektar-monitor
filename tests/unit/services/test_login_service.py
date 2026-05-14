"""Unit tests for LoginService.

Anti-mock pattern: ``FakeLoginSession`` implements ALL methods of the
``LoginSession`` Protocol and has a test that calls each one. This prevents
runtime bugs from untested fake API calls.

Coverage:
  1. start_login() → 202, returns LoginJobHandle with a future.
  2. start_login() while running → LoginBusyError.
  3. status() when idle → running=False, last_outcome=None.
  4. status() after completed job → running=False, last_outcome set.
  5. cancel_active_job() delegates to login_session.cancel() (idempotent).
  6. bind_executor() rebinds the executor.
  7. start_login() without executor → RuntimeError.
  8. FakeLoginSession all-methods invocation (anti-mock).
"""

from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import LoginOutcome
from fis_monitor.services.login import LoginBusyError, LoginJobHandle, LoginService, LoginStatus

# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock fake for LoginService tests."""

    def __init__(self, *, mono: float = 1000.0) -> None:
        self._mono = mono

    def now(self) -> datetime:
        return datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._mono


class FakeLoginSession:
    """Fake LoginSession that records calls — exercises ALL Protocol methods.

    ``open_headed_login`` blocks until ``_release_event`` is set, allowing
    tests to control job completion timing.
    """

    def __init__(self, outcome: LoginOutcome | None = None) -> None:
        self._outcome = outcome or LoginOutcome(
            success=True, cookies_updated=True, error=None
        )
        self._release_event = threading.Event()
        self._cancel_called = False
        self._open_called = False

    def open_headed_login(self, *, deadline: float) -> LoginOutcome:
        """Block until released; return the configured outcome."""
        self._open_called = True
        self._release_event.wait(timeout=5.0)
        return self._outcome

    def cancel(self) -> None:
        """Record that cancel was called."""
        self._cancel_called = True
        # Unblock open_headed_login so the future resolves.
        self._release_event.set()

    # Test helpers

    def release(self) -> None:
        """Unblock the worker thread (simulate login completed)."""
        self._release_event.set()


def _make_service(
    session: FakeLoginSession | None = None,
    *,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[LoginService, FakeLoginSession]:
    """Create a LoginService with a bound FakeLoginSession and executor."""
    sess = session or FakeLoginSession()
    clock = FakeClock()
    svc = LoginService(login_session=sess, clock=clock, executor=executor)
    return svc, sess


# ---------------------------------------------------------------------------
# Tests: FakeLoginSession — all methods (anti-mock §6)
# ---------------------------------------------------------------------------


def test_fake_login_session_all_methods() -> None:
    """Invoke ALL methods of FakeLoginSession to detect runtime API bugs."""
    sess = FakeLoginSession()
    # cancel() is idempotent before any job starts
    sess.cancel()
    assert sess._cancel_called is True
    # open_headed_login() returns configured outcome once released
    sess.release()
    result = sess.open_headed_login(deadline=300.0)
    assert isinstance(result, LoginOutcome)
    assert sess._open_called is True


# ---------------------------------------------------------------------------
# Tests: LoginService
# ---------------------------------------------------------------------------


def test_status_idle() -> None:
    """Status when no job has ever been started."""
    svc, _ = _make_service()
    st = svc.status()
    assert isinstance(st, LoginStatus)
    assert st.running is False
    assert st.last_outcome is None


def test_start_login_without_executor_raises() -> None:
    """start_login() without a bound executor raises RuntimeError."""
    svc, _ = _make_service(executor=None)
    with pytest.raises(RuntimeError, match="no executor bound"):
        svc.start_login()


def test_start_login_returns_handle() -> None:
    """start_login() with an executor returns a LoginJobHandle."""
    sess = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess, executor=ex)
        handle = svc.start_login()
        assert isinstance(handle, LoginJobHandle)
        # Unblock the worker.
        sess.release()
        # Wait for completion.
        outcome = handle.future.result(timeout=5.0)
    assert outcome.success is True


def test_start_login_single_flight() -> None:
    """A second start_login() while running raises LoginBusyError."""
    sess = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess, executor=ex)
        handle = svc.start_login()
        # Job is still running (session not released yet).
        with pytest.raises(LoginBusyError):
            svc.start_login()
        # Clean up: release the worker.
        sess.release()
        handle.future.result(timeout=5.0)


def test_start_login_single_flight_releases_after_done() -> None:
    """After a job completes, start_login() is allowed again."""
    sess1 = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess1, executor=ex)
        handle1 = svc.start_login()
        sess1.release()
        handle1.future.result(timeout=5.0)

        # Second job allowed after first completes.
        sess2 = FakeLoginSession()
        svc._session = sess2
        handle2 = svc.start_login()
        sess2.release()
        outcome = handle2.future.result(timeout=5.0)
    assert outcome.success is True


def test_status_running_during_job() -> None:
    """status() returns running=True while a job is in progress."""
    sess = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess, executor=ex)
        handle = svc.start_login()
        # Brief yield to let the worker thread start.
        time.sleep(0.05)
        st = svc.status()
        assert st.running is True
        # Clean up.
        sess.release()
        handle.future.result(timeout=5.0)


def test_status_after_job_completed() -> None:
    """status() returns running=False, last_outcome set after job completes."""
    sess = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess, executor=ex)
        handle = svc.start_login()
        sess.release()
        handle.future.result(timeout=5.0)
        st = svc.status()
    assert st.running is False
    assert st.last_outcome is not None
    assert st.last_outcome.success is True


def test_cancel_active_job_delegates_to_session() -> None:
    """cancel_active_job() calls session.cancel()."""
    sess = FakeLoginSession()
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc, _ = _make_service(session=sess, executor=ex)
        handle = svc.start_login()
        svc.cancel_active_job()
        # cancel() unblocks the worker via _release_event.set()
        handle.future.result(timeout=5.0)
    assert sess._cancel_called is True


def test_cancel_idempotent_when_idle() -> None:
    """cancel_active_job() is idempotent when no job is running."""
    sess = FakeLoginSession()
    svc, _ = _make_service(session=sess)
    # Should not raise.
    svc.cancel_active_job()
    assert sess._cancel_called is True


def test_bind_executor() -> None:
    """bind_executor() allows start_login() to run after binding."""
    sess = FakeLoginSession()
    svc, _ = _make_service(session=sess, executor=None)
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc.bind_executor(ex)
        handle = svc.start_login()
        sess.release()
        outcome = handle.future.result(timeout=5.0)
    assert outcome.success is True


# ---------------------------------------------------------------------------
# Tests: exception propagation (M2 — single error-mapping point)
# ---------------------------------------------------------------------------


class _RaisingLoginSession:
    """LoginSession fake that raises RuntimeError in open_headed_login."""

    def open_headed_login(self, *, deadline: float) -> LoginOutcome:
        raise RuntimeError("simulated playwright crash")

    def cancel(self) -> None:
        pass


def test_unhandled_exception_in_run_login_maps_to_playwright_other() -> None:
    """Unhandled exception in worker maps to error='playwright_other' in last_outcome.

    _on_done is the single error-mapping point: it catches the re-raised exception
    from future.result() and stores a LoginOutcome with error='playwright_other'.
    """
    sess = _RaisingLoginSession()
    clock = FakeClock()
    svc = LoginService(login_session=sess, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as ex:
        svc.bind_executor(ex)
        handle = svc.start_login()
        # Wait for the future to settle (exception is stored, _on_done fires).
        # future.result() itself raises — that's expected. We check last_outcome.
        with contextlib.suppress(RuntimeError):
            handle.future.result(timeout=5.0)
    # Give _on_done time to run (it's a done callback on the executor thread).
    # The lock release in _on_done confirms it ran — check status instead.
    st = svc.status()
    assert st.running is False
    assert st.last_outcome is not None
    assert st.last_outcome.success is False
    assert st.last_outcome.error == "playwright_other"
