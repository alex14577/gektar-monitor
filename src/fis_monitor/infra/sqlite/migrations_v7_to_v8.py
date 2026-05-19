"""Migration v7 → v8: backfill lots.region_id from macro-id to site-id namespace.

Background:
  The v6→v7 migration (and the parser prior to this fix) stored the
  macro-region ID (1=ДФО, 2=Арктика) in ``lots.region_id``.  However,
  ``Settings.filters.rf_subjects`` holds **site-ids** (e.g. 27 for
  Республика Карелия) from the ``SUBJECT_TITLE_BY_ID`` catalog.
  ``RfSubjectFilterMatcher`` performs ``lot.region_id in filters.rf_subjects``
  — the two namespaces never intersect, so email notifications were silently
  suppressed for all lots.

Fix (ADR-035 §I2, pc1g):
  ``region_id`` is redefined as a **site-id** — a key of
  ``SUBJECT_TITLE_BY_ID``.  This migration backfills every row whose
  current ``region_id`` is in the macro-id range {1, 2} by resolving the
  display name stored in ``lots.region`` to the correct site-id via the
  inverted catalog.

Algorithm:
  1. Build ``name → site_id`` map from ``SUBJECT_TITLE_BY_ID``.
  2. For each distinct ``region`` text present in the DB, compute the
     correct site-id.
  3. UPDATE rows where the resolved site-id differs from the stored
     ``region_id`` (covers macro-id rows AND NULL rows in one pass).
  4. Rows whose ``region`` text is not in the catalog are set to NULL
     (fail-open; the text value is preserved in ``region``).

Idempotency:
  The UPDATE uses ``WHERE region = ?`` unconditionally (no ``region_id``
  guard), so re-running the migration is safe — it will set the same
  correct value again.

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-035-three-scope-filter-model.md §I2
    docs/decisions/ADR-NNN-region-id-namespace-canonical.md (pc1g)
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v7_to_v8(conn: sqlite3.Connection) -> None:
    """Apply v7→v8 schema migration: backfill region_id to site-id namespace.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    # Import inside the function to avoid circular imports at module load.
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

    # Build display-name → site-id map (inverse of SUBJECT_TITLE_BY_ID).
    name_to_site_id: dict[str, int] = {
        title: sid for sid, title in SUBJECT_TITLE_BY_ID.items()
    }

    # Fetch all distinct region text values present in the lots table.
    cur = conn.execute(
        "SELECT DISTINCT region FROM lots WHERE region IS NOT NULL"
    )
    regions_in_db = [row[0] for row in cur.fetchall()]
    cur.close()

    for region_name in regions_in_db:
        site_id = name_to_site_id.get(region_name)  # None for unknown names
        conn.execute(
            "UPDATE lots SET region_id = ? WHERE region = ?",
            (site_id, region_name),
        )
