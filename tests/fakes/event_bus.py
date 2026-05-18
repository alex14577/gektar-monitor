"""FakeEventBus — canonical in-memory fake for the EventBus Protocol.

See ADR-041 §Fake signature canon — single fake per Protocol.
"""

from __future__ import annotations

from fis_monitor.domain.interfaces import EventSubscription, SseEvent


class FakeEventBus:
    """In-memory recording fake for ``EventBus``.

    ``published`` collects every event passed to ``publish()`` in order.
    Tests assert on the contents of this list to verify correct bus usage
    without a real threading infrastructure.

    ``evicted_types`` collects every event_type string passed to
    ``evict_normal_replay()`` in order. Mirrors the extension method on
    ``ThreadEventBus`` (same pattern as ``last_critical()``) so tests can
    assert the eviction was called without a real bus.

    ``subscribe()`` is not supported by this fake — it raises
    ``NotImplementedError`` if called, because the fake is scoped to
    publisher-side tests only.
    """

    def __init__(self) -> None:
        self.published: list[SseEvent] = []
        self.evicted_types: list[str] = []

    def publish(self, event: SseEvent) -> None:
        self.published.append(event)

    def evict_normal_replay(self, event_type: str) -> None:
        """Record the eviction request (fake implementation of ThreadEventBus extension)."""
        self.evicted_types.append(event_type)

    def subscribe(self) -> EventSubscription[SseEvent]:  # pragma: no cover
        raise NotImplementedError(
            "FakeEventBus.subscribe() is not implemented — "
            "use a real ThreadEventBus for subscriber-side tests."
        )
