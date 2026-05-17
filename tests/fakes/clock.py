"""FakeClock — canonical in-memory fake for the Clock Protocol.

See ADR-041 §Fake signature canon — single fake per Protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime


class FakeClock:
    """Deterministic clock for tests.

    ``now()`` returns the value supplied at construction time and never
    advances on its own. Tests that need to model time progression call
    ``advance(seconds)`` or ``set_now(...)`` explicitly so the test reads
    as a sequence of explicit time moves rather than implicit drift.

    ``monotonic()`` returns a value that grows in lock-step with ``now()``;
    callers that only care about ordering (not wall-clock) can rely on it.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now: datetime = now if now is not None else datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic: float = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def set_now(self, value: datetime) -> None:
        self._now = value

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += seconds
