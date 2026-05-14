"""Tests for ThreadSupervisor (infra/thread_supervisor.py).

All tests are deterministic — no real sleeps longer than 0.5 s except the
hung-thread test which uses a threading.Event (not time.sleep) to block the
test thread. The hung-thread test itself uses a short grace_timeout so the
total wall-clock cost is ~0.3 s.
"""

from __future__ import annotations

import threading
import time

from fis_monitor.infra.thread_supervisor import ThreadSupervisor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cooperative_target(stop_event: threading.Event) -> None:
    """Target that exits promptly when stop_event is set."""
    stop_event.wait()


def _multi_cooperative_factory(name: str):
    """Return a cooperative target (ignores the name arg — just for clarity)."""
    return _cooperative_target


# ---------------------------------------------------------------------------
# Test 1: start + immediate shutdown, cooperative target → clean
# ---------------------------------------------------------------------------


def test_shutdown_cooperative_clean():
    """Single cooperative thread exits cleanly within grace_timeout."""
    supervisor = ThreadSupervisor()
    supervisor.start("worker", _cooperative_target)
    report = supervisor.shutdown(grace_timeout=5.0)

    assert report.clean is True
    assert report.pending == []


# ---------------------------------------------------------------------------
# Test 2: shutdown(0.3) against a thread that ignores stop_event → not clean
# ---------------------------------------------------------------------------


def test_shutdown_hung_thread():
    """Hung thread (ignores stop_event) is reported as pending, not waited forever."""
    # Use an event so the test can release the thread in teardown.
    release_event = threading.Event()

    def hung_target(stop_event: threading.Event) -> None:
        # Does NOT honour stop_event — simulates a blocking C-extension or misbehaving code.
        release_event.wait()  # blocks until this test releases it

    supervisor = ThreadSupervisor()
    supervisor.start("slow", hung_target)

    report = supervisor.shutdown(grace_timeout=0.3)

    assert report.clean is False
    assert "slow" in report.pending

    # Cleanup: release the hung thread so it doesn't leak into other tests.
    release_event.set()
    # Give the thread a moment to actually exit before the test ends.
    for t in supervisor.threads:
        t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Test 3: multiple cooperative threads all exit cleanly
# ---------------------------------------------------------------------------


def test_shutdown_multiple_threads_clean():
    """Three cooperative threads all join cleanly."""
    supervisor = ThreadSupervisor()
    for name in ("alpha", "beta", "gamma"):
        supervisor.start(name, _cooperative_target)

    report = supervisor.shutdown(grace_timeout=5.0)

    assert report.clean is True
    assert report.pending == []
    assert len(supervisor.threads) == 3


# ---------------------------------------------------------------------------
# Test 4: all started threads are daemon=True
# ---------------------------------------------------------------------------


def test_threads_are_daemon():
    """All supervised threads must be daemon so interpreter exit kills them."""
    supervisor = ThreadSupervisor()
    supervisor.start("d1", _cooperative_target)
    supervisor.start("d2", _cooperative_target)

    for t in supervisor.threads:
        assert t.daemon is True, f"thread {t.name!r} must be daemon=True"

    supervisor.shutdown(grace_timeout=2.0)


# ---------------------------------------------------------------------------
# Test 5: shutdown is idempotent
# ---------------------------------------------------------------------------


def test_shutdown_idempotent():
    """Second call to shutdown() is a no-op and does not raise or block."""
    supervisor = ThreadSupervisor()
    supervisor.start("one", _cooperative_target)

    report1 = supervisor.shutdown(grace_timeout=5.0)
    assert report1.clean is True

    # Second call — must return immediately (no double-set anomaly).
    t_start = time.monotonic()
    report2 = supervisor.shutdown(grace_timeout=5.0)
    elapsed = time.monotonic() - t_start

    assert report2.clean is True
    assert report2.pending == []
    # Must return near-instantly (not wait grace_timeout again).
    assert elapsed < 0.5, f"second shutdown took {elapsed:.3f}s — expected <0.5s"


# ---------------------------------------------------------------------------
# Test 6: stop_event property is the same event passed to targets
# ---------------------------------------------------------------------------


def test_stop_event_property():
    """stop_event property is the same object the targets receive."""
    received: list[threading.Event] = []

    def capturing_target(stop_event: threading.Event) -> None:
        received.append(stop_event)
        stop_event.wait()

    supervisor = ThreadSupervisor()
    supervisor.start("cap", capturing_target)

    # Give the thread a moment to start and capture the event.
    timeout = time.monotonic() + 2.0
    while not received and time.monotonic() < timeout:
        time.sleep(0.01)

    assert received, "target did not start"
    assert received[0] is supervisor.stop_event

    supervisor.shutdown(grace_timeout=2.0)


# ---------------------------------------------------------------------------
# Test 7: threads property returns a tuple of all registered threads
# ---------------------------------------------------------------------------


def test_threads_property():
    """threads property exposes all registered threads as a tuple."""
    supervisor = ThreadSupervisor()
    assert supervisor.threads == ()

    supervisor.start("t1", _cooperative_target)
    supervisor.start("t2", _cooperative_target)

    threads = supervisor.threads
    assert isinstance(threads, tuple)
    assert len(threads) == 2
    names = {t.name for t in threads}
    assert names == {"t1", "t2"}

    supervisor.shutdown(grace_timeout=2.0)
