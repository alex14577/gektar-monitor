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
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, get_args, get_type_hints

from fis_monitor.domain.interfaces import EventBus
from fis_monitor.domain.models import SseEvent

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import EventSubscription

logger = logging.getLogger(__name__)


def _derive_known_sse_events() -> frozenset[str]:
    """Derive the set of valid SSE event discriminators from the ``SseEvent`` union.

    ``SseEvent`` is a PEP-695 ``TypeAliasType`` (``type SseEvent = A | B | ...``).
    ``get_args()`` on a ``TypeAliasType`` returns an empty tuple; the actual union
    is stored in ``__value__``.  We unpack that to enumerate each concrete member,
    then read the ``Literal[...]`` default on their ``event`` field.

    This is the SSOT — adding a new member to ``SseEvent`` automatically includes
    its discriminator here with no manual maintenance.
    """
    # PEP-695 TypeAliasType stores the RHS expression in __value__.
    union = getattr(SseEvent, "__value__", SseEvent)
    known: set[str] = set()
    for member in get_args(union):
        hints = get_type_hints(member, include_extras=False)
        event_hint = hints.get("event")
        if event_hint is None:
            continue
        for arg in get_args(event_hint):
            if isinstance(arg, str):
                known.add(arg)
    return frozenset(known)


#: Closed set of valid SSE event-type discriminators, derived from the ``SseEvent``
#: union at import time.  Unknown discriminators trigger schema-drift logging + drop.
_KNOWN_SSE_EVENTS: frozenset[str] = _derive_known_sse_events()

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
        sse_executor: ThreadPoolExecutor | None = None,
        ping_interval: float = _DEFAULT_PING_INTERVAL,
        event_encoder: Callable[[SseEvent], bytes] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._sse_executor = sse_executor
        self._ping_interval = ping_interval
        self._event_encoder: Callable[[SseEvent], bytes] = (
            event_encoder if event_encoder is not None else encode_sse_event
        )

    def bind_executor(self, executor: ThreadPoolExecutor) -> None:
        """Bind the executor pool (called in lifespan, ADR-014 late-binding pattern).

        Mirrors ``LoginService.bind_executor``: the executor is created in
        lifespan *after* ``build_container()`` because executor construction is
        a runtime-resource concern, not a wiring concern.  ``SseStreamer`` is
        constructed without an executor in the composition root, then receives
        one here once the lifespan has created the pool.

        Safe to call before or after the first ``stream()`` call, as long as
        it is called before any ``stream()`` call attempts to block on
        ``run_in_executor``.  In production this is always done in lifespan
        startup before any request is served.

        See: ADR-014 (late-binding executor pattern).
        """
        self._sse_executor = executor

    def bind_event_encoder(self, encoder: Callable[[SseEvent], bytes]) -> None:
        """Replace the event encoder (called in lifespan after templates are ready).

        The default encoder (``encode_sse_event``) serialises every event to
        JSON.  The web layer supplies a template-aware encoder via
        ``web.sse_encoder.make_html_sse_encoder(templates.env)`` that renders
        Jinja2 HTML fragments for ``SseLotNew`` events and falls back to JSON
        for all others.

        Late-binding mirrors ``bind_executor``: the Jinja2 ``Environment`` is
        created before the lifespan but the encoder is only wired once both the
        container and templates are confirmed live.

        Thread-safety: ``_event_encoder`` is replaced atomically (Python GIL
        guarantees reference assignment is atomic on CPython).  Encoders called
        concurrently in flight will finish with the old encoder; the new encoder
        applies to all subsequent calls.
        """
        self._event_encoder = encoder

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

        if self._sse_executor is None:
            raise RuntimeError(
                "SseStreamer: no executor bound — call bind_executor() first"
            )
        sse_executor = self._sse_executor

        subscription: EventSubscription[SseEvent] = self._event_bus.subscribe()
        try:
            # Initial keep-alive so the client knows the connection is alive.
            yield _encode_ping()
            loop = asyncio.get_running_loop()
            while True:
                # Drain one batch — runs in sse_executor, does NOT block event loop.
                events = await loop.run_in_executor(
                    sse_executor,
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
                        encoded = self._event_encoder(event)
                        if not encoded:
                            continue  # dropped by schema-drift guard
                        yield encoded
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

    The ``event`` field (Literal discriminator) from the model is used as the
    SSE event type. Each concrete ``SseEvent`` subclass has an ``event``
    field that doubles as the SSE event name.

    Schema-drift guard: if ``event.event`` is not in ``_KNOWN_SSE_EVENTS``
    (e.g. a future event type not yet handled by this infra version) the
    event is dropped (returns ``b""``) and logged at ERROR level.  The caller
    MUST skip empty return values.  Fail-closed — unknown types never reach
    the wire.
    """
    # event_type: the Literal discriminator field value, e.g. "lot.new"
    event_type: str = event.event  # type: ignore[attr-defined]

    if event_type not in _KNOWN_SSE_EVENTS:
        logger.error(
            "sse.schema_drift",
            extra={"event_type": event_type, "event_class": type(event).__name__},
        )
        return b""  # drop signal — caller skips empty bytes

    json_data: str = event.model_dump_json()  # type: ignore[union-attr]

    # Split on newlines for RFC compliance.
    data_lines = "\n".join(f"data: {line}" for line in json_data.split("\n"))
    return f"event: {event_type}\n{data_lines}\n\n".encode()


def _encode_ping() -> bytes:
    """Return a keep-alive SSE ping frame."""
    return b"event: ping\ndata: \n\n"
