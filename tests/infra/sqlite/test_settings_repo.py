"""Integration tests for SqliteSettingsRepository.

Uses the ``tmp_db`` fixture (from conftest.py) which provides a fresh
ConnectionProvider with the full v2 schema applied.

A local ``FakeClock`` is defined here to keep tests hermetic.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from fis_monitor.domain.models import OnboardingState
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.settings import SqliteSettingsRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock stub — returns a fixed UTC datetime that can be advanced."""

    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        """Advance the clock by *seconds* so updated_at values differ."""
        self._dt = self._dt + timedelta(seconds=seconds)


def make_repo(
    tmp_db: ConnectionProvider, clock: FakeClock | None = None
) -> SqliteSettingsRepository:
    if clock is None:
        clock = FakeClock()
    return SqliteSettingsRepository(conn_provider=tmp_db, clock=clock)


# ---------------------------------------------------------------------------
# Tests — generic k/v
# ---------------------------------------------------------------------------


def test_get_missing_key_returns_none(tmp_db: ConnectionProvider) -> None:
    """get() on a non-existent key returns None."""
    repo = make_repo(tmp_db)
    assert repo.get("non_existent_key") is None


def test_set_and_get_round_trip(tmp_db: ConnectionProvider) -> None:
    """set() followed by get() returns the stored value."""
    repo = make_repo(tmp_db)
    repo.set("my_key", "my_value")
    assert repo.get("my_key") == "my_value"


def test_set_is_idempotent_overwrites_value(tmp_db: ConnectionProvider) -> None:
    """Repeated set() on the same key overwrites value and updates updated_at."""
    clock = FakeClock()
    repo = make_repo(tmp_db, clock)

    repo.set("k", "v1")
    t1 = _get_updated_at(tmp_db, "k")

    clock.advance(1)
    repo.set("k", "v2")
    t2 = _get_updated_at(tmp_db, "k")

    assert repo.get("k") == "v2"
    assert t2 > t1, "updated_at must advance on overwrite"


def _get_updated_at(provider: ConnectionProvider, key: str) -> str:
    conn = provider.get_connection()
    row = conn.execute("SELECT updated_at FROM state WHERE key = ?", (key,)).fetchone()
    assert row is not None
    return row[0]


def test_empty_string_value_is_stored(tmp_db: ConnectionProvider) -> None:
    """set() with empty string stores '' — not confused with None."""
    repo = make_repo(tmp_db)
    repo.set("flag", "")
    result = repo.get("flag")
    assert result == ""
    assert result is not None


def test_whitespace_value_preserved(tmp_db: ConnectionProvider) -> None:
    """set() with whitespace-only value preserves it exactly."""
    repo = make_repo(tmp_db)
    repo.set("ws", "   \t\n  ")
    assert repo.get("ws") == "   \t\n  "


# ---------------------------------------------------------------------------
# Tests — onboarding FSM helpers
# ---------------------------------------------------------------------------


def test_get_onboarding_default_is_not_started(tmp_db: ConnectionProvider) -> None:
    """get_onboarding() returns NOT_STARTED when no row exists."""
    repo = make_repo(tmp_db)
    assert repo.get_onboarding() == OnboardingState.NOT_STARTED


def test_set_and_get_onboarding_round_trip_all_states(tmp_db: ConnectionProvider) -> None:
    """set_onboarding/get_onboarding round-trips every enum member."""
    repo = make_repo(tmp_db)
    for state in OnboardingState:
        repo.set_onboarding(state)
        assert repo.get_onboarding() == state


# ---------------------------------------------------------------------------
# Tests — concurrency / isolation
# ---------------------------------------------------------------------------


def test_parallel_set_from_different_connections(tmp_db: ConnectionProvider) -> None:
    """Two separate connection objects can set different keys without conflict."""
    # We simulate two independent 'callers' by calling get_connection() on
    # different ConnectionProvider instances that share the same db path.
    # Since ConnectionProvider is per-thread, we run each set on a thread.
    from pathlib import Path

    db_path: Path = tmp_db._db_path  # type: ignore[attr-defined]  # test-only private access

    provider_a = ConnectionProvider(db_path=db_path)
    provider_b = ConnectionProvider(db_path=db_path)

    clock_a = FakeClock()
    clock_b = FakeClock()
    repo_a = SqliteSettingsRepository(conn_provider=provider_a, clock=clock_a)
    repo_b = SqliteSettingsRepository(conn_provider=provider_b, clock=clock_b)

    errors: list[Exception] = []

    def writer_a() -> None:
        try:
            repo_a.set("key_a", "value_a")
        except Exception as exc:
            errors.append(exc)

    def writer_b() -> None:
        try:
            repo_b.set("key_b", "value_b")
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer_a)
    t2 = threading.Thread(target=writer_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    provider_a.close_all()
    provider_b.close_all()

    assert errors == [], f"Concurrent writes raised: {errors}"

    # Verify both values are readable from tmp_db (shared file)
    repo_main = make_repo(tmp_db)
    assert repo_main.get("key_a") == "value_a"
    assert repo_main.get("key_b") == "value_b"
