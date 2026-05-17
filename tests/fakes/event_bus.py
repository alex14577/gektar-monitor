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

    ``subscribe()`` is not supported by this fake — it raises
    ``NotImplementedError`` if called, because the fake is scoped to
    publisher-side tests only.
    """

    def __init__(self) -> None:
        self.published: list[SseEvent] = []

    def publish(self, event: SseEvent) -> None:
        self.published.append(event)

    def subscribe(self) -> EventSubscription[SseEvent]:  # pragma: no cover
        raise NotImplementedError(
            "FakeEventBus.subscribe() is not implemented — "
            "use a real ThreadEventBus for subscriber-side tests."
        )
