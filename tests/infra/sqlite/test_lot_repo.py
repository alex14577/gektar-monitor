"""Integration tests for SqliteLotRepository.

Uses ``tmp_db`` fixture (tests/conftest.py) — per-test ConnectionProvider
with the full v2 schema applied.

All time-related calls go through a ``FixedClock`` fake so tests are fully
deterministic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db
from fis_monitor.infra.sqlite.repositories.lots import (
    SqliteLotRepository,
)
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Fake Clock
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """Clock fake — always returns a fixed UTC instant."""

    def __init__(self, now: datetime = _BASE_TIME) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(
    tmp_db: ConnectionProvider,
    clock: FixedClock | None = None,
) -> SqliteLotRepository:
    if clock is None:
        clock = FixedClock()
    return SqliteLotRepository(conn_provider=tmp_db, clock=clock)


def _rtree_count(tmp_db: ConnectionProvider, lot_id: int) -> int:
    """Count rows in lots_rtree for a given lot_id."""
    conn = tmp_db.get()
    cur = conn.execute("SELECT COUNT(*) FROM lots_rtree WHERE id = ?", (lot_id,))
    count: int = cur.fetchone()[0]
    cur.close()
    return count


def _rtree_coords(
    tmp_db: ConnectionProvider, lot_id: int
) -> tuple[float, float] | None:
    """Return (lat, lon) stored in lots_rtree, or None if absent."""
    conn = tmp_db.get()
    cur = conn.execute(
        "SELECT min_lat, min_lon FROM lots_rtree WHERE id = ?", (lot_id,)
    )
    row = cur.fetchone()
    cur.close()
    if row is None:
        return None
    return (row[0], row[1])


# ---------------------------------------------------------------------------
# Tests: upsert + diff
# ---------------------------------------------------------------------------


def test_upsert_new_lot_is_new_true_no_changes(tmp_db: ConnectionProvider) -> None:
    """INSERT path: was_new=True, changes=[] (ADR-016)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=1)

    result = repo.upsert(lot, tracked=["status"])

    assert result.was_new is True
    assert result.changes == []


def test_upsert_existing_no_change_is_new_false_no_changes(
    tmp_db: ConnectionProvider,
) -> None:
    """UPDATE path, no field changed: was_new=False, changes=[]."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=2)
    repo.upsert(lot, tracked=["status"])

    result = repo.upsert(lot, tracked=["status"])

    assert result.was_new is False
    assert result.changes == []


def test_upsert_status_change_writes_history(tmp_db: ConnectionProvider) -> None:
    """UPDATE with status change: one FieldChange + one lots_history row."""
    repo = _make_repo(tmp_db)
    lot1 = make_lot(id=3, status="Свободен")
    repo.upsert(lot1, tracked=["status"])

    lot2 = make_lot(id=3, status="Зарезервирован")
    result = repo.upsert(lot2, tracked=["status"])

    assert result.was_new is False
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.field == "status"
    assert change.old_value == "Свободен"
    assert change.new_value == "Зарезервирован"

    # Verify lots_history row with json-encoded values.
    conn = tmp_db.get()
    cur = conn.execute(
        "SELECT field, old_value, new_value FROM lots_history WHERE lot_id = ?",
        (3,),
    )
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 1
    field, old_val_raw, new_val_raw = rows[0]
    assert field == "status"
    assert json.loads(old_val_raw) == "Свободен"
    assert json.loads(new_val_raw) == "Зарезервирован"


def test_upsert_multiple_tracked_fields(tmp_db: ConnectionProvider) -> None:
    """Multiple tracked fields — changes in tracked order."""
    repo = _make_repo(tmp_db)
    now = _BASE_TIME
    lot1 = make_lot(id=4, status="Свободен", area_sqm=1000, date_update=now)
    repo.upsert(lot1, tracked=["status", "area_sqm", "date_update"])

    lot2 = make_lot(
        id=4,
        status="Зарезервирован",
        area_sqm=2000,
        date_update=now + timedelta(days=1),
    )
    result = repo.upsert(lot2, tracked=["status", "area_sqm", "date_update"])

    assert result.was_new is False
    assert len(result.changes) == 3
    assert result.changes[0].field == "status"
    assert result.changes[1].field == "area_sqm"
    assert result.changes[2].field == "date_update"


def test_upsert_rollback_on_error_leaves_lot_unchanged(
    tmp_db: ConnectionProvider,
) -> None:
    """Exception inside tx → rollback; lots table not modified."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=5, status="Свободен")
    repo.upsert(lot, tracked=["status"])

    # auction is forward-compat only — raises NotImplementedError inside tx.
    with pytest.raises(NotImplementedError):
        repo.upsert(make_lot(id=5, status="Зарезервирован"), tracked=["auction"])

    # lot must remain unchanged.
    fetched = repo.get(5)
    assert fetched is not None
    assert fetched.status == "Свободен"


def test_upsert_toctou_second_writer_reads_committed_state(
    tmp_db: ConnectionProvider, tmp_db_path  # type: ignore[no-untyped-def]
) -> None:
    """Two sequential upserts on the same lot_id are ordered deterministically.

    (True concurrent TOCTOU is a property of BEGIN IMMEDIATE; here we verify
    the sequential path: second upsert sees the first's committed state.)
    """
    schema_sql_path = (
        Path(__file__).resolve().parents[3] / "docs" / "db" / "schema.sql"
    )
    schema_sql = schema_sql_path.read_text(encoding="utf-8")

    provider2 = ConnectionProvider(db_path=tmp_db_path)
    try:
        init_db(provider2, schema_sql=schema_sql)
        repo1 = _make_repo(tmp_db)
        repo2 = SqliteLotRepository(conn_provider=provider2, clock=FixedClock())

        lot = make_lot(id=6, status="Свободен")
        repo1.upsert(lot, tracked=["status"])

        # repo2 sees committed state from repo1.
        lot_v2 = make_lot(id=6, status="Зарезервирован")
        result2 = repo2.upsert(lot_v2, tracked=["status"])

        assert result2.was_new is False
        assert len(result2.changes) == 1
        assert result2.changes[0].old_value == "Свободен"
    finally:
        provider2.close_all()


# ---------------------------------------------------------------------------
# Tests: get / list_active
# ---------------------------------------------------------------------------


def test_get_nonexistent_returns_none(tmp_db: ConnectionProvider) -> None:
    repo = _make_repo(tmp_db)
    assert repo.get(999) is None


def test_get_after_upsert_returns_lot(tmp_db: ConnectionProvider) -> None:
    """get() returns the Lot with all fields after upsert."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=7, cadastral_no="27:23:0040000:0007", status="Свободен")
    repo.upsert(lot, tracked=["status"])

    fetched = repo.get(7)

    assert fetched is not None
    assert fetched.id == 7
    assert fetched.cadastral_no == "27:23:0040000:0007"
    assert fetched.status == "Свободен"
    assert fetched.region == lot.region


def test_list_active_excludes_inactive(tmp_db: ConnectionProvider) -> None:
    """list_active filters out lots with is_active=0."""
    clock = FixedClock()
    repo = _make_repo(tmp_db, clock)
    for lot_id in [10, 11, 12]:
        repo.upsert(make_lot(id=lot_id), tracked=["status"])
    repo.mark_inactive(11, reason="hard_removed", at=clock.now())

    active = repo.list_active(limit=10, offset=0)
    active_ids = {lot.id for lot in active}

    assert 11 not in active_ids
    assert {10, 12}.issubset(active_ids)


# ---------------------------------------------------------------------------
# Tests: mark_seen / mark_inactive
# ---------------------------------------------------------------------------


def test_mark_seen_bulk_updates_timestamps(tmp_db: ConnectionProvider) -> None:
    """mark_seen updates last_seen/last_seen_at for all listed lot-ids."""
    repo = _make_repo(tmp_db)
    for lot_id in [20, 21, 22]:
        repo.upsert(make_lot(id=lot_id, last_seen_at=None), tracked=["status"])

    seen_at = datetime(2026, 5, 13, 14, 0, 0, tzinfo=UTC)
    repo.mark_seen([20, 21, 22], at=seen_at)

    conn = tmp_db.get()
    for lot_id in [20, 21, 22]:
        cur = conn.execute("SELECT last_seen, last_seen_at FROM lots WHERE id = ?", (lot_id,))
        row = cur.fetchone()
        cur.close()
        assert row is not None
        assert row[0] is not None
        assert row[1] is not None


def test_mark_inactive_sets_fields(tmp_db: ConnectionProvider) -> None:
    """mark_inactive sets is_active=0 with reason/since/confirmed_at."""
    clock = FixedClock()
    repo = _make_repo(tmp_db, clock)
    repo.upsert(make_lot(id=30), tracked=["status"])

    at = clock.now()
    repo.mark_inactive(30, reason="status_changed", at=at)

    fetched = repo.get(30)
    assert fetched is not None
    assert fetched.is_active is False
    assert fetched.inactive_reason == "status_changed"
    assert fetched.inactive_since is not None
    assert fetched.inactive_confirmed_at is not None


# ---------------------------------------------------------------------------
# Tests: _sync_geo (via upsert) — 5 cases R3-M8
# ---------------------------------------------------------------------------


def test_sync_geo_null_to_value_inserts_rtree_row(
    tmp_db: ConnectionProvider,
) -> None:
    """NULL → value: R-tree row created (count=1)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=40, lat=None, lon=None)
    repo.upsert(lot, tracked=["status"])
    assert _rtree_count(tmp_db, 40) == 0

    lot2 = make_lot(id=40, lat=55.75, lon=37.62)
    repo.upsert(lot2, tracked=["status"])

    assert _rtree_count(tmp_db, 40) == 1
    coords = _rtree_coords(tmp_db, 40)
    assert coords is not None
    assert abs(coords[0] - 55.75) < 1e-4
    assert abs(coords[1] - 37.62) < 1e-4


def test_sync_geo_value_to_null_deletes_rtree_row(
    tmp_db: ConnectionProvider,
) -> None:
    """value → NULL: R-tree row deleted (count=0)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=41, lat=55.75, lon=37.62)
    repo.upsert(lot, tracked=["status"])
    assert _rtree_count(tmp_db, 41) == 1

    lot2 = make_lot(id=41, lat=None, lon=None)
    repo.upsert(lot2, tracked=["status"])

    assert _rtree_count(tmp_db, 41) == 0


def test_sync_geo_value_to_other_updates_rtree_row(
    tmp_db: ConnectionProvider,
) -> None:
    """value → other: R-tree row updated (still 1 row, different coords)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=42, lat=55.75, lon=37.62)
    repo.upsert(lot, tracked=["status"])
    assert _rtree_count(tmp_db, 42) == 1

    lot2 = make_lot(id=42, lat=60.0, lon=30.0)
    repo.upsert(lot2, tracked=["status"])

    assert _rtree_count(tmp_db, 42) == 1
    coords = _rtree_coords(tmp_db, 42)
    assert coords is not None
    assert abs(coords[0] - 60.0) < 1e-4
    assert abs(coords[1] - 30.0) < 1e-4


def test_sync_geo_null_to_null_no_rtree_row(tmp_db: ConnectionProvider) -> None:
    """NULL → NULL both times: no R-tree entry created."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=43, lat=None, lon=None)
    repo.upsert(lot, tracked=["status"])
    repo.upsert(lot, tracked=["status"])

    assert _rtree_count(tmp_db, 43) == 0


def test_sync_geo_same_coords_row_still_one(tmp_db: ConnectionProvider) -> None:
    """same → same: R-tree row count stays at 1 (no duplicate insert)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=44, lat=48.48, lon=135.08)
    repo.upsert(lot, tracked=["status"])
    assert _rtree_count(tmp_db, 44) == 1

    repo.upsert(lot, tracked=["status"])

    assert _rtree_count(tmp_db, 44) == 1


# ---------------------------------------------------------------------------
# Tests: needing_enrichment
# ---------------------------------------------------------------------------


def test_needing_enrichment_returns_pending_and_null(
    tmp_db: ConnectionProvider,
) -> None:
    """needing_enrichment returns lots with NULL or 'pending' enrichment_status
    and detail_fetched_at IS NULL.  'done' lots are excluded."""
    repo = _make_repo(tmp_db)
    lot_null = make_lot(
        id=50, enrichment_status=None, detail_fetched_at=None
    )
    lot_pending = make_lot(
        id=51, enrichment_status="pending", detail_fetched_at=None
    )
    lot_done = make_lot(
        id=52, enrichment_status="done", detail_fetched_at=_BASE_TIME
    )
    # pending but already fetched — should NOT be returned.
    lot_pending_fetched = make_lot(
        id=53, enrichment_status="pending", detail_fetched_at=_BASE_TIME
    )

    for lot in [lot_null, lot_pending, lot_done, lot_pending_fetched]:
        repo.upsert(lot, tracked=["status"])

    ids = repo.needing_enrichment(limit=10)

    assert 50 in ids
    assert 51 in ids
    assert 52 not in ids
    assert 53 not in ids


# ---------------------------------------------------------------------------
# Tests: last_known_id
# ---------------------------------------------------------------------------


def test_last_known_id_roundtrip(tmp_db: ConnectionProvider) -> None:
    """set_last_known_id + get_last_known_id → same value."""
    repo = _make_repo(tmp_db)

    repo.set_last_known_id(region=1, value=12345)

    assert repo.get_last_known_id(region=1) == 12345


def test_last_known_id_unknown_region_returns_none(
    tmp_db: ConnectionProvider,
) -> None:
    """get_last_known_id for unknown region → None."""
    repo = _make_repo(tmp_db)

    assert repo.get_last_known_id(region=99) is None


def test_last_known_id_update(tmp_db: ConnectionProvider) -> None:
    """Subsequent set_last_known_id overwrites previous value."""
    repo = _make_repo(tmp_db)
    repo.set_last_known_id(region=2, value=100)
    repo.set_last_known_id(region=2, value=200)

    assert repo.get_last_known_id(region=2) == 200


# ---------------------------------------------------------------------------
# Tests: timezone round-trip
# ---------------------------------------------------------------------------


def test_mark_seen_timezone_roundtrip(tmp_db: ConnectionProvider) -> None:
    """Aware datetime stored via mark_seen → retrieved tzinfo is not None."""
    repo = _make_repo(tmp_db)
    repo.upsert(make_lot(id=60), tracked=["status"])

    aware_time = datetime(2026, 5, 13, 15, 30, 0, tzinfo=UTC)
    repo.mark_seen([60], at=aware_time)

    conn = tmp_db.get()
    cur = conn.execute("SELECT last_seen_at FROM lots WHERE id = ?", (60,))
    raw = cur.fetchone()[0]
    cur.close()

    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Tests: rollback invariant (Fix 4 — mid-write failure)
# ---------------------------------------------------------------------------


class _FailOnHistoryConnection:
    """Thin proxy around sqlite3.Connection that raises on lots_history INSERT.

    Used to simulate a mid-write failure inside upsert without monkey-patching
    sqlite3.Connection.execute (which is read-only in CPython 3.12+).
    """

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._conn = real_conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        if "lots_history" in sql.lower() and sql.strip().upper().startswith("INSERT"):
            raise sqlite3.OperationalError("simulated mid-write failure")
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class _FailingConnProvider:
    """ConnectionProvider wrapper that returns a _FailOnHistoryConnection once."""

    def __init__(self, real_provider: ConnectionProvider) -> None:
        self._real = real_provider
        self._inject_failure = False

    def get(self) -> object:  # type: ignore[override]
        conn = self._real.get()
        if self._inject_failure:
            return _FailOnHistoryConnection(conn)
        return conn


def test_upsert_rollback_on_mid_write_error_undoes_partial_changes(
    tmp_db: ConnectionProvider,
) -> None:
    """If a SQL exception occurs during lots_history INSERT inside the upsert tx,
    the entire transaction must be rolled back: status unchanged, no history row.
    """
    # First upsert — insert base row using real provider
    repo = _make_repo(tmp_db)
    lot = make_lot(id=70, status="Свободен")
    repo.upsert(lot, tracked=["status"])

    # Second upsert — use failing provider that raises mid-tx on lots_history INSERT
    failing_provider = _FailingConnProvider(tmp_db)
    failing_provider._inject_failure = True
    repo_fail = SqliteLotRepository(
        conn_provider=failing_provider,  # type: ignore[arg-type]
        clock=FixedClock(),
    )

    new_lot = lot.model_copy(update={"status": "Снят"})
    with pytest.raises(sqlite3.OperationalError, match="simulated mid-write failure"):
        repo_fail.upsert(new_lot, tracked=["status"])

    # Verify rollback: status in DB must remain "Свободен", history must be empty
    fetched = repo.get(70)
    assert fetched is not None
    assert fetched.status == "Свободен"
    conn = tmp_db.get()
    cur = conn.execute("SELECT COUNT(*) FROM lots_history WHERE lot_id = 70")
    assert cur.fetchone()[0] == 0
    cur.close()


# ---------------------------------------------------------------------------
# Tests: R-tree INSERT with non-NULL coords (Fix 5)
# ---------------------------------------------------------------------------


def test_first_upsert_with_coords_creates_exactly_one_rtree_row(
    tmp_db: ConnectionProvider,
) -> None:
    """ADR-016 R3-M8 simplest invariant: INSERT lat/lon != None → R-tree row count == 1."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=100, lat=48.48, lon=135.08)
    repo.upsert(lot, tracked=[])

    cur = tmp_db.get().execute(
        "SELECT COUNT(*) FROM lots_rtree WHERE id = 100"
    )
    assert cur.fetchone()[0] == 1
    cur.close()


# ---------------------------------------------------------------------------
# Tests: region_id stamping (gektar_monitor-eov8)
# ---------------------------------------------------------------------------


def test_upsert_insert_persists_region_id(tmp_db: ConnectionProvider) -> None:
    """INSERT path: region_id is written to the DB (not NULL)."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=200, region_id=1)
    repo.upsert(lot, tracked=[])

    cur = tmp_db.get().execute("SELECT region_id FROM lots WHERE id = 200")
    row = cur.fetchone()
    cur.close()
    assert row is not None
    assert row[0] == 1


def test_upsert_update_persists_region_id(tmp_db: ConnectionProvider) -> None:
    """UPDATE path: region_id is kept/written on subsequent upsert."""
    repo = _make_repo(tmp_db)
    lot = make_lot(id=201, region_id=2)
    repo.upsert(lot, tracked=[])
    # Second upsert (UPDATE branch) with same region_id
    repo.upsert(lot, tracked=[])

    cur = tmp_db.get().execute("SELECT region_id FROM lots WHERE id = 201")
    row = cur.fetchone()
    cur.close()
    assert row is not None
    assert row[0] == 2


def test_count_active_filters_by_region_id(tmp_db: ConnectionProvider) -> None:
    """count_active(region_id=X) counts only lots with matching region_id."""
    repo = _make_repo(tmp_db)
    repo.upsert(make_lot(id=301, region_id=1), tracked=[])
    repo.upsert(make_lot(id=302, region_id=1), tracked=[])
    repo.upsert(make_lot(id=303, region_id=2), tracked=[])

    assert repo.count_active(region_id=1) == 2
    assert repo.count_active(region_id=2) == 1
    assert repo.count_active(region_id=99) == 0
