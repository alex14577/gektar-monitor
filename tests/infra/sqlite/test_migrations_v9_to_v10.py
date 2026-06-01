"""Integration tests for the v9 → v10 SQLite migration.

Invariants tested:
  1. Macro-rows are expanded: after migration, each subject site-id from
     SUBJECTS_BY_MACRO appears in region_subscriptions.
  2. Original macro-id rows (1, 2) are deleted.
  3. Shared subjects (87, 96) get MIN(subscribed_at) when both macros present.
  4. Idempotency: running the migration again (same user_version guard makes
     this impossible in production, but v9_to_v10 itself is safe to call twice
     — INSERT OR IGNORE + MIN update + DELETE of already-absent rows are no-ops).

Layer 3 — Infrastructure (SQLite migration).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fis_monitor.domain.regions import SUBJECTS_BY_MACRO
from fis_monitor.infra.sqlite.migrations import Migration, SqliteMigrationRunner
from fis_monitor.infra.sqlite.migrations_v9_to_v10 import v9_to_v10

# DFO macro-id and Arktika macro-id
_DFO = 1
_ARCTIC = 2
_DFO_SUBJECTS = set(SUBJECTS_BY_MACRO[_DFO])
_ARCTIC_SUBJECTS = set(SUBJECTS_BY_MACRO[_ARCTIC])
_SHARED_SUBJECTS = _DFO_SUBJECTS & _ARCTIC_SUBJECTS  # {87, 96}

_T_EARLY = "2026-01-01T00:00:00+00:00"
_T_LATE = "2026-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _create_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS region_subscriptions (
            region_id     INTEGER PRIMARY KEY,
            subscribed_at TEXT NOT NULL
        );
        PRAGMA user_version = 9;
    """)
    conn.commit()


def _insert(conn: sqlite3.Connection, region_id: int, subscribed_at: str) -> None:
    conn.execute(
        "INSERT INTO region_subscriptions (region_id, subscribed_at) VALUES (?, ?)",
        (region_id, subscribed_at),
    )
    conn.commit()


def _all_rows(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(
        "SELECT region_id, subscribed_at FROM region_subscriptions"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Runner helper
# ---------------------------------------------------------------------------


def _run(conn: sqlite3.Connection) -> None:
    runner = SqliteMigrationRunner(
        migrations=[Migration(from_version=9, to_version=10, apply=v9_to_v10)]
    )
    runner(conn, from_version=9, to_version=10)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dfo_only_expands_to_subjects(tmp_db_path: Path) -> None:
    """Single macro ДФО (1) → all DFO subject site-ids present; macro row gone."""
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_table(conn)
        _insert(conn, _DFO, _T_EARLY)

        _run(conn)

        rows = _all_rows(conn)
        assert _DFO not in rows, "macro-id 1 must be deleted"
        assert _ARCTIC not in rows
        for sid in _DFO_SUBJECTS:
            assert sid in rows, f"subject {sid} missing after DFO expansion"
            assert rows[sid] == _T_EARLY

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 10
    finally:
        conn.close()


def test_arctic_only_expands_to_subjects(tmp_db_path: Path) -> None:
    """Single macro Арктика (2) → all Arctic subject site-ids present; macro row gone."""
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_table(conn)
        _insert(conn, _ARCTIC, _T_LATE)

        _run(conn)

        rows = _all_rows(conn)
        assert _ARCTIC not in rows, "macro-id 2 must be deleted"
        for sid in _ARCTIC_SUBJECTS:
            assert sid in rows, f"subject {sid} missing after Arctic expansion"
            assert rows[sid] == _T_LATE
    finally:
        conn.close()


def test_both_macros_shared_subjects_get_min_subscribed_at(tmp_db_path: Path) -> None:
    """Both macros present: shared subjects (87, 96) get MIN(subscribed_at)."""
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_table(conn)
        _insert(conn, _DFO, _T_EARLY)    # earlier
        _insert(conn, _ARCTIC, _T_LATE)  # later

        _run(conn)

        rows = _all_rows(conn)
        assert _DFO not in rows
        assert _ARCTIC not in rows

        for sid in _DFO_SUBJECTS | _ARCTIC_SUBJECTS:
            assert sid in rows, f"subject {sid} missing"

        # Shared subjects must have the MIN (DFO = _T_EARLY)
        for sid in _SHARED_SUBJECTS:
            assert rows[sid] == _T_EARLY, (
                f"shared subject {sid}: expected {_T_EARLY!r}, got {rows[sid]!r}"
            )

        # DFO-only subjects carry DFO timestamp
        for sid in _DFO_SUBJECTS - _SHARED_SUBJECTS:
            assert rows[sid] == _T_EARLY

        # Arctic-only subjects carry Arctic timestamp
        for sid in _ARCTIC_SUBJECTS - _SHARED_SUBJECTS:
            assert rows[sid] == _T_LATE
    finally:
        conn.close()


def test_no_macro_rows_is_noop(tmp_db_path: Path) -> None:
    """If region_subscriptions already contains only subject site-ids, migration is a no-op."""
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_table(conn)
        # Pre-populate with a subject site-id (already migrated)
        _insert(conn, 72, _T_EARLY)

        _run(conn)

        rows = _all_rows(conn)
        assert rows == {72: _T_EARLY}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 10
    finally:
        conn.close()


def test_idempotent_second_call(tmp_db_path: Path) -> None:
    """Calling v9_to_v10 directly a second time does not change the result."""
    conn = sqlite3.connect(str(tmp_db_path))
    try:
        _create_table(conn)
        _insert(conn, _DFO, _T_EARLY)

        _run(conn)
        rows_after_first = _all_rows(conn)

        # Simulate a second invocation (manually — in prod user_version guard prevents it)
        conn.execute("BEGIN IMMEDIATE")
        v9_to_v10(conn)
        conn.commit()

        rows_after_second = _all_rows(conn)
        assert rows_after_first == rows_after_second
    finally:
        conn.close()
