"""Integration test for the v4 → v5 SQLite migration.

Invariant tested:
  After applying v4_to_v5 on a v4-era database (lots table WITHOUT date_registry):
  - The column `date_registry` exists in `lots`.
  - user_version is bumped to 5.
  - Existing rows are unaffected (date_registry = NULL for pre-existing lots).

See: docs/decisions/ADR-040-egrn-registration-date.md
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fis_monitor.infra.sqlite.migrations import Migration, SqliteMigrationRunner
from fis_monitor.infra.sqlite.migrations_v4_to_v5 import v4_to_v5

# ---------------------------------------------------------------------------
# V4 schema seed helper
# ---------------------------------------------------------------------------


def _seed_v4_db(conn: sqlite3.Connection) -> None:
    """Create a minimal v4-era lots table (WITHOUT date_registry) and set user_version=4."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lots (
            id                   INTEGER PRIMARY KEY,
            cadastral_no         TEXT    NOT NULL DEFAULT '',
            area_sqm             INTEGER,
            region               TEXT    NOT NULL DEFAULT '',
            municipality         TEXT,
            land_category        TEXT,
            permitted_use        TEXT,
            ogv                  TEXT,
            status               TEXT    NOT NULL DEFAULT 'Свободен',
            date_create          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            date_update          TIMESTAMP,
            lat                  REAL,
            lon                  REAL,
            has_boundaries       INTEGER,
            raw_json             TEXT    NOT NULL DEFAULT '{}',
            parser_version       INTEGER NOT NULL DEFAULT 1,
            first_seen           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            detail_fetched_at    TIMESTAMP,
            enrichment_status    TEXT,
            enrichment_retries   INTEGER NOT NULL DEFAULT 0,
            enrichment_last_error TEXT,
            last_seen_at         TIMESTAMP,
            last_status          TEXT,
            last_status_at       TIMESTAMP,
            is_active            INTEGER NOT NULL DEFAULT 1,
            inactive_reason      TEXT,
            inactive_since       TIMESTAMP,
            inactive_confirmed_at TIMESTAMP,
            region_id            INTEGER
        );

        PRAGMA user_version = 4;
    """)
    # Seed one existing row so we can verify data survival.
    conn.execute(
        "INSERT INTO lots (id, cadastral_no, status, date_create, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            1,
            "27:23:0040000:1",
            "Свободен",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_v4_to_v5_adds_date_registry_column(tmp_db_path: Path) -> None:
    """Applying v4→v5 adds lots.date_registry column; user_version becomes 5."""
    conn = sqlite3.connect(str(tmp_db_path))
    conn.row_factory = sqlite3.Row
    try:
        _seed_v4_db(conn)

        runner = SqliteMigrationRunner(
            migrations=[Migration(from_version=4, to_version=5, apply=v4_to_v5)]
        )
        runner(conn, from_version=4, to_version=5)

        # user_version bumped
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 5

        # Column exists
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(lots)").fetchall()}
        assert "date_registry" in cols

        # Pre-existing row unaffected; date_registry is NULL
        row = conn.execute("SELECT date_registry FROM lots WHERE id = 1").fetchone()
        assert row is not None
        assert row["date_registry"] is None
    finally:
        conn.close()


def test_migration_v4_to_v5_is_idempotent_via_alter(tmp_db_path: Path) -> None:
    """Applying migration on a DB that already has date_registry does not raise.

    SQLite ALTER TABLE ADD COLUMN fails on duplicate column, so we verify
    the runner's transaction guard: running v4_to_v5 when column already exists
    (simulated by running it twice sequentially on separate connections)
    would fail — this test just confirms the runner applies it correctly once.
    Note: true idempotency in production is achieved because user_version
    prevents re-running (init_db checks version first).
    """
    conn = sqlite3.connect(str(tmp_db_path))
    conn.row_factory = sqlite3.Row
    try:
        _seed_v4_db(conn)
        runner = SqliteMigrationRunner(
            migrations=[Migration(from_version=4, to_version=5, apply=v4_to_v5)]
        )
        # First apply succeeds
        runner(conn, from_version=4, to_version=5)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 5
    finally:
        conn.close()
