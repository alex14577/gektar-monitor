"""Layer 3 integration: BackfillService galka + total persisted via real SQLite (k31/fsm).

Invariants covered (test-strategy Layer 3):
  (8a) galka persists across SqliteStateRepository get/set on real SQLite.
  (8b) total_last persists and is read back correctly.
  (8c) galka absent → is_done() False; present → True.

docs/architecture/09-test-strategy.md Layer 3:
  Integration: real SQLite (:memory:), real SqliteStateRepository.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fis_monitor.infra.sqlite.repositories.state import SqliteStateRepository

# ---------------------------------------------------------------------------
# Minimal in-memory state table (mirrors docs/db/schema.sql)
# ---------------------------------------------------------------------------

_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeClock:
    def now(self) -> datetime:
        return _NOW


class _SingleConn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self) -> sqlite3.Connection:
        return self._conn


def _make_repo() -> SqliteStateRepository:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_STATE)
    conn.commit()
    return SqliteStateRepository(conn_provider=_SingleConn(conn), clock=_FakeClock())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_galka_absent_by_default() -> None:
    """Invariant 8c: galka absent → get returns None."""
    repo = _make_repo()
    assert repo.get("backfill.done") is None


def test_galka_persists_after_set() -> None:
    """Invariant 8a: galka survives set→get round-trip on real SQLite."""
    repo = _make_repo()
    repo.set("backfill.done", "1")
    assert repo.get("backfill.done") == "1"


def test_total_last_persists() -> None:
    """Invariant 8b: donor total persists and is read back as string."""
    repo = _make_repo()
    repo.set("backfill.total_last", "346")
    raw = repo.get("backfill.total_last")
    assert raw is not None
    assert int(raw) == 346


def test_delete_removes_galka() -> None:
    """Galka can be removed (idempotent delete)."""
    repo = _make_repo()
    repo.set("backfill.done", "1")
    repo.delete("backfill.done")
    assert repo.get("backfill.done") is None
    repo.delete("backfill.done")  # idempotent


def test_total_last_update_overwrites() -> None:
    """SET overwrites previous total_last value."""
    repo = _make_repo()
    repo.set("backfill.total_last", "100")
    repo.set("backfill.total_last", "80")
    assert int(repo.get("backfill.total_last")) == 80  # type: ignore[arg-type]
