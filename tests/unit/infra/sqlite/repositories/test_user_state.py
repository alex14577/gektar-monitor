"""Unit tests for SqliteUserStateRepository.

Uses the ``tmp_db`` fixture (tests/conftest.py) — per-test ConnectionProvider
with the full v2 schema applied via init_db().

All time-related assertions use a ``FixedClock`` fake so tests are
fully deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.user_state import SqliteUserStateRepository

# ---------------------------------------------------------------------------
# Fake Clock
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)


class FixedClock:
    """Clock fake that always returns a fixed UTC instant."""

    def __init__(self, now: datetime = _BASE_TIME) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

LOT_ID = 101


def _make_repo(tmp_db: ConnectionProvider) -> SqliteUserStateRepository:
    return SqliteUserStateRepository(conn_provider=tmp_db, clock=FixedClock())


# ---------------------------------------------------------------------------
# Tests: get()
# ---------------------------------------------------------------------------


def test_get_missing_lot_returns_none(tmp_db: ConnectionProvider) -> None:
    """get() for an unknown lot_id returns None (no row in DB)."""
    repo = _make_repo(tmp_db)
    assert repo.get(999) is None


def test_get_after_set_starred_returns_state(tmp_db: ConnectionProvider) -> None:
    """get() reflects state written by set_starred()."""
    repo = _make_repo(tmp_db)
    repo.set_starred(LOT_ID, True)
    state = repo.get(LOT_ID)
    assert state is not None
    assert state.lot_id == LOT_ID
    assert state.starred is True
    assert state.submitted is False  # default


def test_get_returns_correct_updated_at(tmp_db: ConnectionProvider) -> None:
    """updated_at is stored and returned as an aware datetime."""
    repo = _make_repo(tmp_db)
    repo.set_starred(LOT_ID, False)
    state = repo.get(LOT_ID)
    assert state is not None
    assert state.updated_at is not None
    assert state.updated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Tests: get_many()
# ---------------------------------------------------------------------------


def test_get_many_empty_list_returns_empty_dict(tmp_db: ConnectionProvider) -> None:
    """get_many([]) must return {} without touching the DB (no SQL error)."""
    repo = _make_repo(tmp_db)
    result = repo.get_many([])
    assert result == {}


def test_get_many_all_missing_returns_empty_dict(tmp_db: ConnectionProvider) -> None:
    """get_many with all unknown ids returns an empty dict."""
    repo = _make_repo(tmp_db)
    result = repo.get_many([1, 2, 3])
    assert result == {}


def test_get_many_mixed_returns_only_existing(tmp_db: ConnectionProvider) -> None:
    """get_many returns only the ids that have rows; missing ones are absent."""
    repo = _make_repo(tmp_db)
    repo.set_starred(10, True)
    repo.set_starred(20, False)
    result = repo.get_many([10, 20, 99])  # 99 is missing
    assert set(result.keys()) == {10, 20}
    assert result[10].starred is True
    assert result[20].starred is False


# ---------------------------------------------------------------------------
# Tests: set_starred / set_submitted / set_note (UPSERT field isolation)
# ---------------------------------------------------------------------------


def test_set_starred_then_set_submitted_both_values_preserved(
    tmp_db: ConnectionProvider,
) -> None:
    """UPSERT for one field must NOT clobber a different field set earlier."""
    repo = _make_repo(tmp_db)
    submitted_at = datetime(2026, 5, 14, 9, 0, 0, tzinfo=UTC)

    repo.set_starred(LOT_ID, True)
    repo.set_submitted(LOT_ID, True, submitted_at)

    state = repo.get(LOT_ID)
    assert state is not None
    assert state.starred is True
    assert state.submitted is True
    assert state.submitted_at is not None
    assert state.submitted_at == submitted_at


def test_set_submitted_then_set_starred_both_values_preserved(
    tmp_db: ConnectionProvider,
) -> None:
    """Reverse order: submitted first, starred second — both survive."""
    repo = _make_repo(tmp_db)
    repo.set_submitted(LOT_ID, True, None)
    repo.set_starred(LOT_ID, True)

    state = repo.get(LOT_ID)
    assert state is not None
    assert state.submitted is True
    assert state.starred is True


def test_set_note_stores_and_retrieves(tmp_db: ConnectionProvider) -> None:
    """set_note() persists a string note."""
    repo = _make_repo(tmp_db)
    repo.set_note(LOT_ID, "Interesting lot")
    state = repo.get(LOT_ID)
    assert state is not None
    assert state.note == "Interesting lot"


def test_set_note_none_clears_existing_note(tmp_db: ConnectionProvider) -> None:
    """set_note(None) erases a previously stored note."""
    repo = _make_repo(tmp_db)
    repo.set_note(LOT_ID, "Some note")
    repo.set_note(LOT_ID, None)
    state = repo.get(LOT_ID)
    assert state is not None
    assert state.note is None


def test_set_submitted_with_none_at(tmp_db: ConnectionProvider) -> None:
    """set_submitted with at=None stores submitted=True, submitted_at=None."""
    repo = _make_repo(tmp_db)
    repo.set_submitted(LOT_ID, True, None)
    state = repo.get(LOT_ID)
    assert state is not None
    assert state.submitted is True
    assert state.submitted_at is None


# ---------------------------------------------------------------------------
# Tests: mark_visited / last_visit
# ---------------------------------------------------------------------------


def test_last_visit_without_prior_mark_returns_none(tmp_db: ConnectionProvider) -> None:
    """last_visit() returns None when mark_visited() was never called."""
    repo = _make_repo(tmp_db)
    assert repo.last_visit() is None


def test_mark_visited_and_last_visit_round_trip(tmp_db: ConnectionProvider) -> None:
    """mark_visited() followed by last_visit() returns the same aware datetime."""
    repo = _make_repo(tmp_db)
    visit_time = datetime(2026, 5, 14, 8, 30, 0, tzinfo=UTC)
    repo.mark_visited(visit_time)
    result = repo.last_visit()
    assert result is not None
    assert result == visit_time


def test_mark_visited_preserves_tzinfo(tmp_db: ConnectionProvider) -> None:
    """last_visit() result must be timezone-aware (tzinfo is not None)."""
    repo = _make_repo(tmp_db)
    visit_time = datetime(2026, 5, 14, 8, 30, 0, tzinfo=UTC)
    repo.mark_visited(visit_time)
    result = repo.last_visit()
    assert result is not None
    assert result.tzinfo is not None


def test_mark_visited_non_utc_offset_preserved(tmp_db: ConnectionProvider) -> None:
    """mark_visited with a non-UTC offset retains the offset after round-trip."""
    repo = _make_repo(tmp_db)
    tz_plus3 = timezone(timedelta(hours=3))
    visit_time = datetime(2026, 5, 14, 11, 30, 0, tzinfo=tz_plus3)
    repo.mark_visited(visit_time)
    result = repo.last_visit()
    assert result is not None
    # Both represent the same instant in time
    assert result == visit_time


def test_mark_visited_naive_datetime_raises_value_error(
    tmp_db: ConnectionProvider,
) -> None:
    """mark_visited() must reject naive datetimes with ValueError."""
    repo = _make_repo(tmp_db)
    naive = datetime(2026, 5, 14, 8, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="naive datetime"):
        repo.mark_visited(naive)


def test_mark_visited_overwrites_previous_timestamp(tmp_db: ConnectionProvider) -> None:
    """Calling mark_visited() twice keeps only the most recent timestamp."""
    repo = _make_repo(tmp_db)
    first = datetime(2026, 5, 14, 8, 0, 0, tzinfo=UTC)
    second = datetime(2026, 5, 14, 9, 0, 0, tzinfo=UTC)
    repo.mark_visited(first)
    repo.mark_visited(second)
    result = repo.last_visit()
    assert result == second
