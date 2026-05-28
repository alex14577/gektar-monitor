"""Migration v8 → v9: idx_lots_first_seen(DESC) for cheap MAX(first_seen).

Scope:
  - Add covering index on ``lots(first_seen DESC)`` so that the per-cycle
    header refresh query ``SELECT MAX(first_seen) FROM lots`` can be
    satisfied by a single B-tree lookup instead of a full sequential
    scan.

Background (bd gektar_monitor-cpo4, discovered from dj9f):
  `monitor_cycle._publish_status` calls
  `lots.latest_new_first_seen` every header refresh (lots.py:561 area).
  Without an index on ``first_seen``, SQLite resolves the MAX via a full
  scan of the ``lots`` table. Harmless at small scale; matters as the
  table grows to 10k+ rows.

  SQLite optimizes ``MAX(col)`` against an index on ``col`` whose leading
  key is ``col`` — DESC ordering lets the planner read a single tail
  page. ASC would also work for MAX, but DESC matches the access pattern
  ("most recently created lot") and is symmetric for any future
  ``ORDER BY first_seen DESC LIMIT N`` queries.

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v8_to_v9(conn: sqlite3.Connection) -> None:
    """Apply v8→v9 schema migration: add idx_lots_first_seen.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lots_first_seen ON lots(first_seen DESC)"
    )
