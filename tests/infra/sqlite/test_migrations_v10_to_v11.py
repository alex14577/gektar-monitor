"""Integration tests for the v10 → v11 SQLite migration.

Invariants tested:
  1. Column added: after migration ``lots.is_backfill`` exists.
  2. Default 0: existing rows get is_backfill = 0 (treated as live).
  3. user_version bumped to 11.
  4. Idempotency: calling v10_to_v11 a second time is a no-op (the
     PRAGMA user_version guard prevents re-application in production, but
     the function itself guards on column presence).

Layer 3 — Infrastructure (SQLite migration).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fis_monitor.infra.sqlite.migrations import Migration, SqliteMigrationRunner
from fis_monitor.infra.sqlite.migrations_v10_to_v11 import v10_to_v11


def _create_v10_lots(conn: sqlite3.Connection) -> None:
    """Minimal v10-era lots table (no is_backfill) at user_version=10."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lots (
            id          INTEGER PRIMARY KEY,
            cadastral_no TEXT NOT NULL,
            region       TEXT NOT NULL,
            status       TEXT NOT NULL,
            date_create  TIMESTAMP NOT NULL,
            first_seen   TIMESTAMP NOT NULL,
            last_seen    TIMESTAMP NOT NULL,
            is_active    INTEGER NOT NULL DEFAULT 1
        );
        PRAGMA user_version = 10;
    """)
    conn.commit()


def _insert(conn: sqlite3.Connection, lot_id: int, first_seen: str) -> None:
    conn.execute(
        "INSERT INTO lots (id, cadastral_no, region, status, date_create, "
        "first_seen, last_seen) VALUES (?, '00:00', 'X', 'active', ?, ?, ?)",
        (lot_id, first_seen, first_seen, first_seen),
    )
    conn.commit()


def _run(conn: sqlite3.Connection) -> None:
    runner = SqliteMigrationRunner(
        migrations=[Migration(from_version=10, to_version=11, apply=v10_to_v11)]
    )
    runner(conn, from_version=10, to_version=11)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(lots)")}


def test_adds_is_backfill_column(tmp_db_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_v10_lots(conn)
        assert "is_backfill" not in _columns(conn)

        _run(conn)

        assert "is_backfill" in _columns(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 11
    finally:
        conn.close()


def test_existing_rows_default_to_zero(tmp_db_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_v10_lots(conn)
        _insert(conn, 1, "2026-06-01T08:00:00+00:00")

        _run(conn)

        value = conn.execute("SELECT is_backfill FROM lots WHERE id = 1").fetchone()[0]
        assert value == 0, "existing rows must default to is_backfill=0 (live)"
    finally:
        conn.close()


def test_idempotent_second_call(tmp_db_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_v10_lots(conn)
        _run(conn)

        # Direct second invocation — column already present → no-op, no raise.
        conn.execute("BEGIN IMMEDIATE")
        v10_to_v11(conn)
        conn.commit()

        assert "is_backfill" in _columns(conn)
    finally:
        conn.close()
