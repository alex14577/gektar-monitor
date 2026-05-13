"""Integration tests for SqliteNotificationsRepository.

Uses ``tmp_db`` fixture (tests/conftest.py) — per-test ConnectionProvider
with the full v2 schema applied.

All time-related calls go through a ``FixedClock`` fake defined below, so
tests are fully deterministic and never touch ``datetime.now()``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.notifications import (
    SqliteNotificationsRepository,
)

# ---------------------------------------------------------------------------
# Fake Clock
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """Clock fake that always returns a fixed UTC instant.

    ``advance(delta)`` advances the pinned time for the next ``now()`` call —
    useful to simulate time passing between operations.
    """

    def __init__(self, now: datetime = _BASE_TIME) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:  # satisfy Clock Protocol shape
        return 0.0

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOT_ID = 42
CHANNEL = "email"
RECIPIENT = "test@example.com"


def _make_repo(
    tmp_db: ConnectionProvider,
    clock: FixedClock | None = None,
) -> SqliteNotificationsRepository:
    if clock is None:
        clock = FixedClock()
    return SqliteNotificationsRepository(tmp_db, clock)


def _direct_fetch(tmp_db: ConnectionProvider) -> tuple | None:
    """Read raw row from DB for assertion — bypasses repo layer."""
    conn = tmp_db.get_connection()
    cur = conn.execute(
        "SELECT lot_id, channel, recipient, status, attempt_no,"
        "       last_attempt_at, sent_at"
        " FROM notifications"
        " WHERE lot_id = ? AND channel = ? AND recipient = ?",
        (LOT_ID, CHANNEL, RECIPIENT),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReserve:
    def test_reserve_creates_row_returns_true(self, tmp_db: ConnectionProvider) -> None:
        """reserve() on a fresh key inserts a row and returns True."""
        repo = _make_repo(tmp_db)
        result = repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        assert result is True
        row = _direct_fetch(tmp_db)
        assert row is not None
        lot_id, channel, _recipient, status, attempt_no, last_attempt_at, sent_at = row
        assert lot_id == LOT_ID
        assert channel == CHANNEL
        assert status == "pending"
        assert attempt_no == 0
        assert last_attempt_at is None
        assert sent_at is None

    def test_reserve_second_call_returns_false_row_unchanged(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Second reserve() on same key returns False and does NOT change the row."""
        repo = _make_repo(tmp_db)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        result = repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        assert result is False
        row = _direct_fetch(tmp_db)
        assert row is not None
        _, _, _, status, attempt_no, *_ = row
        assert status == "pending"
        assert attempt_no == 0


class TestStatusOf:
    def test_status_of_no_row_returns_none(self, tmp_db: ConnectionProvider) -> None:
        repo = _make_repo(tmp_db)
        result = repo.status_of(LOT_ID, CHANNEL, RECIPIENT)
        assert result is None

    def test_status_of_after_reserve_returns_pending(
        self, tmp_db: ConnectionProvider
    ) -> None:
        repo = _make_repo(tmp_db)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        assert repo.status_of(LOT_ID, CHANNEL, RECIPIENT) == "pending"


class TestMarkAttempt:
    def test_mark_attempt_increments_attempt_no(
        self, tmp_db: ConnectionProvider
    ) -> None:
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        result = repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        assert result == 1
        row = _direct_fetch(tmp_db)
        assert row is not None
        _, _, _, status, attempt_no, last_attempt_at, _ = row
        assert status == "pending"
        assert attempt_no == 1
        assert last_attempt_at is not None

    def test_mark_attempt_twice_increments_to_two(
        self, tmp_db: ConnectionProvider
    ) -> None:
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())
        clock.advance(timedelta(minutes=5))
        result = repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        assert result == 2
        row = _direct_fetch(tmp_db)
        assert row is not None
        _, _, _, _, attempt_no, *_ = row
        assert attempt_no == 2

    def test_mark_attempt_after_mark_sent_returns_none_race(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """R4-C4: mark_attempt on a terminal (sent) row returns None."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())
        repo.mark_sent(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        result = repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        assert result is None

    def test_mark_attempt_after_mark_permanent_fail_returns_none(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """R4-C4: mark_attempt on a terminal (permanent_fail) row returns None."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())
        repo.mark_permanent_fail(LOT_ID, CHANNEL, RECIPIENT)

        result = repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        assert result is None


class TestMarkSent:
    def test_mark_sent_after_mark_attempt(self, tmp_db: ConnectionProvider) -> None:
        """mark_sent transitions status to 'sent' and sets sent_at."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        sent_time = clock.now()
        repo.mark_sent(LOT_ID, CHANNEL, RECIPIENT, at=sent_time)

        row = _direct_fetch(tmp_db)
        assert row is not None
        _, _, _, status, _, _, sent_at = row
        assert status == "sent"
        assert sent_at is not None
        assert datetime.fromisoformat(sent_at) == sent_time


class TestMarkPermanentFail:
    def test_mark_permanent_fail_after_mark_attempt(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """mark_permanent_fail transitions status to 'permanent_fail'."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        repo.mark_permanent_fail(LOT_ID, CHANNEL, RECIPIENT)

        row = _direct_fetch(tmp_db)
        assert row is not None
        _, _, _, status, *_ = row
        assert status == "permanent_fail"


class TestListPendingOlderThan:
    def test_does_not_return_sent_or_permanent_fail(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """list_pending_older_than excludes terminal-status rows."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)

        # Sent row
        repo.reserve(LOT_ID, "email", "sent@example.com")
        repo.mark_attempt(LOT_ID, "email", "sent@example.com", at=clock.now())
        repo.mark_sent(LOT_ID, "email", "sent@example.com", at=clock.now())

        # permanent_fail row
        repo.reserve(LOT_ID, "email", "fail@example.com")
        repo.mark_attempt(LOT_ID, "email", "fail@example.com", at=clock.now())
        repo.mark_permanent_fail(LOT_ID, "email", "fail@example.com")

        # Advance clock so the pending row would qualify if one existed
        clock.advance(timedelta(hours=1))

        results = repo.list_pending_older_than(timedelta(minutes=1))
        assert results == []

    def test_returns_zombie_reserve_last_attempt_at_is_null(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """R4-C3: zombie-reserve (last_attempt_at IS NULL) is included."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)

        # Reserve only — no mark_attempt, so last_attempt_at stays NULL
        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)

        # Advance clock significantly — age parameter is just 1 minute
        clock.advance(timedelta(hours=2))

        results = repo.list_pending_older_than(timedelta(minutes=1))
        assert len(results) == 1
        rec = results[0]
        assert rec.lot_id == LOT_ID
        assert rec.status == "pending"
        assert rec.last_attempt_at is None

    def test_filters_by_cutoff_correctly(self, tmp_db: ConnectionProvider) -> None:
        """Rows with last_attempt_at AFTER cutoff are NOT returned."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)

        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        attempt_time = clock.now()
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=attempt_time)

        # Advance clock by only 30 seconds — age=1min means cutoff is 30s ago
        # last_attempt_at is 30s before clock.now(), so it's AFTER the 1-min cutoff
        clock.advance(timedelta(seconds=30))

        results = repo.list_pending_older_than(timedelta(minutes=1))
        assert results == []

    def test_returns_pending_older_than_cutoff(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Rows with last_attempt_at before cutoff are returned."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)

        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=clock.now())

        # Advance well past the age threshold
        clock.advance(timedelta(minutes=10))

        results = repo.list_pending_older_than(timedelta(minutes=1))
        assert len(results) == 1
        assert results[0].lot_id == LOT_ID

    def test_round_trip_preserves_timezone(self, tmp_db: ConnectionProvider) -> None:
        """Blocker fix: datetime read back from DB retains UTC tzinfo (not naive)."""
        aware_time = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
        clock = FixedClock(now=aware_time)
        repo = _make_repo(tmp_db, clock)

        repo.reserve(LOT_ID, CHANNEL, RECIPIENT)
        repo.mark_attempt(LOT_ID, CHANNEL, RECIPIENT, at=aware_time)

        # Advance clock so the row qualifies for the recovery query
        clock.advance(timedelta(hours=1))

        results = repo.list_pending_older_than(timedelta(minutes=1))
        assert len(results) == 1
        rec = results[0]
        assert rec.last_attempt_at is not None
        assert rec.last_attempt_at.tzinfo is not None, (
            "last_attempt_at must be timezone-aware after DB round-trip"
        )


class TestListRecent:
    def test_list_recent_sorts_by_sent_at_desc(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """list_recent returns sent rows ordered by sent_at DESC."""
        clock = FixedClock()
        repo = _make_repo(tmp_db, clock)

        # First notification sent at t=0
        repo.reserve(LOT_ID, "email", "first@example.com")
        repo.mark_attempt(LOT_ID, "email", "first@example.com", at=clock.now())
        sent_first = clock.now()
        repo.mark_sent(LOT_ID, "email", "first@example.com", at=sent_first)

        # Second notification sent at t=+1min
        clock.advance(timedelta(minutes=1))
        repo.reserve(LOT_ID, "email", "second@example.com")
        repo.mark_attempt(LOT_ID, "email", "second@example.com", at=clock.now())
        sent_second = clock.now()
        repo.mark_sent(LOT_ID, "email", "second@example.com", at=sent_second)

        results = repo.list_recent(limit=10)

        assert len(results) == 2
        # Most recent first
        assert results[0].recipient == "second@example.com"
        assert results[1].recipient == "first@example.com"
        assert results[0].sent_at == sent_second
        assert results[1].sent_at == sent_first
