from __future__ import annotations


class FakeStateRepository:
    """Canonical in-memory fake for StateRepository Protocol.

    See ADR-041 §Fake signature canon — single fake per Protocol.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial) if initial else {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
