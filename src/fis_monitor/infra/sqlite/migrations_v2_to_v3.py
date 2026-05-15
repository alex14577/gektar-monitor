"""Migration v2 → v3: smtp_credentials.smtp_from_name column (bd ljp).

This module provides the `v2_to_v3` callable for use with `Migration` dataclass
in `migrations.py`. The function runs INSIDE the runner's BEGIN IMMEDIATE
transaction — it MUST NOT open its own BEGIN/COMMIT/ROLLBACK and MUST NOT
call PRAGMA user_version.

Scope:
  - `smtp_credentials`: ADD COLUMN smtp_from_name TEXT (nullable, no DEFAULT
    constraint needed — SQLite allows ADD COLUMN for nullable columns without
    a DEFAULT).

Migration notes:
  - Additive only: existing rows get smtp_from_name = NULL which is the correct
    semantic (no display name → bare email address in From: header).
  - Column order after migration on existing DBs:
        id, smtp_user, smtp_password, use_default, updated_at, smtp_host,
        smtp_port, smtp_from_name
    (different from schema.sql declaration order due to prior ALTER TABLE
    migrations A.M1 from v1→v2). Access MUST be by name — never by positional
    index.

See:
  - docs/data-model/settings.md §SmtpCredentials
  - docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

import sqlite3


def v2_to_v3(conn: sqlite3.Connection) -> None:
    """Apply v2→v3 schema migration.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

    Args:
        conn: Open SQLite connection inside an active BEGIN IMMEDIATE tx.
    """
    _migrate_smtp_credentials(conn)


def _migrate_smtp_credentials(conn: sqlite3.Connection) -> None:
    """ADD COLUMN smtp_from_name TEXT to smtp_credentials (bd ljp).

    Nullable with no DEFAULT — existing rows get NULL (correct: bare email).
    No rebuild needed: SQLite supports ADD COLUMN for nullable columns.

    ВНИМАНИЕ — column order divergence (A.M2):
    After this migration on an upgraded DB the physical column order is:
        id, smtp_user, smtp_password, use_default, updated_at, smtp_host,
        smtp_port, smtp_from_name
    On a greenfield install (schema.sql) the order is as declared in schema.sql.
    Always use named access via sqlite3.Row or explicit column list in SELECT.
    """
    conn.execute(
        "ALTER TABLE smtp_credentials ADD COLUMN smtp_from_name TEXT"
    )
