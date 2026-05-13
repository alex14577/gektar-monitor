"""SQLite implementation of ``CyclesRepository``.

Tracks monitor-cycle lifecycle in the ``cycles`` table.

Write methods (``open``, ``close``) use ``BEGIN IMMEDIATE`` per ADR-016.
Read method (``list_recent``) is a plain SELECT — no explicit transaction.

``prune_older_than`` deletes old closed cycles in chunks of ``batch_size``
rows, each chunk in its own short ``BEGIN IMMEDIATE`` transaction so the
writer-lock is never held for bulk DELETE (R3-M7).

Design note — ``now`` parameter for prune:
  Consistent with ``SqliteNotificationsRepository`` (which uses a ``Clock``
  injected in the constructor), this repository takes ``clock: Clock`` in its
  constructor and uses ``clock.now()`` inside ``prune_older_than``.  This
  gives full determinism in tests (no patching required) and keeps the
  interface clean — callers never need to pass a ``now`` argument to prune.

See:
  docs/decisions/ADR-016-repository-invariants-begin-immediate.md
  docs/architecture/03-protocols.md §3.1
  docs/data-model/cycles.md (if exists)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fis_monitor.domain.interfaces import Clock
from fis_monitor.domain.models import CycleResult
from fis_monitor.infra.sqlite.connection import ConnectionProvider


def _iso(dt: datetime) -> str:
    """Serialise a datetime to ISO-8601 string for storage."""
    return dt.isoformat()


def _parse_dt(raw: str) -> datetime:
    """Parse an ISO-8601 string from DB; restore UTC tzinfo if naive."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _row_to_result(row: tuple) -> CycleResult:
    """Convert a DB row to ``CycleResult``.

    Expected column order (from SELECT statement):
    id, region, started_at, finished_at, status, lots_fetched, new_lots,
    error, id_schema_check
    """
    (
        id_,
        region,
        started_at_raw,
        finished_at_raw,
        status,
        lots_fetched,
        new_lots,
        error,
        id_schema_check,
    ) = row
    return CycleResult(
        id=id_,
        region=region,
        started_at=_parse_dt(started_at_raw),
        finished_at=_parse_dt(finished_at_raw),
        status=status,
        lots_fetched=lots_fetched,
        new_lots=new_lots,
        error=error,
        id_schema_check=id_schema_check,
    )


_SELECT_COLUMNS = (
    "id, region, started_at, finished_at, status,"
    " lots_fetched, new_lots, error, id_schema_check"
)


class SqliteCyclesRepository:
    """SQLite-backed ``CyclesRepository``.

    Responsibilities (SRP): CRUD + tx-invariants for the ``cycles`` table.
    Business rules (e.g., max cycle duration, alerting) live outside this class.

    DI via constructor::

        repo = SqliteCyclesRepository(conn_provider=provider, clock=SystemClock())

    ``clock`` must be UTC-aware (``Clock`` Protocol from domain/interfaces.py).
    It is used by ``prune_older_than`` to compute the cutoff timestamp.
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API — CyclesRepository Protocol
    # ------------------------------------------------------------------

    def open(self, region: int, at: datetime) -> int:
        """Insert an open cycle row and return the new ``cycle_id``.

        Uses ``BEGIN IMMEDIATE`` to capture the writer-lock atomically.
        The row is created with ``status='open'``.
        """
        at_iso = _iso(at)
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO cycles (region, started_at, status)"
                " VALUES (?, ?, 'open')"
                " RETURNING id",
                (region, at_iso),
            )
            row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return row[0]

    def close(self, cycle_id: int, result: CycleResult) -> None:
        """Update an open cycle row with the final result.

        Raises ``RuntimeError`` if ``cycle_id`` is not found (rowcount == 0).
        Uses ``BEGIN IMMEDIATE`` per ADR-016.
        """
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE cycles"
                " SET finished_at = ?,"
                "     status = ?,"
                "     lots_fetched = ?,"
                "     new_lots = ?,"
                "     error = ?,"
                "     id_schema_check = ?"
                " WHERE id = ?",
                (
                    _iso(result.finished_at),
                    result.status,
                    result.lots_fetched,
                    result.new_lots,
                    result.error,
                    result.id_schema_check,
                    cycle_id,
                ),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise RuntimeError(f"cycle not found: id={cycle_id}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def list_recent(self, limit: int) -> list[CycleResult]:
        """Return the most recently completed cycles, ordered by ``started_at DESC``.

        Excludes open cycles (``status='open'``).
        Read-only — no explicit transaction (auto-commit, per ADR-016).
        """
        conn = self._conn_provider.get_connection()
        cur = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM cycles"
            " WHERE status != 'open'"
            " ORDER BY started_at DESC"
            " LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        return [_row_to_result(row) for row in rows]

    # ------------------------------------------------------------------
    # Retention / maintenance
    # ------------------------------------------------------------------

    def prune_older_than(
        self, age: timedelta, *, batch_size: int = 1000
    ) -> int:
        """Delete closed cycles older than ``age`` in chunks of ``batch_size``.

        Each chunk runs in its own short ``BEGIN IMMEDIATE`` transaction so
        the writer-lock is never held for a large bulk DELETE (R3-M7).

        Algorithm:
          cutoff = clock.now() - age
          loop:
            BEGIN IMMEDIATE
            DELETE FROM cycles WHERE id IN (
              SELECT id FROM cycles
              WHERE started_at < cutoff
              ORDER BY started_at ASC LIMIT batch_size
            )
            COMMIT
            stop when rowcount == 0

        Returns total number of rows deleted.
        """
        cutoff = _iso(self._clock.now() - age)
        conn = self._conn_provider.get_connection()
        total_deleted = 0

        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "DELETE FROM cycles"
                    " WHERE id IN ("
                    "   SELECT id FROM cycles"
                    "   WHERE started_at < ?"
                    "   ORDER BY started_at ASC"
                    "   LIMIT ?"
                    " )",
                    (cutoff, batch_size),
                )
                deleted = cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            total_deleted += deleted
            if deleted == 0:
                break

        return total_deleted
