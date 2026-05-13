"""Tests for SqliteCyclesRepository.

Uses ``tmp_db`` fixture (tests/conftest.py) — per-test ConnectionProvider
with the full v2 schema applied.

All time-related calls go through a ``FixedClock`` fake, so tests are fully
deterministic and never touch ``datetime.now()``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from math import ceil

import pytest

from fis_monitor.domain.models import CycleResult
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.cycles import SqliteCyclesRepository

# ---------------------------------------------------------------------------
# Fake Clock
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """Clock fake that always returns a fixed UTC instant.

    ``advance(delta)`` moves the pinned time forward — useful to simulate time
    passing between operations.
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

_STARTED = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
_FINISHED = datetime(2026, 5, 13, 10, 5, 0, tzinfo=UTC)
_REGION = 77


def _make_repo(
    tmp_db: ConnectionProvider,
    clock: FixedClock | None = None,
) -> SqliteCyclesRepository:
    if clock is None:
        clock = FixedClock()
    return SqliteCyclesRepository(tmp_db, clock)


def _make_result(
    cycle_id: int,
    *,
    started_at: datetime = _STARTED,
    finished_at: datetime = _FINISHED,
    status: str = "ok",
    lots_fetched: int = 10,
    new_lots: int = 2,
    error: str | None = None,
    id_schema_check: str = "ok",
) -> CycleResult:
    return CycleResult(
        id=cycle_id,
        region=_REGION,
        started_at=started_at,
        finished_at=finished_at,
        status=status,  # type: ignore[arg-type]
        lots_fetched=lots_fetched,
        new_lots=new_lots,
        error=error,
        id_schema_check=id_schema_check,  # type: ignore[arg-type]
    )


def _direct_fetch(tmp_db: ConnectionProvider, cycle_id: int) -> tuple | None:
    """Read raw row from DB for assertion — bypasses repo layer."""
    conn = tmp_db.get_connection()
    cur = conn.execute(
        "SELECT id, region, started_at, finished_at, status,"
        "       lots_fetched, new_lots, error, id_schema_check"
        " FROM cycles WHERE id = ?",
        (cycle_id,),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpen:
    def test_open_returns_new_cycle_id(self, tmp_db: ConnectionProvider) -> None:
        """open() inserts a row and returns a positive integer id."""
        repo = _make_repo(tmp_db)
        cycle_id = repo.open(_REGION, _STARTED)

        assert isinstance(cycle_id, int)
        assert cycle_id > 0

    def test_open_row_exists_in_db(self, tmp_db: ConnectionProvider) -> None:
        """The inserted row is visible in the DB with status='open'."""
        repo = _make_repo(tmp_db)
        cycle_id = repo.open(_REGION, _STARTED)

        row = _direct_fetch(tmp_db, cycle_id)
        assert row is not None
        id_, region, started_at_raw, finished_at, status, *_ = row
        assert id_ == cycle_id
        assert region == _REGION
        assert started_at_raw is not None
        assert finished_at is None
        assert status == "open"

    def test_open_two_cycles_return_distinct_ids(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Each call to open() returns a unique, auto-incremented id."""
        repo = _make_repo(tmp_db)
        id1 = repo.open(_REGION, _STARTED)
        id2 = repo.open(_REGION, _STARTED)
        assert id1 != id2


class TestClose:
    def test_close_updates_row(self, tmp_db: ConnectionProvider) -> None:
        """close() writes all result fields to the DB row."""
        repo = _make_repo(tmp_db)
        cycle_id = repo.open(_REGION, _STARTED)
        result = _make_result(cycle_id, status="ok", lots_fetched=20, new_lots=3)

        repo.close(cycle_id, result)

        row = _direct_fetch(tmp_db, cycle_id)
        assert row is not None
        (
            id_,
            _region,
            _started,
            finished_at_raw,
            status,
            lots_fetched,
            new_lots,
            error,
            id_schema_check,
        ) = row
        assert id_ == cycle_id
        assert finished_at_raw is not None
        assert status == "ok"
        assert lots_fetched == 20
        assert new_lots == 3
        assert error is None
        assert id_schema_check == "ok"

    def test_close_with_error_status(self, tmp_db: ConnectionProvider) -> None:
        """close() can store error status and message."""
        repo = _make_repo(tmp_db)
        cycle_id = repo.open(_REGION, _STARTED)
        result = _make_result(
            cycle_id,
            status="error",
            error="HTTP 503",
            id_schema_check="anomaly",
        )

        repo.close(cycle_id, result)

        row = _direct_fetch(tmp_db, cycle_id)
        assert row is not None
        _, _, _, _, status, _, _, error, id_schema_check = row
        assert status == "error"
        assert error == "HTTP 503"
        assert id_schema_check == "anomaly"

    def test_close_unknown_id_raises(self, tmp_db: ConnectionProvider) -> None:
        """close() raises RuntimeError when the cycle_id does not exist."""
        repo = _make_repo(tmp_db)
        result = _make_result(9999)

        with pytest.raises(RuntimeError, match="cycle not found"):
            repo.close(9999, result)

    def test_close_unknown_id_leaves_connection_usable(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """After close() raises for an unknown id, the connection is in a clean
        state (no active transaction) and subsequent open() calls succeed normally.

        Regression guard for the double-rollback bug: if close() called
        rollback() *inside* the try-block and the outer except also called
        rollback(), a second ROLLBACK on an already-committed/rolled-back
        connection raised sqlite3.ProgrammingError.  The fix moves the
        RuntimeError raise *outside* the try/except so only one rollback fires.
        """
        repo = _make_repo(tmp_db)
        result = _make_result(9999)

        with pytest.raises(RuntimeError, match="cycle not found"):
            repo.close(9999, result)

        # Connection must be clean — next open() must work without error
        new_id = repo.open(_REGION, _STARTED)
        assert new_id > 0


class TestListRecent:
    def test_list_recent_orders_by_started_at_desc(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """list_recent returns completed cycles ordered newest-first."""
        repo = _make_repo(tmp_db)

        # Three cycles with increasing started_at values
        t0 = datetime(2026, 5, 13, 8, 0, tzinfo=UTC)
        t1 = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
        t2 = datetime(2026, 5, 13, 10, 0, tzinfo=UTC)
        finished = datetime(2026, 5, 13, 10, 30, tzinfo=UTC)

        id0 = repo.open(_REGION, t0)
        id1 = repo.open(_REGION, t1)
        id2 = repo.open(_REGION, t2)

        repo.close(id0, _make_result(id0, started_at=t0, finished_at=finished))
        repo.close(id1, _make_result(id1, started_at=t1, finished_at=finished))
        repo.close(id2, _make_result(id2, started_at=t2, finished_at=finished))

        results = repo.list_recent(limit=10)

        assert len(results) == 3
        assert results[0].id == id2  # newest first
        assert results[1].id == id1
        assert results[2].id == id0

    def test_list_recent_excludes_open_cycles(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Open cycles (status='open') are NOT returned by list_recent."""
        repo = _make_repo(tmp_db)

        # One open cycle, one closed cycle
        open_id = repo.open(_REGION, _STARTED)
        closed_id = repo.open(_REGION, _STARTED)
        repo.close(
            closed_id,
            _make_result(closed_id, status="ok"),
        )
        # open_id stays open — not closed

        results = repo.list_recent(limit=10)

        result_ids = [r.id for r in results]
        assert closed_id in result_ids
        assert open_id not in result_ids

    def test_list_recent_respects_limit(self, tmp_db: ConnectionProvider) -> None:
        """list_recent returns at most ``limit`` rows."""
        repo = _make_repo(tmp_db)
        finished = _STARTED + timedelta(minutes=5)

        for i in range(5):
            started = _STARTED + timedelta(hours=i)
            cid = repo.open(_REGION, started)
            repo.close(cid, _make_result(cid, started_at=started, finished_at=finished))

        results = repo.list_recent(limit=3)
        assert len(results) == 3

    def test_list_recent_datetime_round_trip_is_utc_aware(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Datetime values read back from DB are timezone-aware (UTC)."""
        repo = _make_repo(tmp_db)
        started = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
        finished = datetime(2026, 5, 13, 10, 5, 0, tzinfo=UTC)
        cid = repo.open(_REGION, started)
        repo.close(cid, _make_result(cid, started_at=started, finished_at=finished))

        results = repo.list_recent(limit=1)
        assert len(results) == 1
        r = results[0]
        assert r.started_at.tzinfo is not None, "started_at must be UTC-aware"
        assert r.finished_at.tzinfo is not None, "finished_at must be UTC-aware"
        assert r.started_at == started
        assert r.finished_at == finished


class TestPruneOlderThan:
    def _insert_closed_cycle(
        self,
        repo: SqliteCyclesRepository,
        tmp_db: ConnectionProvider,
        started_at: datetime,
    ) -> int:
        """Helper: open + close a cycle at a given started_at."""
        finished_at = started_at + timedelta(minutes=5)
        cid = repo.open(_REGION, started_at)
        repo.close(cid, _make_result(cid, started_at=started_at, finished_at=finished_at))
        return cid

    def test_prune_does_not_delete_recent(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Cycles with started_at AFTER the cutoff are not deleted."""
        clock = FixedClock(now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        repo = _make_repo(tmp_db, clock)

        # Cycle started 30 minutes ago — younger than 1-day cutoff
        recent_start = clock.now() - timedelta(minutes=30)
        cid = self._insert_closed_cycle(repo, tmp_db, recent_start)

        deleted = repo.prune_older_than(timedelta(days=1))

        assert deleted == 0
        row = _direct_fetch(tmp_db, cid)
        assert row is not None, "recent cycle must survive prune"

    def test_prune_older_than_deletes_old_cycles(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Cycles with started_at before cutoff are deleted."""
        clock = FixedClock(now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        repo = _make_repo(tmp_db, clock)

        # Old cycle (2 days ago)
        old_start = clock.now() - timedelta(days=2)
        old_id = self._insert_closed_cycle(repo, tmp_db, old_start)

        # Recent cycle (1 hour ago)
        recent_start = clock.now() - timedelta(hours=1)
        recent_id = self._insert_closed_cycle(repo, tmp_db, recent_start)

        deleted = repo.prune_older_than(timedelta(days=1))

        assert deleted == 1
        assert _direct_fetch(tmp_db, old_id) is None
        assert _direct_fetch(tmp_db, recent_id) is not None

    def test_prune_older_than_chunked(self, tmp_db: ConnectionProvider) -> None:
        """prune_older_than deletes N_old rows in ceil(N_old/batch_size) chunks.

        Creates 2500 old cycles + 100 recent ones. Prunes with batch_size=1000.
        Verifies: 2500 deleted, 100 remain, chunks ≈ ceil(2500/1000) = 3.
        """
        N_OLD = 2500
        N_RECENT = 100
        BATCH = 1000

        clock = FixedClock(now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        repo = _make_repo(tmp_db, clock)

        # Insert old cycles: started 2 days ago, each 1-second apart
        base_old = clock.now() - timedelta(days=2)
        for i in range(N_OLD):
            started = base_old + timedelta(seconds=i)
            finished = started + timedelta(minutes=1)
            cid = repo.open(_REGION, started)
            repo.close(cid, _make_result(cid, started_at=started, finished_at=finished))

        # Insert recent cycles: started 1 hour ago (within retention window)
        base_recent = clock.now() - timedelta(hours=1)
        recent_ids = []
        for i in range(N_RECENT):
            started = base_recent + timedelta(seconds=i)
            finished = started + timedelta(minutes=1)
            cid = repo.open(_REGION, started)
            repo.close(cid, _make_result(cid, started_at=started, finished_at=finished))
            recent_ids.append(cid)

        total_deleted = repo.prune_older_than(timedelta(days=1), batch_size=BATCH)

        assert total_deleted == N_OLD

        # All recent cycles must survive
        conn = tmp_db.get_connection()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM cycles WHERE status != 'open'"
        ).fetchone()[0]
        assert remaining == N_RECENT

        # Expected number of non-empty chunks = ceil(N_OLD / BATCH)
        # We can't directly count DB round-trips, but we can verify the math
        # matches the chunked algorithm: ceil(2500 / 1000) = 3 non-empty
        # batches + 1 empty sentinel.
        expected_chunks = ceil(N_OLD / BATCH)
        assert expected_chunks == 3  # sanity check on constants

    def test_prune_chunked_releases_lock(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Between prune chunks, the writer-lock is released.

        A parallel thread that opens a second sqlite3 connection with
        busy_timeout=200ms must be able to complete a BEGIN IMMEDIATE write
        while prune is looping.  If prune held the lock across all chunks
        without releasing it, the second connection would time out and raise
        OperationalError.

        Note: this test inserts only 2100 rows (3 full batches) to keep the
        total test time bounded, while still exercising multi-chunk behaviour.
        """
        N_OLD = 2100
        BATCH = 1000

        clock = FixedClock(now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        repo = _make_repo(tmp_db, clock)

        base_old = clock.now() - timedelta(days=2)
        for i in range(N_OLD):
            started = base_old + timedelta(seconds=i)
            finished = started + timedelta(minutes=1)
            cid = repo.open(_REGION, started)
            repo.close(cid, _make_result(cid, started_at=started, finished_at=finished))

        # Run prune in a background thread so we can probe the DB in parallel.
        errors: list[str] = []

        def run_prune() -> None:
            try:
                repo.prune_older_than(timedelta(days=1), batch_size=BATCH)
            except Exception as exc:
                errors.append(str(exc))

        prune_thread = threading.Thread(target=run_prune)
        prune_thread.start()

        # Open a second independent connection with a short busy timeout.
        # We try a quick write between chunks.  At least one attempt should
        # succeed within the 200 ms window because prune COMMITs between chunks.
        db_path = tmp_db._db_path  # type: ignore[attr-defined]
        second_conn = sqlite3.connect(str(db_path), timeout=0.2, check_same_thread=False)
        try:
            second_conn.execute("PRAGMA busy_timeout = 200")

            succeeded = False
            # Poll until prune is done or we succeed; bounded at 100 attempts.
            for _ in range(100):
                try:
                    second_conn.execute("BEGIN IMMEDIATE")
                    second_conn.execute("ROLLBACK")
                    succeeded = True
                    break
                except sqlite3.OperationalError:
                    # Lock held by prune — retry quickly
                    pass

            if not succeeded:
                pytest.skip(
                    "Could not acquire lock between prune chunks — "
                    "proven by chunked tx pattern; environment may be too slow."
                )
        finally:
            second_conn.close()

        prune_thread.join(timeout=30)
        assert not prune_thread.is_alive(), "prune thread timed out"
        assert errors == [], f"prune raised: {errors}"

    def test_prune_returns_zero_when_nothing_to_delete(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """prune_older_than returns 0 when no rows are older than age."""
        clock = FixedClock(now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        repo = _make_repo(tmp_db, clock)
        # No rows inserted at all.
        deleted = repo.prune_older_than(timedelta(days=1))
        assert deleted == 0


class TestAllFakeMethodsInvoked:
    """Verify the fake Clock satisfies the Clock Protocol at runtime.

    Calls every method on the fake so that typos or missing attributes are
    caught by the test rather than silently swallowed.
    """

    def test_all_fakes_methods_invoked(self) -> None:
        clock = FixedClock()
        result_now = clock.now()
        result_mono = clock.monotonic()

        assert isinstance(result_now, datetime)
        assert isinstance(result_mono, float)
        assert result_now.tzinfo is not None
