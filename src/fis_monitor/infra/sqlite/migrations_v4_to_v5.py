"""Migration v4 → v5: lots.date_registry column.

Scope:
  - ``lots``: ADD COLUMN ``date_registry TIMESTAMP NULL`` — дата постановки
    на учёт в ЕГРН, извлекается из detail-страницы
    ``/cabinet/free-lot-view?id=N`` по ключу «Дата постановки на учет».
    Отличается от ``date_create`` (дата добавления лота в ФИС-БД).

This function runs INSIDE the runner's BEGIN IMMEDIATE transaction.
MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

See:
    docs/decisions/ADR-040-egrn-registration-date.md
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

import sqlite3


def v4_to_v5(conn: sqlite3.Connection) -> None:
    """Apply v4→v5 schema migration.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.
    """
    conn.execute("ALTER TABLE lots ADD COLUMN date_registry TIMESTAMP")
