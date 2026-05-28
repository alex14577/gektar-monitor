"""ShutdownRequesterCell — late-binding, fail-closed ShutdownRequester.

Infra detail: NOT domain, NOT composition.  Placed in ``infra/`` because it is
a runtime adapter concern — it wraps a concrete requester that depends on the
uvicorn Server which is only available after lifespan startup.

Design
------
- Implements the ``ShutdownRequester`` Protocol structurally.
- ``bind(real)`` may be called exactly once; a second call raises ``RuntimeError``
  (latch semantics — prevents silent rebinding).
- Before ``bind()`` is called, ``request_shutdown()`` is **fail-closed**: prints
  a forensic banner to stderr, logs CRITICAL, and calls ``os._exit(1)``.
  This prevents a silent fail-open if expiry fires before the real requester is
  bound (e.g., during a very early startup race).
- After ``bind()``, delegates to the real requester.

Usage (composition root + lifespan)::

    # composition.py — build time
    cell = ShutdownRequesterCell()
    supervisor = LicenseExpirySupervisor(..., shutdown_requester=cell)

    # app.py — lifespan, BEFORE supervisor.start("license-expiry", ...)
    cell.bind(_UvicornShutdownRequester(server, _license_expiry_triggered))

Invariant: ``bind()`` must be called BEFORE ``supervisor.start("license-expiry", ...)``
to ensure the cell is bound before the supervisor's background thread can run.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

__all__ = ["ShutdownRequesterCell"]


class ShutdownRequesterCell:
    """One-shot late-binding cell implementing the ``ShutdownRequester`` Protocol.

    Fail-closed: calling ``request_shutdown()`` before ``bind()`` triggers
    an emergency hard-exit (``os._exit(1)``) rather than silently ignoring
    the shutdown request.
    """

    def __init__(self) -> None:
        self._real: object | None = None
        self._lock = threading.Lock()

    def bind(self, real: object) -> None:
        """Bind the real ``ShutdownRequester`` implementation.

        Args:
            real: An object with a ``request_shutdown() -> None`` method.

        Raises:
            RuntimeError: If ``bind`` has already been called (latch semantics).
        """
        with self._lock:
            if self._real is not None:
                raise RuntimeError(
                    "ShutdownRequesterCell.bind() called more than once — "
                    "only one binding is permitted."
                )
            self._real = real

    def request_shutdown(self) -> None:
        """Delegate to the bound requester, or hard-exit if not yet bound.

        Fail-closed: if no real requester has been bound, this method prints
        a forensic banner and calls ``os._exit(1)`` to prevent silent fail-open.
        """
        with self._lock:
            real = self._real

        if real is None:
            # Fail-closed: no requester bound yet.  This should never happen in
            # production because bind() is called before supervisor.start().
            print(
                "CRITICAL: ShutdownRequesterCell.request_shutdown() called before bind() — "
                "emergency hard-exit to prevent running with an invalid license.",
                file=sys.stderr,
            )
            logger.critical(
                "shutdown_cell.unbound_exit: request_shutdown called before bind; "
                "hard-exit via os._exit(1)"
            )
            os._exit(1)

        real.request_shutdown()  # type: ignore[union-attr]
