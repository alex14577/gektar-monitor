"""Concrete subscription handle classes for ThreadEventBus and config hot-reload.

Two classes:
  - ``ThreadEventSubscription`` — per-subscriber queue-based handle returned by
    ``ThreadEventBus.subscribe()``. Extracted from ``bus.py`` for cohesion
    (bus = fan-out logic; subscription = per-subscriber handle lifecycle).
  - ``ThreadConfigSubscription`` — callback-based handle returned by
    ``ConfigSource.subscribe(cb)``.

Low-coupling design: both classes accept a *remover callback* in their
constructors instead of a direct reference to the owning bus/source. This
prevents a circular-import chain between ``subscriptions.py`` and ``bus.py``,
and allows the remover to be any callable — making unit testing straightforward
(pass a plain lambda/Mock instead of a full bus instance).
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fis_monitor.domain.models import Settings, SseEvent

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 100


# ---------------------------------------------------------------------------
# ThreadEventSubscription
# ---------------------------------------------------------------------------


class ThreadEventSubscription:
    """Per-subscriber queue-based context-manager handle.

    Returned by ``ThreadEventBus.subscribe()``. Callers SHOULD use it as a
    context manager so that ``unsubscribe()`` is guaranteed on disconnect.

    ``iter()`` is a non-blocking generator: drains the queue until empty, then
    returns. Callers poll it inside an async executor (SSE route).

    Low coupling:
        The constructor takes a *remover* callable instead of the bus object.
        ``ThreadEventBus`` passes ``self._remove_subscriber`` at construction
        time; unit tests pass a simple ``Mock`` or lambda.

    Thread-safety:
        ``_alive`` flag is written under the bus lock during force-unsubscribe.
        In CPython the boolean read/write is GIL-atomic. For PEP-703
        free-threaded interpreter (3.13t+) — re-evaluate.
    """

    def __init__(
        self,
        remover: Callable[[ThreadEventSubscription], None],
        maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        self._remover = remover
        self._q: queue.Queue[SseEvent] = queue.Queue(maxsize=maxsize)
        self._alive = True

    # ------------------------------------------------------------------
    # Context-manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ThreadEventSubscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        self.unsubscribe()
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """True while the subscription is active; False after unsubscribe or force-remove."""
        return self._alive

    def unsubscribe(self) -> None:
        """Remove this subscription from the bus. Idempotent."""
        self._remover(self)

    def iter(self) -> Iterator[SseEvent]:
        """Non-blocking generator: yield all events currently in the queue."""
        while True:
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return

    def wait_one(self, timeout: float) -> SseEvent | None:
        """Blocking dequeue. Returns the next event, or None on timeout / dead subscription.

        Called from an executor thread (NOT the asyncio event loop). Blocks at most
        *timeout* seconds. Returns None both on queue.Empty timeout and when the
        subscription has been force-unsubscribed (alive=False).
        """
        if not self._alive:
            return None
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# ThreadConfigSubscription
# ---------------------------------------------------------------------------


class ThreadConfigSubscription:
    """Concrete context-manager handle for ``ConfigSource.subscribe(callback)``.

    Holds the callback and a remover callable. ``deliver(settings)`` is called
    by the ``ConfigSource`` to push a new ``Settings`` snapshot to all live
    subscribers.

    Idempotency guarantee:
        ``unsubscribe()`` calls the remover at most once (guarded by ``_lock``).
        Subsequent calls are no-ops. ``deliver()`` is silently skipped after
        ``unsubscribe()``.

    Thread-safety:
        A single ``threading.Lock`` serialises the ``_alive`` flag flip to
        prevent double-invoke of the remover or delivery to a dead callback.
    """

    def __init__(
        self,
        cb: Callable[[Settings], None],
        remover: Callable[[ThreadConfigSubscription], None],
    ) -> None:
        self._cb = cb
        self._remover = remover
        self._alive = True
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Context-manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ThreadConfigSubscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        self.unsubscribe()
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """True while the subscription is active; False after unsubscribe."""
        with self._lock:
            return self._alive

    def unsubscribe(self) -> None:
        """Remove this subscription from the config source. Idempotent."""
        with self._lock:
            if not self._alive:
                return
            self._alive = False
        # Call remover outside the lock to avoid potential deadlock if the
        # remover itself acquires a lock (e.g. a list lock in ConfigSource).
        self._remover(self)

    def deliver(self, settings: Settings) -> None:
        """Push *settings* to the callback if still alive. Called by ConfigSource."""
        with self._lock:
            alive = self._alive
        if alive:
            self._cb(settings)
