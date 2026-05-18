"""Migration v5 → v6: drop lot_user_state.starred column + idx_lus_starred index.

Background:
  The «Избранное» (starred) feature was removed by product decision 2026-05-18
  (ADR-053). The ``starred`` column and its partial index are no longer
  referenced by any application code.

SQLite 3.35+ supports ALTER TABLE ... DROP COLUMN when the column is not:
  - a PRIMARY KEY,
  - referenced by a UNIQUE constraint,
  - referenced by an index covering a column that also stays (partial indexes
    on the column alone are fine to drop first).

Steps (order matters — drop index before drop column):
  1. DROP INDEX IF EXISTS idx_lus_starred
  2. ALTER TABLE lot_user_state DROP COLUMN starred

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-053-remove-favorites-feature.md
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v5_to_v6(conn: sqlite3.Connection) -> None:
    """Apply v5→v6 schema migration.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    conn.execute("DROP INDEX IF EXISTS idx_lus_starred")
    conn.execute("ALTER TABLE lot_user_state DROP COLUMN starred")
