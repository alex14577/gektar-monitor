"""Migration v10 → v11: add lots.is_backfill provenance flag.

Scope:
  - Add column ``lots.is_backfill INTEGER NOT NULL DEFAULT 0`` recording how a
    lot row first entered the DB: 0 = discovered by the live monitor cycle,
    1 = first inserted by the catalogue backfill.

Background (bd gektar-monitor-31g):
  ``latest_new_first_seen()`` backs the "Последний новый" header chip via
  ``SELECT MAX(first_seen) FROM lots``. ``first_seen`` is stamped with the
  ingestion wallclock (``clock.now()``) for BOTH live and backfill lots, so
  after any backfill cycle ``MAX(first_seen)`` returned the time the historical
  catch-up rows were written — not the moment a genuinely-new live lot
  appeared. The chip therefore showed a misleadingly-recent time belonging to
  an old auction. The flag lets ``latest_new_first_seen()`` exclude
  backfill-discovered rows (``WHERE is_backfill = 0``); it extends the
  "backfill is silent" decision of ADR-060 to the header chip.

  Existing rows get DEFAULT 0 (we cannot know which legacy rows were backfill);
  ``MAX(first_seen)`` self-heals once the next live lot arrives.

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-060-backfill-sse-insertion-and-true-total-counter.md
"""

from __future__ import annotations

import sqlite3


def v10_to_v11(conn: sqlite3.Connection) -> None:
    """Apply v10→v11 schema migration: add lots.is_backfill.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

    Idempotent: skips the ADD COLUMN when the column already exists, so a
    direct second invocation is a no-op (in production the user_version guard
    already prevents re-application).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lots)")}
    if "is_backfill" not in cols:
        conn.execute(
            "ALTER TABLE lots ADD COLUMN is_backfill INTEGER NOT NULL DEFAULT 0"
            "  CHECK (is_backfill IN (0, 1))"
        )
