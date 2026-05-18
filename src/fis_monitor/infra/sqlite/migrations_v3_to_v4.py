"""Migration v3 → v4: region_subscriptions table + lots.region_id column.

Scope:
  - ``lots``: ADD COLUMN ``region_id INTEGER`` (nullable; backfilled from the
    domain regions catalog using the existing ``region`` text column).
  - ``region_subscriptions``: new table ``(region_id INTEGER PRIMARY KEY,
    subscribed_at TEXT NOT NULL)`` per ADR-039.
  - Index ``idx_lots_region_id_active`` on ``lots(region_id, is_active)`` for
    cheap ``count_active(region_id=X)`` queries.

The ``region`` TEXT column is retained for backward compatibility.

Backfill algorithm:
  1. Invert ``SUBJECT_TITLE_BY_ID`` (site-id → RF subject name) to
     ``name → site-id``.
  2. For each macro-region in ``SUBJECTS_BY_MACRO``, map site-ids to
     the macro-region ID (1=ДФО, 2=Арктика).  Dual-membership subjects
     (87 Якутия, 96 Чукотка appear in both) keep the first assignment.
  3. UPDATE lots SET region_id = <macro_id> WHERE region = <name>.

Lots whose ``region`` text is not in the catalog get ``region_id = NULL``
(no data loss — the text value is preserved in ``region``).

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-039-subscribed-at-region-cutoff.md
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v3_to_v4(conn: sqlite3.Connection) -> None:
    """Apply v3→v4 schema migration.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    _add_region_id_to_lots(conn)
    _create_region_subscriptions(conn)


def _add_region_id_to_lots(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE lots ADD COLUMN region_id INTEGER")

    # Build name → macro_region_id mapping from domain catalog.
    # Import here (inside the function) to avoid circular imports at module load.
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID, SUBJECTS_BY_MACRO

    name_to_macro: dict[str, int] = {}
    for macro_id, site_ids in SUBJECTS_BY_MACRO.items():
        for site_id in site_ids:
            name = SUBJECT_TITLE_BY_ID.get(site_id)
            if name and name not in name_to_macro:
                name_to_macro[name] = macro_id

    cur = conn.execute("SELECT DISTINCT region FROM lots WHERE region IS NOT NULL")
    regions_in_db = [row[0] for row in cur.fetchall()]
    cur.close()

    for region_name in regions_in_db:
        mapped_macro_id = name_to_macro.get(region_name)
        if mapped_macro_id is not None:
            conn.execute(
                "UPDATE lots SET region_id = ? WHERE region = ?",
                (mapped_macro_id, region_name),
            )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lots_region_id_active"
        " ON lots(region_id, is_active)"
    )


def _create_region_subscriptions(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS region_subscriptions ("
        "  region_id     INTEGER PRIMARY KEY,"
        "  subscribed_at TEXT NOT NULL"
        ")"
    )
