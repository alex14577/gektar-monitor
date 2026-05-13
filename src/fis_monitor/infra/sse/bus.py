"""ThreadEventBus — sync→async SSE fan-out bridge.

Implements the ``EventBus`` Protocol (domain/interfaces.py).

Architecture:
  - One ``queue.Queue(maxsize=100)`` per subscriber.
  - Normal events: ``put_nowait`` with drop-from-tail on overflow.
  - Critical events: blocking ``put(timeout)``; force-unsubscribe on timeout.
  - Per-type in-memory slots for last critical event (``last_critical()``).
  - Single ``threading.Lock`` protecting subscriber list + critical slots.

ADR-008: No DB persistence. Event slots live in memory only.
See docs/decisions/ADR-008-eventbus-dual-circuit-no-db-persistence.md.

Subscription handle is ``ThreadEventSubscription`` from ``.subscriptions``
(extracted for cohesion: bus = fan-out logic, handle = per-subscriber lifecycle).
"""
from __future__ import annotations

import contextlib
import logging
import queue
import threading

from fis_monitor.domain.models import SseEvent
from fis_monitor.infra.sse.subscriptions import ThreadEventSubscription

logger = logging.getLogger(__name__)

_DEFAULT_CRITICAL_TIMEOUT = 2.0


class ThreadEventBus:
    """Thread-safe in-memory EventBus with normal / critical routing.

    Implements ``EventBus`` Protocol from ``domain/interfaces.py``.

    Scope (tic.1 vs tic.2+):
        Per-type slots stored in memory only. State-table persistence with
        TTL=1h per ADR-008 R3-C5 is deferred to follow-up ``gektar_monitor-12y``
        (StateRepository). Until then, ``last_critical()`` is process-lifetime
        best-effort: on restart history is lost, SSE reconnect won't replay
        critical events from prior process.

    Extra public method (not in Protocol):
        ``last_critical(event_type) -> SseEvent | None``
        Returns the most-recently published critical event for *event_type*,
        or ``None`` if none has been published yet. Used by SSE reconnect
        logic to replay missed critical events.

        Not part of EventBus Protocol. Callers requiring this method must hold
        a concrete ``ThreadEventBus`` reference (not ``EventBus``). Future:
        consider ``ReplayableEventBus(EventBus, Protocol)`` extension if more
        impls add replay support.

    Constructor:
        critical_timeout: seconds to block on ``queue.put`` for critical events
            before force-unsubscribing the slow consumer. Default 2.0s
            (matches ADR-008 / docs/architecture/07-concurrency.md §7.3).
    """

    def __init__(self, critical_timeout: float = _DEFAULT_CRITICAL_TIMEOUT) -> None:
        self._critical_timeout = critical_timeout
        self._lock = threading.Lock()
        self._subscribers: list[ThreadEventSubscription] = []
        # Per-type in-memory slot for last critical event.
        # Key: concrete SseEvent class; value: the event instance.
        self._last_critical: dict[type, SseEvent] = {}

    # ------------------------------------------------------------------
    # EventBus Protocol
    # ------------------------------------------------------------------

    def publish(self, event: SseEvent) -> None:
        """Publish *event* to all live subscribers.

        Routing is determined by ``event.priority`` (ClassVar on each model):
          - ``"normal"``  → ``put_nowait``; drop-from-tail on overflow.
          - ``"critical"``→ blocking ``put(timeout)``; force-unsubscribe on
                            timeout; update per-type last_critical slot.
        """
        priority: str = event.priority  # type: ignore[attr-defined]

        # Take a snapshot of subscribers under lock so we don't hold the lock
        # while doing potentially blocking queue operations.
        with self._lock:
            snapshot = list(self._subscribers)

        if priority == "critical":
            self._publish_critical(event, snapshot)
        else:
            self._publish_normal(event, snapshot)

    def subscribe(self) -> ThreadEventSubscription:
        """Return a new per-subscriber handle and register it."""
        sub = ThreadEventSubscription(remover=self._remove_subscriber)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    # ------------------------------------------------------------------
    # Extra public helper (not in Protocol)
    # ------------------------------------------------------------------

    def last_critical(self, event_type: type) -> SseEvent | None:
        """Return the last published critical event of *event_type*, or None.

        Not part of EventBus Protocol. Callers requiring this method must hold
        a concrete ``ThreadEventBus`` reference (not ``EventBus``). Future:
        consider ``ReplayableEventBus(EventBus, Protocol)`` extension if more
        impls add replay support.
        """
        with self._lock:
            return self._last_critical.get(event_type)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish_normal(
        self, event: SseEvent, snapshot: list[ThreadEventSubscription]
    ) -> None:
        """Best-effort delivery for normal events.

        The eviction (get_nowait + put_nowait) is non-atomic; a concurrent
        publisher may consume the freed slot, in which case this publication
        is silently dropped. Acceptable because normal events are stale-able
        (SSE clients receive fresher updates on next publish).
        DO NOT add per-subscriber lock — it would contend with the fast path
        and harm throughput.
        """
        for sub in snapshot:
            # Lock-free read of _alive: safe in CPython (GIL guarantees boolean atomicity).
            # For PEP 703 free-threaded interpreter (3.13t+) — re-evaluate.
            if not sub._alive:
                continue
            try:
                sub._q.put_nowait(event)
            except queue.Full:
                # Drop-from-tail: evict the oldest item, then enqueue the new one.
                with contextlib.suppress(queue.Empty):
                    sub._q.get_nowait()
                with contextlib.suppress(queue.Full):
                    sub._q.put_nowait(event)
                    # queue.Full here means another producer raced us; accept the loss.

    def _publish_critical(
        self, event: SseEvent, snapshot: list[ThreadEventSubscription]
    ) -> None:
        # Update per-type slot first (under lock, atomic overwrite).
        with self._lock:
            self._last_critical[type(event)] = event

        to_force_unsubscribe: list[ThreadEventSubscription] = []

        for sub in snapshot:
            # Lock-free read of _alive: safe in CPython (GIL guarantees boolean atomicity).
            # For PEP 703 free-threaded interpreter (3.13t+) — re-evaluate.
            if not sub._alive:
                continue
            try:
                sub._q.put(event, timeout=self._critical_timeout)
            except queue.Full:
                # Slow consumer: force-unsubscribe.
                logger.warning(
                    "EventBus: force-unsubscribing slow consumer "
                    "(event_type=%s, queue full after %.1fs timeout)",
                    type(event).__name__,
                    self._critical_timeout,
                )
                to_force_unsubscribe.append(sub)

        for sub in to_force_unsubscribe:
            self._force_remove(sub)

    def _force_remove(self, sub: ThreadEventSubscription) -> None:
        """Mark subscriber dead and remove from the live list (under lock)."""
        with self._lock:
            sub._alive = False
            with contextlib.suppress(ValueError):
                self._subscribers.remove(sub)

    def _remove_subscriber(self, sub: ThreadEventSubscription) -> None:
        """Called by ``_ThreadEventSubscription.unsubscribe()`` — idempotent."""
        with self._lock:
            sub._alive = False
            with contextlib.suppress(ValueError):
                self._subscribers.remove(sub)
