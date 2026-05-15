"""Layer 1 tests for SqliteStateRepository.

Test layer: Layer 1 (Repository).
Coverage: round-trip get/set/delete, NULL for absent key, overwrite semantics.

Concurrency (BEGIN IMMEDIATE) is NOT covered here — the invariant is shared
across all repository write-paths and is covered by the general concurrent-write
tests in test_settings_repo.py.

Uses the ``tmp_db`` fixture (conftest.py) — fresh ConnectionProvider with the
full v2 schema applied.  A local FakeClock keeps timing deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.state import SqliteStateRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock stub with controllable UTC time."""

    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2024, 6, 1, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self._dt = self._dt + timedelta(seconds=seconds)


def make_repo(
    tmp_db: ConnectionProvider,
    clock: FakeClock | None = None,
) -> SqliteStateRepository:
    if clock is None:
        clock = FakeClock()
    return SqliteStateRepository(conn_provider=tmp_db, clock=clock)


def _raw_updated_at(provider: ConnectionProvider, key: str) -> str:
    """Read updated_at directly from the DB for a given key."""
    conn = provider.get()
    row = conn.execute(
        "SELECT updated_at FROM state WHERE key = ?", (key,)
    ).fetchone()
    assert row is not None, f"Key {key!r} not found in state table"
    return row[0]


# ---------------------------------------------------------------------------
# Tests — get: absent key
# ---------------------------------------------------------------------------


def test_get_absent_key_returns_none(tmp_db: ConnectionProvider) -> None:
    """get() returns None for a key that has never been set."""
    repo = make_repo(tmp_db)
    assert repo.get("no_such_key") is None


def test_get_absent_key_after_delete_returns_none(tmp_db: ConnectionProvider) -> None:
    """get() returns None after the key has been deleted."""
    repo = make_repo(tmp_db)
    repo.set("ephemeral", "value")
    repo.delete("ephemeral")
    assert repo.get("ephemeral") is None


# ---------------------------------------------------------------------------
# Tests — set / get round-trip
# ---------------------------------------------------------------------------


def test_set_and_get_round_trip(tmp_db: ConnectionProvider) -> None:
    """set() followed by get() returns the exact stored value."""
    repo = make_repo(tmp_db)
    repo.set("my_key", "my_value")
    assert repo.get("my_key") == "my_value"


def test_set_stores_empty_string(tmp_db: ConnectionProvider) -> None:
    """Empty string is a valid value and must not be confused with None."""
    repo = make_repo(tmp_db)
    repo.set("flag", "")
    result = repo.get("flag")
    assert result == ""
    assert result is not None


def test_set_stores_whitespace_value_verbatim(tmp_db: ConnectionProvider) -> None:
    """Whitespace-only values are preserved exactly (no strip side-effects)."""
    repo = make_repo(tmp_db)
    repo.set("ws", "   \t\n  ")
    assert repo.get("ws") == "   \t\n  "


def test_set_stores_unicode_value(tmp_db: ConnectionProvider) -> None:
    """Unicode values (JSON, Cyrillic, emoji) round-trip without corruption."""
    repo = make_repo(tmp_db)
    value = '{"type":"session","msg":"сессия истекла 🔑"}'
    repo.set("last_critical_event:session", value)
    assert repo.get("last_critical_event:session") == value


# ---------------------------------------------------------------------------
# Tests — overwrite
# ---------------------------------------------------------------------------


def test_set_overwrites_existing_value(tmp_db: ConnectionProvider) -> None:
    """Second set() on the same key replaces the first value."""
    repo = make_repo(tmp_db)
    repo.set("k", "original")
    repo.set("k", "updated")
    assert repo.get("k") == "updated"


def test_set_overwrite_advances_updated_at(tmp_db: ConnectionProvider) -> None:
    """updated_at is bumped on every set(), even when the value is unchanged."""
    clock = FakeClock()
    repo = make_repo(tmp_db, clock)

    repo.set("k", "v1")
    t1 = _raw_updated_at(tmp_db, "k")

    clock.advance(1)
    repo.set("k", "v2")
    t2 = _raw_updated_at(tmp_db, "k")

    assert t2 > t1, "updated_at must advance on overwrite"


def test_set_same_value_still_advances_updated_at(tmp_db: ConnectionProvider) -> None:
    """Writing the same value again still refreshes updated_at."""
    clock = FakeClock()
    repo = make_repo(tmp_db, clock)

    repo.set("k", "same_value")
    t1 = _raw_updated_at(tmp_db, "k")

    clock.advance(2)
    repo.set("k", "same_value")
    t2 = _raw_updated_at(tmp_db, "k")

    assert t2 > t1


# ---------------------------------------------------------------------------
# Tests — delete
# ---------------------------------------------------------------------------


def test_delete_removes_key(tmp_db: ConnectionProvider) -> None:
    """delete() removes a key so subsequent get() returns None."""
    repo = make_repo(tmp_db)
    repo.set("to_delete", "present")
    repo.delete("to_delete")
    assert repo.get("to_delete") is None


def test_delete_non_existent_key_is_noop(tmp_db: ConnectionProvider) -> None:
    """delete() on a key that does not exist is idempotent — no exception."""
    repo = make_repo(tmp_db)
    repo.delete("phantom_key")  # must not raise


def test_delete_does_not_affect_other_keys(tmp_db: ConnectionProvider) -> None:
    """Deleting one key leaves unrelated keys intact."""
    repo = make_repo(tmp_db)
    repo.set("a", "alpha")
    repo.set("b", "beta")
    repo.delete("a")
    assert repo.get("a") is None
    assert repo.get("b") == "beta"


def test_set_after_delete_reinstates_key(tmp_db: ConnectionProvider) -> None:
    """A key can be re-set after deletion — the row is recreated cleanly."""
    repo = make_repo(tmp_db)
    repo.set("lifecycle", "first")
    repo.delete("lifecycle")
    repo.set("lifecycle", "second")
    assert repo.get("lifecycle") == "second"


# ---------------------------------------------------------------------------
# Tests — multiple distinct keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("last_critical_event:session", '{"ts":"2024-01-01T00:00:00"}'),
        ("last_critical_event:cycle", '{"ts":"2024-01-02T00:00:00"}'),
        ("last_critical_event:smtp", '{"ts":"2024-01-03T00:00:00"}'),
        ("smtp_test_last_result_ok", "1"),
        ("session_expired", "1"),
    ],
)
def test_known_key_namespaces_round_trip(
    tmp_db: ConnectionProvider, key: str, value: str
) -> None:
    """Known key namespaces from the schema comment each round-trip correctly."""
    repo = make_repo(tmp_db)
    repo.set(key, value)
    assert repo.get(key) == value
