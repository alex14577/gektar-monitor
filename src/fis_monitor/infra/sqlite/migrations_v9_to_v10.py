"""Migration v9 → v10: expand region_subscriptions from macro-ids to subject site-ids.

Scope:
  - Each row with a macro-region id (1=ДФО, 2=Арктика) is expanded into one
    row per subject site-id drawn from SUBJECTS_BY_MACRO.
  - For subjects shared across macros (87, 96) the earliest subscribed_at is
    kept (MIN).
  - Original macro-rows are deleted after expansion.
  - The migration is idempotent: INSERT OR IGNORE guards re-runs; macro-rows
    absent from the table on a second pass produce no-ops.

Background (bd gektar-monitor-v1t, ADR-062 Phase 1):
  lots.region_id stores subject site-ids (27–96, since migration v7→v8).
  region_subscriptions.region_id still stored macro-ids (1, 2).
  JOIN lots.region_id = rs.region_id therefore yielded 0 matches, breaking:
    - subscription_cutoff_fragment (feed cutoff filter)
    - notifier_dispatcher.should_suppress
    - delta-trigger in monitor_cycle

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-062-region-subscription-namespace.md
"""

from __future__ import annotations

import sqlite3

from fis_monitor.domain.regions import SUBJECTS_BY_MACRO


def v9_to_v10(conn: sqlite3.Connection) -> None:
    """Apply v9→v10 schema migration: expand macro-ids to subject site-ids.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    rows = conn.execute(
        "SELECT region_id, subscribed_at FROM region_subscriptions"
    ).fetchall()

    macro_ids_found: list[int] = []
    for region_id, subscribed_at in rows:
        if region_id not in SUBJECTS_BY_MACRO:
            continue
        macro_ids_found.append(region_id)
        for subject_id in SUBJECTS_BY_MACRO[region_id]:
            conn.execute(
                "INSERT OR IGNORE INTO region_subscriptions (region_id, subscribed_at)"
                " VALUES (?, ?)",
                (subject_id, subscribed_at),
            )
            # For shared subjects (87, 96): if a row already exists, keep MIN(subscribed_at).
            conn.execute(
                "UPDATE region_subscriptions"
                " SET subscribed_at = MIN(subscribed_at, ?)"
                " WHERE region_id = ?",
                (subscribed_at, subject_id),
            )

    for macro_id in macro_ids_found:
        conn.execute(
            "DELETE FROM region_subscriptions WHERE region_id = ?",
            (macro_id,),
        )
