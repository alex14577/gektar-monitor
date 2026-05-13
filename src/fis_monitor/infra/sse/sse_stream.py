"""SseStreamer — sync EventBus → async text/event-stream bridge.

Architecture (docs/architecture/07-concurrency.md §7.3):
  - Each SSE connection gets its own ``EventSubscription`` via ``event_bus.subscribe()``.
  - Blocking ``subscription.wait_one(timeout)`` is off-loaded to a dedicated
    ``ThreadPoolExecutor`` (sse-wait executor, max_workers=64 in production).
    This keeps the asyncio event loop free while waiting for events.
  - On timeout → yield SSE keep-alive ping.
  - On cancelled / disconnected → ``subscription.unsubscribe()`` in ``finally``.

Origin check is NOT performed here (transport-agnostic bridge).
Origin validation lives in the FastAPI SSE route (web/routes/sse.py, task oxy.6).
See micro-decision in ADR-??? (to be created by oxy.6 task).
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from fis_monitor.domain.interfaces import EventBus
from fis_monitor.domain.models import SseEvent

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import EventSubscription

logger = logging.getLogger(__name__)

_DEFAULT_PING_INTERVAL = 15.0  # seconds between keep-alive pings


class SseStreamer:
    """Sync EventBus → async text/event-stream bridge.

    Created in composition root, injected into SSE routes.
    Uses a dedicated ``ThreadPoolExecutor`` for blocking ``queue.get()``
    (sse-wait executor, max_workers=64, separate from the FastAPI handler pool).

    Invariants:
      - Each call to ``stream()`` creates its own ``EventSubscription`` via
        ``event_bus.subscribe()``.
      - ``subscription.unsubscribe()`` is guaranteed in ``finally`` on every
        exit path (normal, CancelledError, GeneratorExit, exception).
      - On ``wait_one`` timeout → yield SSE ping (``event: ping\\ndata: \\n\\n``).
      - On dead subscription (force-unsubscribed by bus) → stream terminates.
    """

    def __init__(
        self,
        *,
        event_bus: EventBus,
        sse_executor: ThreadPoolExecutor,
        ping_interval: float = _DEFAULT_PING_INTERVAL,
    ) -> None:
        self._event_bus = event_bus
        self._sse_executor = sse_executor
        self._ping_interval = ping_interval

    async def stream(self) -> AsyncIterator[bytes]:
        """Async generator producing SSE-encoded bytes.

        Usage in FastAPI::

            @app.get("/sse/events")
            async def events(request: Request):
                return StreamingResponse(
                    streamer.stream(), media_type="text/event-stream"
                )
        """
        import asyncio

        subscription: EventSubscription[SseEvent] = self._event_bus.subscribe()
        try:
            # Initial keep-alive so the client knows the connection is alive.
            yield _encode_ping()
            loop = asyncio.get_running_loop()
            while True:
                # Drain one batch — runs in sse_executor, does NOT block event loop.
                events = await loop.run_in_executor(
                    self._sse_executor,
                    self._drain_one,
                    subscription,
                )
                if events is None:
                    # Bus force-unsubscribed us (slow consumer or explicit removal).
                    logger.debug("SseStreamer: subscription dead, closing stream")
                    return
                if not events:
                    # Timeout — emit keep-alive ping.
                    yield _encode_ping()
                else:
                    for event in events:
                        yield encode_sse_event(event)
        finally:
            with contextlib.suppress(Exception):
                subscription.unsubscribe()

    def _drain_one(
        self, subscription: EventSubscription[SseEvent]
    ) -> list[SseEvent] | None:
        """Blocking helper running in sse_executor thread.

        Waits up to ``ping_interval`` seconds for at least one event.
        Returns:
          - ``None``         — subscription is dead (bus force-unsubscribed it).
          - ``[]``           — timeout, no events; caller should emit ping.
          - ``[evt, ...]``   — one or more events drained from the queue.

        Contract with ``wait_one``:
          *  ``wait_one`` returns ``None`` on timeout AND on dead subscription.
          *  We disambiguate by checking ``subscription.alive`` afterward:
             alive=True  → timeout     → return ``[]``
             alive=False → dead sub    → return ``None``
        """
        event = subscription.wait_one(timeout=self._ping_interval)
        if event is None:
            # Could be timeout or dead subscription — check alive flag.
            if subscription.alive:
                return []  # timeout
            return None  # dead subscription

        # Got at least one event; drain any additional immediately available.
        result: list[SseEvent] = [event]
        for extra in subscription.iter():
            result.append(extra)
        return result


# ---------------------------------------------------------------------------
# Serialization helpers (module-level, pure functions — easy to unit-test)
# ---------------------------------------------------------------------------


def encode_sse_event(event: SseEvent) -> bytes:
    """Serialize an ``SseEvent`` to a text/event-stream chunk.

    Format per RFC 8895:
      ``event: <type>\\ndata: <json>\\n\\n``

    Multi-line data (``\\n`` in JSON) is split so each line starts with
    ``data:``, satisfying RFC 8895 §9.2.5 ("If the data string contains
    a U+000A LINE FEED (LF) character, then dispatch the event, then
    initialize the data buffer to the empty string.").

    The ``event`` ClassVar (discriminator) from the model is used as the
    SSE event type. Each concrete ``SseEvent`` subclass has an ``event``
    field (Literal) that doubles as the SSE event name.
    """
    # event_type: the Literal discriminator field value, e.g. "lot.new"
    event_type: str = event.event  # type: ignore[attr-defined]
    json_data: str = event.model_dump_json()  # type: ignore[union-attr]

    # Split on newlines for RFC compliance.
    data_lines = "\n".join(f"data: {line}" for line in json_data.split("\n"))
    return f"event: {event_type}\n{data_lines}\n\n".encode()


def _encode_ping() -> bytes:
    """Return a keep-alive SSE ping frame."""
    return b"event: ping\ndata: \n\n"
