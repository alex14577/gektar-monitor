"""Migration v6 → v7: backfill lots.region_id for legacy NULL rows.

Background:
  The v3→v4 migration (nvx2) added ``lots.region_id INTEGER`` and backfilled
  it from the ``region`` text column using the domain catalog
  ``SUBJECT_TITLE_BY_ID``.  Any lot whose ``region`` text did not match a
  catalog entry at that time was left with ``region_id = NULL``.

  These NULL rows trigger fail-open in ``RfSubjectFilterMatcher`` (ADR-035 I2),
  meaning they are never suppressed by the rf_subjects filter regardless of
  user configuration — an intentional but undesirable default for lots that
  would normally be filtered.

Backfill algorithm:
  1. Invert ``SUBJECT_TITLE_BY_ID`` (site-id → RF-subject name) to
     ``name → site-id`` (i.e., region text → macro-region integer ID).
     The macro-region is derived via ``SUBJECTS_BY_MACRO``: for each
     macro_id, iterate its subject site-ids; ``region_id`` is set to the
     macro-region ID (1=ДФО, 2=Арктика), matching the convention established
     in v3_to_v4.
  2. Emit a single UPDATE per distinct region name that now resolves.
  3. Lots whose ``region`` is still not in the catalog remain NULL (no data
     loss — the text value is preserved in ``region``).

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-035-three-scope-filter-model.md §I2
    docs/decisions/ADR-039-subscribed-at-region-cutoff.md
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v6_to_v7(conn: sqlite3.Connection) -> None:
    """Apply v6→v7 schema migration: backfill region_id for NULL rows.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    # Import inside the function to avoid circular imports at module load.
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID, SUBJECTS_BY_MACRO

    # Build region text → macro_region_id mapping (mirrors v3_to_v4 logic).
    name_to_macro: dict[str, int] = {}
    for macro_id, site_ids in SUBJECTS_BY_MACRO.items():
        for site_id in site_ids:
            name = SUBJECT_TITLE_BY_ID.get(site_id)
            if name and name not in name_to_macro:
                name_to_macro[name] = macro_id

    # Only look at rows that have region_id IS NULL — non-destructive.
    cur = conn.execute(
        "SELECT DISTINCT region FROM lots WHERE region_id IS NULL AND region IS NOT NULL"
    )
    regions_in_db = [row[0] for row in cur.fetchall()]
    cur.close()

    for region_name in regions_in_db:
        macro_id = name_to_macro.get(region_name)
        if macro_id is not None:
            conn.execute(
                "UPDATE lots SET region_id = ? WHERE region = ? AND region_id IS NULL",
                (macro_id, region_name),
            )
