"""Database initialisation with pre-flight schema version check.

Implements the FIXME captured in docs/architecture/03-protocols.md §3.1 line 177
(R5 review — DB): init_db() MUST perform a pre-flight PRAGMA user_version check
before applying or skipping the schema.

Design decisions (brainstorm akv.2, locked — do not re-open):
* MigrationRunner is typed as Callable[[sqlite3.Connection, int, int], None] | None
  (not a Protocol) to keep this module dependency-free of any runner class.
  The concrete ``SqliteMigrationRunner`` is introduced in akv.4
  (``infra/sqlite/migrations.py``); its ``__call__`` matches this signature.
* user_version is read OUTSIDE any transaction — PRAGMA user_version is a
  meta-operation and must not be wrapped in a writer tx (BEGIN IMMEDIATE).
* Schema is applied via executescript() which manages its own transaction
  internally; no BEGIN IMMEDIATE wrapper around it.
* PII contract: RuntimeError message contains ONLY version integers — no file
  path, no database file name, no user data.

Layer: infra/sqlite (Layer 0). No domain Protocol dependency.
Collaborator: ConnectionProvider (infra-internal, concrete class).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from fis_monitor.domain.errors import MigrationRequired
from fis_monitor.infra.sqlite.connection import ConnectionProvider

# Type alias for the optional migration callable.
# Signature: (conn, from_version, to_version) -> None
MigrationRunner = Callable[[sqlite3.Connection, int, int], None]


def init_db(
    provider: ConnectionProvider,
    *,
    schema_sql: str,
    latest_version: int = 2,
    migration_runner: MigrationRunner | None = None,
) -> None:
    """Initialise or verify the SQLite database schema.

    Algorithm (all PRAGMA user_version reads are outside any writer tx):

    1. ``current == 0`` AND no tables exist (truly fresh DB):
       Apply ``schema_sql`` via ``executescript()`` then verify the resulting
       ``user_version`` matches ``latest_version``.

    2. ``current == latest_version``:
       No-op — DB is already up to date.

    3. ``current > latest_version``:
       Raise ``RuntimeError`` — app is older than the DB (downgrade scenario).
       Message contains only version integers (PII-safe).

    4. ``0 < current < latest_version``  OR  ``current == 0`` with existing tables
       (legacy DB without user_version stamp):
       - If ``migration_runner`` is provided: invoke it as
         ``migration_runner(conn, current, latest_version)``.
       - Otherwise: raise ``MigrationRequired(from_version=current,
         to_version=latest_version)``.

    Args:
        provider:         Infra-internal ConnectionProvider for the target DB.
        schema_sql:       Full DDL script (contents of ``docs/db/schema.sql``).
                          Ignored when no schema application is needed.
        latest_version:   Expected ``PRAGMA user_version`` after initialisation.
                          Defaults to 2 (current schema version per schema.sql).
        migration_runner: Optional callable that performs in-place schema upgrade.
                          Receives ``(conn, from_version, to_version)``.
                          If *None* and migration is needed, raises
                          ``MigrationRequired``.

    Raises:
        RuntimeError:       DB ``user_version`` is *greater* than ``latest_version``.
        MigrationRequired:  DB ``user_version`` is *less* than ``latest_version``
                            and no ``migration_runner`` was provided.
    """
    conn = provider.get()

    # Read current version OUTSIDE any transaction (PRAGMA is a meta-operation).
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current == latest_version:
        # Already up to date — fast path, no schema change.
        return

    if current > latest_version:
        raise RuntimeError(
            f"DB schema newer than app (got {current}, expected {latest_version})"
        )

    # current < latest_version OR current == 0
    # Distinguish fresh (no tables) from legacy (has tables but no user_version).
    if current == 0:
        has_tables = _has_user_tables(conn)
        if not has_tables:
            # Truly fresh database — apply the full schema.
            conn.executescript(schema_sql)
            # Verify that schema_sql stamped the correct user_version.
            actual: int = conn.execute("PRAGMA user_version").fetchone()[0]
            if actual != latest_version:
                raise RuntimeError(
                    f"Schema applied but user_version is {actual}, "
                    f"expected {latest_version}"
                )
            return
        # Fall through: user_version=0 with existing tables → treat as legacy.

    # Migration path: current < latest_version (including legacy zero-with-tables).
    if migration_runner is not None:
        migration_runner(conn, current, latest_version)
        # Verify that the runner actually stamped the expected user_version.
        actual_after: int = conn.execute("PRAGMA user_version").fetchone()[0]
        if actual_after != latest_version:
            raise RuntimeError(
                f"Migration runner returned but user_version is {actual_after}, "
                f"expected {latest_version}"
            )
    else:
        raise MigrationRequired(from_version=current, to_version=latest_version)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _has_user_tables(conn: sqlite3.Connection) -> bool:
    """Return True if the database contains any user-created tables.

    Excludes SQLite internal tables (``sqlite_*``).  Virtual tables (FTS5,
    R-tree) count as user tables because their presence signals a partially or
    fully initialised schema.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return (row[0] if row else 0) > 0
