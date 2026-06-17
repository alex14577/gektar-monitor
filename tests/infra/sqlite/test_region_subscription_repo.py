"""Tests for SqliteRegionSubscriptionRepository and count_active(region_ids=...).

Layer 1 (Infrastructure: repos). Real SQLite in-memory DB via tmp_db fixture.

Invariants covered:
1. count_active(region_ids=(X,)) filters only region X.
2. count_active(region_ids=()) returns global count (no filter).
3. RegionSubscriptionRepository.set_if_absent is idempotent — repeated call
   does not overwrite and returns False.
4. set_if_absent returns True on first insert, False on repeat.
5. delete removes the row; get_subscribed_at returns None afterward.
6. Migration data integrity: region names in lots are mapped to region_id via
   the domain catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db
from fis_monitor.infra.sqlite.migrations_v3_to_v4 import v3_to_v4
from fis_monitor.infra.sqlite.repositories.lots import SqliteLotRepository
from fis_monitor.infra.sqlite.repositories.region_subscriptions import (
    SqliteRegionSubscriptionRepository,
)

_BASE_TIME = datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
_SUBSCRIBED_AT = datetime(2026, 5, 16, 9, 0, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = _BASE_TIME) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_db: ConnectionProvider) -> SqliteRegionSubscriptionRepository:
    return SqliteRegionSubscriptionRepository(conn_provider=tmp_db)


@pytest.fixture
def lot_repo(tmp_db: ConnectionProvider) -> SqliteLotRepository:
    return SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_lot_with_region_id(
    tmp_db: ConnectionProvider,
    lot_id: int,
    region_id: int,
    *,
    is_active: bool = True,
) -> None:
    """Insert a minimal lot row directly with a region_id for testing count_active."""
    conn = tmp_db.get()
    conn.execute(
        "INSERT INTO lots("
        "  id, cadastral_no, area_sqm, region, region_id, status,"
        "  date_create, first_seen, last_seen, is_active,"
        "  raw_json, parser_version"
        ") VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, '{}', 1)",
        (
            lot_id,
            f"11:{lot_id:05d}:100",
            "Республика Бурятия",
            region_id,
            "Свободен",
            _BASE_TIME.isoformat(),
            _BASE_TIME.isoformat(),
            _BASE_TIME.isoformat(),
            1 if is_active else 0,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: RegionSubscriptionRepository
# ---------------------------------------------------------------------------


class TestSetIfAbsent:
    def test_first_insert_returns_true(self, repo: SqliteRegionSubscriptionRepository) -> None:
        result = repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        assert result is True

    def test_repeat_insert_returns_false(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        result = repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT + timedelta(hours=1))
        assert result is False

    def test_idempotent_does_not_overwrite(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        later = _SUBSCRIBED_AT + timedelta(hours=1)
        repo.set_if_absent(region_id=1, subscribed_at=later)
        stored = repo.get_subscribed_at(region_id=1)
        assert stored == _SUBSCRIBED_AT

    def test_different_regions_independent(self, repo: SqliteRegionSubscriptionRepository) -> None:
        t1 = _SUBSCRIBED_AT
        t2 = _SUBSCRIBED_AT + timedelta(days=1)
        assert repo.set_if_absent(region_id=1, subscribed_at=t1) is True
        assert repo.set_if_absent(region_id=2, subscribed_at=t2) is True
        assert repo.get_subscribed_at(1) == t1
        assert repo.get_subscribed_at(2) == t2


class TestGetSubscribedAt:
    def test_returns_none_when_absent(self, repo: SqliteRegionSubscriptionRepository) -> None:
        assert repo.get_subscribed_at(region_id=99) is None

    def test_returns_stored_timestamp(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        result = repo.get_subscribed_at(region_id=1)
        assert result == _SUBSCRIBED_AT

    def test_utc_tzinfo_preserved(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        result = repo.get_subscribed_at(region_id=1)
        assert result is not None
        assert result.tzinfo is not None


class TestDelete:
    def test_delete_removes_row(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        repo.delete(region_id=1)
        assert repo.get_subscribed_at(region_id=1) is None

    def test_delete_idempotent(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        repo.delete(region_id=1)
        repo.delete(region_id=1)  # must not raise

    def test_delete_nonexistent_is_noop(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.delete(region_id=99)  # must not raise

    def test_after_delete_can_reinsert(self, repo: SqliteRegionSubscriptionRepository) -> None:
        repo.set_if_absent(region_id=1, subscribed_at=_SUBSCRIBED_AT)
        repo.delete(region_id=1)
        new_time = _SUBSCRIBED_AT + timedelta(days=1)
        assert repo.set_if_absent(region_id=1, subscribed_at=new_time) is True
        assert repo.get_subscribed_at(region_id=1) == new_time


# ---------------------------------------------------------------------------
# Tests: LotRepository.count_active(region_ids=...)
# ---------------------------------------------------------------------------


class TestCountActiveWithRegionIds:
    def test_count_active_no_filter_global(self, tmp_db: ConnectionProvider) -> None:
        _insert_lot_with_region_id(tmp_db, 1, region_id=72)
        _insert_lot_with_region_id(tmp_db, 2, region_id=27)
        lot_repo = SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())
        assert lot_repo.count_active() == 2

    def test_count_active_filters_by_subjects(self, tmp_db: ConnectionProvider) -> None:
        _insert_lot_with_region_id(tmp_db, 1, region_id=72)
        _insert_lot_with_region_id(tmp_db, 2, region_id=72)
        _insert_lot_with_region_id(tmp_db, 3, region_id=27)
        lot_repo = SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())
        assert lot_repo.count_active(region_ids=(72,)) == 2
        assert lot_repo.count_active(region_ids=(27,)) == 1
        assert lot_repo.count_active(region_ids=(72, 27)) == 3

    def test_count_active_empty_equals_global(self, tmp_db: ConnectionProvider) -> None:
        _insert_lot_with_region_id(tmp_db, 1, region_id=72)
        _insert_lot_with_region_id(tmp_db, 2, region_id=27)
        lot_repo = SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())
        assert lot_repo.count_active(region_ids=()) == lot_repo.count_active()

    def test_count_active_excludes_inactive(self, tmp_db: ConnectionProvider) -> None:
        _insert_lot_with_region_id(tmp_db, 1, region_id=72, is_active=True)
        _insert_lot_with_region_id(tmp_db, 2, region_id=72, is_active=False)
        lot_repo = SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())
        assert lot_repo.count_active(region_ids=(72,)) == 1

    def test_count_active_unknown_region_returns_zero(self, tmp_db: ConnectionProvider) -> None:
        _insert_lot_with_region_id(tmp_db, 1, region_id=72)
        lot_repo = SqliteLotRepository(conn_provider=tmp_db, clock=FixedClock())
        assert lot_repo.count_active(region_ids=(99,)) == 0


# ---------------------------------------------------------------------------
# Tests: Migration data integrity (v3→v4 backfill)
# ---------------------------------------------------------------------------


class TestMigrationDataIntegrity:
    """Verify that the v3→v4 migration correctly backfills region_id from
    the existing lots.region text column using the domain catalog."""

    def test_backfill_maps_known_region_names(self, tmp_path: Path, schema_sql: str) -> None:
        """Insert lots with known RF subject names, run migration, verify region_id."""
        # Build a v3 DB (without region_id column and region_subscriptions table).
        # We do this by creating a fresh DB with the v3 schema.
        v3_schema = (
            schema_sql.replace(
                "PRAGMA user_version = 11",
                "PRAGMA user_version = 3",
            )
            .replace(
                "    region_id            INTEGER,"
                "                    -- RF-subject site-id (27–96); ADR-035 §I2, ADR-062\n",
                "",
            )
            .replace(
                "CREATE INDEX IF NOT EXISTS idx_lots_region_id_active"
                " ON lots(region_id, is_active);\n",
                "",
            )
        )
        # Also remove region_subscriptions table from v3 schema
        rs_block_start = v3_schema.find("-- Per-region subscription timestamps")
        rs_block_end = v3_schema.find("-- SMTP-логин/пароль")
        if rs_block_start != -1 and rs_block_end != -1:
            v3_schema = v3_schema[:rs_block_start] + v3_schema[rs_block_end:]

        db_path = tmp_path / "migrate_test.db"
        provider = ConnectionProvider(db_path=db_path)
        try:
            init_db(provider, schema_sql=v3_schema, latest_version=3)
            conn = provider.get()

            # Insert lots with known region names.
            # "Республика Бурятия" (site-id 72) → ДФО (macro-region 1)
            # "Республика Карелия" (site-id 27) → Арктика (macro-region 2)
            # "Неизвестный регион" — unknown, should remain NULL
            for lot_id, region_name in [
                (1, "Республика Бурятия"),
                (2, "Республика Карелия"),
                (3, "Неизвестный регион"),
            ]:
                conn.execute(
                    "INSERT INTO lots("
                    "  id, cadastral_no, region, status, date_create,"
                    "  first_seen, last_seen, is_active, raw_json, parser_version"
                    ") VALUES (?, ?, ?, 'Свободен', ?, ?, ?, 1, '{}', 1)",
                    (
                        lot_id,
                        f"11:{lot_id:05d}:100",
                        region_name,
                        _BASE_TIME.isoformat(),
                        _BASE_TIME.isoformat(),
                        _BASE_TIME.isoformat(),
                    ),
                )
            conn.commit()

            # Run the v3→v4 migration inside BEGIN IMMEDIATE (as the runner would).
            conn.execute("BEGIN IMMEDIATE")
            v3_to_v4(conn)
            conn.execute("PRAGMA user_version = 4")
            conn.commit()

            # Verify backfill results.
            rows = {
                row[0]: row[1]
                for row in conn.execute("SELECT id, region_id FROM lots ORDER BY id").fetchall()
            }
            assert rows[1] == 1, "Республика Бурятия should map to ДФО (1)"
            assert rows[2] == 2, "Республика Карелия should map to Арктика (2)"
            assert rows[3] is None, "Unknown region should remain NULL"

            # Verify region_subscriptions table exists and is empty.
            count = conn.execute("SELECT COUNT(*) FROM region_subscriptions").fetchone()[0]
            assert count == 0

        finally:
            provider.close_all()
