"""ThreadSupervisor — manages supervised daemon threads with two-phase shutdown.

Implements the Phase 1 (graceful) shutdown contract from ADR-014.
Phase 1.5 / Phase 2 / Phase 3 live in lifespan() (different lifecycle concerns:
executors, lock release).

Design:
  - All threads are daemon=True so Python interpreter exit kills any
    dangling threads without needing explicit phase 2 handling here.
  - A shared ``stop_event`` is passed to every target so they can honour
    shutdown without polling.
  - ``shutdown()`` sets stop_event and joins each thread with a
    remaining-deadline budget (so total wait ≤ grace_timeout regardless of
    thread count).
  - ``ShutdownReport`` carries ``clean`` bool and ``pending`` name list for
    logging / test assertions.

See: docs/architecture/04-composition-root.md §4.3.bis, ADR-014.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

logger = logging.getLogger(__name__)


class ShutdownReport(NamedTuple):
    """Result of ``ThreadSupervisor.shutdown()``."""

    clean: bool
    """True iff all threads joined within grace_timeout."""
    pending: list[str]
    """Names of threads still alive after grace_timeout."""


class ThreadSupervisor:
    """Manages a set of supervised daemon threads with a shared stop_event.

    Usage::

        supervisor = ThreadSupervisor()
        supervisor.start("full-scan", service.run_forever)
        supervisor.start("notifier",  lambda _e: dispatcher.consumer_loop())

        report = supervisor.shutdown(grace_timeout=35.0)
        if not report.clean:
            logger.warning("dangling threads: %s", report.pending)

    The ``target`` callable must accept a single ``threading.Event`` argument
    (the shared stop_event). On shutdown, ``stop_event.set()`` is called and
    the callable is expected to exit promptly.

    Threads marked ``daemon=True`` — Python interpreter exit will kill any
    that did not join in time (phase 2 fallback, ADR-014).
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown_called = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, name: str, target: Callable[[threading.Event], None]) -> None:
        """Start a daemon thread running ``target(stop_event)``.

        Args:
            name:   Human-readable thread name (used in logs and ShutdownReport).
            target: Callable that receives the shared stop_event and runs until
                    it is set.
        """
        stop_event = self._stop_event
        thread = threading.Thread(
            target=target,
            args=(stop_event,),
            name=name,
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        logger.debug("supervisor: started thread %r", name)

    def shutdown(self, grace_timeout: float = 35.0) -> ShutdownReport:
        """Phase 1 graceful shutdown: signal all threads and join within deadline.

        Sets stop_event and joins each thread with the remaining time budget
        (deadline = now + grace_timeout, shared across all joins).

        Idempotent: a second call is a no-op and returns immediately with
        ``clean=True, pending=[]`` (all threads were already joined on the
        first call).

        Args:
            grace_timeout: Total seconds to wait across all threads (ADR-014).

        Returns:
            ``ShutdownReport`` with ``clean=True`` if all joined, else
            ``clean=False`` with ``pending`` listing names of stuck threads.
        """
        with self._lock:
            if self._shutdown_called:
                logger.debug("supervisor.shutdown: already called, no-op")
                return ShutdownReport(clean=True, pending=[])
            self._shutdown_called = True
            threads = list(self._threads)

        logger.info("supervisor: signalling %d thread(s) to stop", len(threads))
        self._stop_event.set()

        deadline = time.monotonic() + grace_timeout
        pending: list[str] = []

        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if thread.is_alive():
                    pending.append(thread.name)
                continue
            thread.join(timeout=remaining)
            if thread.is_alive():
                pending.append(thread.name)
                logger.warning(
                    "supervisor: thread %r did not join within grace_timeout=%.1fs",
                    thread.name,
                    grace_timeout,
                )
                # Dump ALL thread stacks to stderr for diagnostics (faulthandler
                # API doesn't filter by thread.ident — phase 2 fallback per ADR-014).
                if thread.ident is not None:
                    with contextlib.suppress(Exception):
                        faulthandler.dump_traceback()

        clean = len(pending) == 0
        if clean:
            logger.info("supervisor: clean shutdown — all threads joined")
        else:
            logger.warning("supervisor: %d thread(s) still alive: %s", len(pending), pending)

        return ShutdownReport(clean=clean, pending=pending)

    @property
    def stop_event(self) -> threading.Event:
        """The shared stop_event passed to every target thread."""
        return self._stop_event

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        """Snapshot of all registered threads (for healthcheck / diagnostics)."""
        with self._lock:
            return tuple(self._threads)
