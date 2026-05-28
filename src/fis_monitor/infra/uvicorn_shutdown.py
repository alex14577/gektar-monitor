"""UvicornShutdownRequester — adapter that signals uvicorn.Server to exit.

Infra adapter: wraps the uvicorn ``Server.should_exit`` flag behind the
``ShutdownRequester`` Protocol so ``LicenseExpirySupervisor`` is decoupled
from uvicorn internals.

Design
------
- ``request_shutdown()`` is safe to call from any thread: it uses
  ``asyncio.AbstractEventLoop.call_soon_threadsafe`` so the flag assignment
  happens from inside the event-loop thread.
- Also sets a caller-supplied ``threading.Event`` so the lifespan finally-
  block can detect that a license-expiry shutdown was requested (and exit
  with code 1 instead of 0).

Usage (app.py lifespan)::

    from fis_monitor.infra.uvicorn_shutdown import UvicornShutdownRequester

    requester = UvicornShutdownRequester(
        loop=asyncio.get_running_loop(),
        server=uvicorn_server,
        triggered_event=_license_expiry_triggered,
    )
    container.services.license_expiry_shutdown_cell.bind(requester)
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

__all__ = ["UvicornShutdownRequester"]


class UvicornShutdownRequester:
    """Thread-safe adapter: requests uvicorn graceful shutdown.

    Args:
        loop:            The running ``asyncio`` event loop (captured in lifespan).
        server:          The ``uvicorn.Server`` instance (has ``should_exit`` attr).
        triggered_event: A ``threading.Event`` set when shutdown is requested,
                         so the lifespan finally-block can detect license-expiry
                         shutdown and exit with code 1.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        server: object,
        triggered_event: threading.Event,
    ) -> None:
        self._loop = loop
        self._server = server
        self._triggered_event = triggered_event

    def request_shutdown(self) -> None:
        """Signal uvicorn to exit gracefully.  Thread-safe, idempotent."""
        # Set the triggered event unconditionally — the lifespan finally-block
        # checks this flag to decide whether to exit with code 1.
        self._triggered_event.set()
        try:
            self._loop.call_soon_threadsafe(
                lambda: setattr(self._server, "should_exit", True)
            )
        except RuntimeError:
            # Event loop already closed — the process is shutting down anyway.
            # triggered_event is set so the lifespan finally-block will call
            # os._exit(1).  The watchdog (armed in _handle_expiry before this
            # call) is our safety net if lifespan is already past its finally.
            # No re-raise: propagating would enter supervisor's outer except
            # which short-circuits on _expiry_handled=True, leaving no exit.
            logger.warning("uvicorn_shutdown.loop_closed_during_request")
