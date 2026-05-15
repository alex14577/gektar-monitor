"""SqliteStateRepository — generic KV store over the ``state`` table.

Architecture: infra/sqlite layer (Layer 2).
Implements ``domain.interfaces.StateRepository`` Protocol.

The ``state`` table schema:
    key        TEXT PRIMARY KEY
    value      TEXT
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP

Known key namespaces are documented in ``docs/db/schema.sql`` (state table
comment block).  This repository has no opinion on key names — callers own
that contract.

Write methods use ``BEGIN IMMEDIATE`` (ADR-016).
Read methods run without an explicit transaction (snapshot isolation).

See:
    - docs/decisions/ADR-016-repository-invariants-begin-immediate.md
    - docs/db/schema.sql (state table + key-namespace comment)
"""

from __future__ import annotations

import sqlite3

from fis_monitor.domain.interfaces import Clock, ConnectionProvider


class SqliteStateRepository:
    """SQLite-backed generic key/value repository over the ``state`` table.

    Implements the ``StateRepository`` Protocol (``domain/interfaces.py``).

    DI via constructor:
        repo = SqliteStateRepository(conn_provider=..., clock=...)

    ``clock`` must return UTC-aware datetimes (``Clock`` Protocol).
    ``conn_provider`` returns per-thread connections (``ConnectionProvider``).

    Write methods (``set``, ``delete``) use ``BEGIN IMMEDIATE`` to prevent
    lost-update races (ADR-016).  ``get`` is non-transactional.
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection = self._conn_provider.get()
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Read (no BEGIN IMMEDIATE)
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the stored value for *key*, or ``None`` if absent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    # ------------------------------------------------------------------
    # Write (BEGIN IMMEDIATE, ADR-016)
    # ------------------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        """Upsert *key* / *value* inside a ``BEGIN IMMEDIATE`` transaction.

        ``updated_at`` is set to ``clock.now()`` on every call, including
        no-op overwrites with the same value.
        """
        conn = self._get_conn()
        now = self._clock.now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                    SET value      = excluded.value,
                        updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def delete(self, key: str) -> None:
        """Remove *key* from the store.

        Idempotent: deleting a non-existent key is a no-op (no exception).
        Executes inside a ``BEGIN IMMEDIATE`` transaction (ADR-016).
        """
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM state WHERE key = ?", (key,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
