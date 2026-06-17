"""Integration tests for the v1→v2 SQLite migration.

Tests exercise the actual `v1_to_v2` function via `SqliteMigrationRunner` on a
real (temporary) SQLite database seeded with a v1-era schema.

Fixtures:
  - `tmp_db_path` (from conftest.py): per-test Path to an unused DB file.

The `tmp_db` fixture (conftest) applies the full v2 schema — we deliberately
do NOT use it here; instead `_seed_v1_db` creates the v1 schema directly.
"""

import sqlite3
from pathlib import Path

import pytest

from fis_monitor.domain.errors import ConcurrentMigrationError
from fis_monitor.infra.sqlite.migrations import (
    Migration,
    SqliteMigrationRunner,
    default_migration_runner,
)
from fis_monitor.infra.sqlite.migrations_v1_to_v2 import (
    v1_to_v2,
)
from fis_monitor.infra.sqlite.migrations_v2_to_v3 import (
    v2_to_v3,
)

# ---------------------------------------------------------------------------
# V1 schema helper
# ---------------------------------------------------------------------------


def _seed_v1_db(conn: sqlite3.Connection) -> None:
    """Create a v1-era schema and populate with test data.

    v1 `notifications`:
      - No status, attempt_no, last_attempt_at columns.
      - sent_at is NOT NULL (all rows are completed sends).
      - Non-partial indexes.

    v1 `smtp_credentials`:
      - No smtp_host, smtp_port columns.
    """
    # notifications v1
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
            lot_id    INTEGER NOT NULL,
            channel   TEXT    NOT NULL,
            recipient TEXT    NOT NULL,
            sent_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (lot_id, channel, recipient)
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_sent_at
            ON notifications(sent_at DESC);
        CREATE INDEX IF NOT EXISTS idx_notifications_channel
            ON notifications(channel, sent_at);

        CREATE TABLE IF NOT EXISTS smtp_credentials (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            smtp_user     TEXT    NOT NULL,
            smtp_password TEXT    NOT NULL,
            use_default   INTEGER NOT NULL DEFAULT 1 CHECK (use_default IN (0, 1)),
            updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        PRAGMA user_version = 1;
    """)

    # Seed notifications
    conn.executemany(
        "INSERT INTO notifications (lot_id, channel, recipient, sent_at) VALUES (?, ?, ?, ?)",
        [
            (101, "email", "user@example.com", "2024-01-15 10:00:00"),
            (102, "email", "other@example.com", "2024-01-16 11:30:00"),
            (103, "browser", "local", "2024-01-17 09:45:00"),
        ],
    )

    # Seed smtp_credentials
    conn.execute(
        "INSERT INTO smtp_credentials (id, smtp_user, smtp_password) VALUES (1, ?, ?)",
        ("bot@yandex.ru", "secret123"),
    )

    conn.commit()


def _open_v1(path: Path) -> sqlite3.Connection:
    """Open a connection, seed v1 schema, and return the connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _seed_v1_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrationPreservesNotificationsData:
    """All v1 rows survive migration with correct v2 field values."""

    def test_migration_preserves_notifications_data(self, tmp_db_path: Path) -> None:
        conn = _open_v1(tmp_db_path)
        try:
            runner = SqliteMigrationRunner(migrations=[Migration(1, 2, apply=v1_to_v2)])
            runner.run_pending(conn, from_version=1, to_version=2)

            rows = conn.execute(
                "SELECT lot_id, channel, recipient, status, attempt_no,"
                "       last_attempt_at, sent_at"
                "  FROM notifications ORDER BY lot_id"
            ).fetchall()

            assert len(rows) == 3

            # lot_id=101
            r = rows[0]
            assert r["lot_id"] == 101
            assert r["status"] == "sent"
            assert r["attempt_no"] == 1
            assert r["last_attempt_at"] == "2024-01-15 10:00:00"
            assert r["sent_at"] == "2024-01-15 10:00:00"

            # lot_id=102
            r = rows[1]
            assert r["lot_id"] == 102
            assert r["status"] == "sent"
            assert r["attempt_no"] == 1
            assert r["last_attempt_at"] == "2024-01-16 11:30:00"
            assert r["sent_at"] == "2024-01-16 11:30:00"

            # lot_id=103
            r = rows[2]
            assert r["lot_id"] == 103
            assert r["status"] == "sent"
            assert r["attempt_no"] == 1
            assert r["last_attempt_at"] == "2024-01-17 09:45:00"
            assert r["sent_at"] == "2024-01-17 09:45:00"
        finally:
            conn.close()


class TestMigrationAddsSmtpHostPortColumns:
    """smtp_credentials rows receive new columns with correct defaults."""

    def test_migration_adds_smtp_host_port_columns(self, tmp_db_path: Path) -> None:
        conn = _open_v1(tmp_db_path)
        try:
            runner = SqliteMigrationRunner(migrations=[Migration(1, 2, apply=v1_to_v2)])
            runner.run_pending(conn, from_version=1, to_version=2)

            row = conn.execute(
                "SELECT smtp_user, smtp_host, smtp_port FROM smtp_credentials WHERE id = 1"
            ).fetchone()

            assert row is not None
            assert row["smtp_user"] == "bot@yandex.ru"
            assert row["smtp_host"] == "smtp.yandex.ru"
            assert row["smtp_port"] == 587
        finally:
            conn.close()


class TestMigrationCreatesV2Indexes:
    """All three v2 indexes are present; old full-scan sent_at index is replaced."""

    def test_migration_creates_v2_indexes(self, tmp_db_path: Path) -> None:
        conn = _open_v1(tmp_db_path)
        try:
            runner = SqliteMigrationRunner(migrations=[Migration(1, 2, apply=v1_to_v2)])
            runner.run_pending(conn, from_version=1, to_version=2)

            # Collect index info from SQLite master
            index_rows = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
                " AND tbl_name='notifications'"
            ).fetchall()
            index_by_name = {r["name"]: r["sql"] for r in index_rows}

            expected = {
                "idx_notifications_sent_at",
                "idx_notifications_channel",
                "idx_notifications_pending",
            }
            assert expected.issubset(index_by_name.keys()), (
                f"Missing indexes. Present: {set(index_by_name.keys())}"
            )

            # Verify the new sent_at index is partial (WHERE status='sent')
            sent_at_sql = index_by_name["idx_notifications_sent_at"]
            assert sent_at_sql is not None
            assert "status" in sent_at_sql.lower(), (
                f"idx_notifications_sent_at should be partial. SQL: {sent_at_sql}"
            )

            # Verify the pending index is partial (WHERE status='pending')
            pending_sql = index_by_name["idx_notifications_pending"]
            assert pending_sql is not None
            assert "pending" in pending_sql.lower(), (
                f"idx_notifications_pending should be partial. SQL: {pending_sql}"
            )
        finally:
            conn.close()


class TestMigrationIdempotentViaRunner:
    """Runner applies migration once; a second run with to_version=2 is no-op."""

    def test_migration_idempotent_via_runner(self, tmp_db_path: Path) -> None:
        conn = _open_v1(tmp_db_path)
        try:
            runner = default_migration_runner()

            # First run: v1 → v2
            runner.run_pending(conn, from_version=1, to_version=2)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2

            # Second run: already at v2, no-op (from_version == to_version)
            runner.run_pending(conn, from_version=2, to_version=2)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2

            # Attempt to run from 1 again → ConcurrentMigrationError (TOCTOU)
            with pytest.raises(ConcurrentMigrationError):
                runner.run_pending(conn, from_version=1, to_version=2)
        finally:
            conn.close()


class TestMigrationAtomicOnMiddleFailure:
    """If a migration in the chain raises, all changes are rolled back."""

    def test_migration_atomic_on_middle_failure(self, tmp_db_path: Path) -> None:
        conn = _open_v1(tmp_db_path)
        try:

            def _always_fail(c: sqlite3.Connection) -> None:
                raise RuntimeError("intentional failure mid-migration")

            # Chain: 1→2 (success), 2→3 (failure)
            # We first need to make the DB look like v1 expecting to go to v3.
            # Use two migrations: first does the real v1→v2, second raises.
            runner = SqliteMigrationRunner(
                migrations=[
                    Migration(from_version=1, to_version=2, apply=v1_to_v2),
                    Migration(from_version=2, to_version=3, apply=_always_fail),
                ]
            )

            with pytest.raises(RuntimeError, match="intentional failure"):
                runner.run_pending(conn, from_version=1, to_version=3)

            # user_version must still be 1 (full rollback)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 1

            # notifications table should still have the v1 structure:
            # attempt to select status column — must fail (column does not exist)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("SELECT status FROM notifications").fetchall()

            # Original v1 data must remain intact after rollback
            rows = conn.execute(
                "SELECT lot_id, sent_at FROM notifications ORDER BY lot_id"
            ).fetchall()
            assert len(rows) == 3
            assert rows[0]["lot_id"] == 101
            assert rows[1]["lot_id"] == 102
            assert rows[2]["lot_id"] == 103

        finally:
            conn.close()


class TestDefaultMigrationRunnerFactory:
    """`default_migration_runner()` returns a runner with all registered migrations."""

    def test_default_migration_runner_factory(self) -> None:
        runner = default_migration_runner()
        migrations = list(runner.list_migrations())

        # v1→v2, v2→v3 (smtp_from_name, bd ljp), v3→v4 (region_subscriptions, nvx2),
        # v4→v5 (lots.date_registry, ADR-040), v5→v6 (drop starred, ADR-053),
        # v6→v7 (backfill region_id NULL rows, ADR-035 I2),
        # v7→v8 (region_id macro-id → site-id namespace fix, pc1g),
        # v8→v9 (idx_lots_first_seen DESC, cpo4),
        # v9→v10 (region_subscriptions macro→subject expansion, ADR-062),
        # v10→v11 (lots.is_backfill provenance flag, bd 31g)
        assert len(migrations) == 10
        m1 = migrations[0]
        assert m1.from_version == 1
        assert m1.to_version == 2
        assert m1.apply is v1_to_v2

        m2 = migrations[1]
        assert m2.from_version == 2
        assert m2.to_version == 3
        assert m2.apply is v2_to_v3
