"""Migration v1 → v2: notifications state-machine + smtp_credentials host/port.

This module provides the `v1_to_v2` callable for use with `Migration` dataclass
in `migrations.py`. The function runs INSIDE the runner's BEGIN IMMEDIATE
transaction — it MUST NOT open its own BEGIN/COMMIT/ROLLBACK and MUST NOT
call PRAGMA user_version.

Scope:
  - `notifications`: 12-step rebuild (SQLite ALTER TABLE cannot loosen NOT NULL
    on `sent_at`).  All v1 rows are treated as successful sends → status='sent',
    attempt_no=1, last_attempt_at=sent_at.
  - `smtp_credentials`: two ADD COLUMN statements (additive, no NOT NULL
    loosening needed — SQLite supports ADD COLUMN with NOT NULL when a DEFAULT
    is supplied).

FK-toggling note:
  SQLite requires PRAGMA foreign_keys=OFF **outside** of any transaction for the
  12-step table-rebuild to work safely (FK constraints would otherwise block
  DROP TABLE if referencing tables exist).  However:
  (a) `notifications` has NO foreign-key references pointing **to** it (nothing
      references notifications.lot_id with REFERENCES notifications).
  (b) `notifications` itself references no other table via FK (lot_id is NOT
      declared as a REFERENCES constraint in the v1 schema).
  Therefore foreign_keys toggling is intentionally omitted here.

  For future migrations that involve tables with FK references, the caller
  (composition root / init_db) MUST issue PRAGMA foreign_keys=OFF before
  entering BEGIN IMMEDIATE, and restore it after COMMIT.  This is a known
  architectural constraint documented here as an explicit decision.

See:
  - `docs/decisions/ADR-019-notification-state-machine.md` §R4-M8
  - `docs/decisions/ADR-020-smtp-host-port-ssot-state-db.md`
  - https://sqlite.org/lang_altertable.html#otheralter (12-step rebuild)
"""

import sqlite3


def v1_to_v2(conn: sqlite3.Connection) -> None:
    """Apply v1→v2 schema migration.

    Runs INSIDE the runner's BEGIN IMMEDIATE transaction.
    MUST NOT issue BEGIN/COMMIT/ROLLBACK or PRAGMA user_version.

    Args:
        conn: Open SQLite connection inside an active BEGIN IMMEDIATE tx.
    """
    _migrate_notifications(conn)
    _migrate_smtp_credentials(conn)


def _migrate_notifications(conn: sqlite3.Connection) -> None:
    """12-step rebuild for `notifications` table.

    v1 schema (inferred from ADR-019 R4-M8 context):
      lot_id INTEGER NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
      sent_at TIMESTAMP NOT NULL,
      PRIMARY KEY (lot_id, channel, recipient)

    v2 schema (from docs/db/schema.sql):
      lot_id INTEGER NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (...),
      attempt_no INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TIMESTAMP,        -- nullable
      sent_at TIMESTAMP,                -- nullable (loosened from NOT NULL)
      PRIMARY KEY (lot_id, channel, recipient)

    Data migration rule (R4-M8):
      All v1 rows are completed sends → status='sent', attempt_no=1,
      last_attempt_at=sent_at (v1 value), sent_at preserved as-is.
    """
    # Step 1: Create notifications_new with v2 schema.
    conn.execute("""
        CREATE TABLE notifications_new (
            lot_id          INTEGER NOT NULL,
            channel         TEXT    NOT NULL,
            recipient       TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending', 'sent', 'permanent_fail')),
            attempt_no      INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMP,
            sent_at         TIMESTAMP,
            PRIMARY KEY (lot_id, channel, recipient)
        )
    """)

    # Step 2: Copy existing v1 data.
    # v1 rows all have sent_at NOT NULL (completed sends).
    # Map: status='sent', attempt_no=1, last_attempt_at=sent_at (original).
    conn.execute("""
        INSERT INTO notifications_new
            (lot_id, channel, recipient, status, attempt_no, last_attempt_at, sent_at)
        SELECT
            lot_id,
            channel,
            recipient,
            'sent',
            1,
            sent_at,
            sent_at
        FROM notifications
    """)

    # Steps 3-4: Drop old indexes before dropping the table.
    # idx_notifications_sent_at existed in v1 as a full (non-partial) index.
    # idx_notifications_channel existed in v1.
    conn.execute("DROP INDEX IF EXISTS idx_notifications_sent_at")
    conn.execute("DROP INDEX IF EXISTS idx_notifications_channel")

    # Step 5: Drop old table.
    conn.execute("DROP TABLE notifications")

    # Step 6: Rename new table to canonical name.
    conn.execute("ALTER TABLE notifications_new RENAME TO notifications")

    # Step 7: Create v2 indexes (R4-M9 + R4-C3).
    # Partial DESC index for list_recent / audit queries (status='sent' only).
    conn.execute("""
        CREATE INDEX idx_notifications_sent_at
            ON notifications(sent_at DESC)
            WHERE status = 'sent'
    """)
    # Channel index for per-channel queries.
    conn.execute("""
        CREATE INDEX idx_notifications_channel
            ON notifications(channel, sent_at)
    """)
    # Partial index for recovery: pending records (including NULL last_attempt_at
    # zombie-reserves created by reserve() before first mark_attempt(), per R4-C3).
    conn.execute("""
        CREATE INDEX idx_notifications_pending
            ON notifications(last_attempt_at)
            WHERE status = 'pending'
    """)

    # Step 8: Best-practice FK check (no-op for notifications but kept for
    # consistency; validates referential integrity of the rebuilt table).
    conn.execute("PRAGMA foreign_key_check(notifications)")


def _migrate_smtp_credentials(conn: sqlite3.Connection) -> None:
    """ADD COLUMN migration for `smtp_credentials` (ADR-020, R4-C1).

    v1 smtp_credentials lacked smtp_host and smtp_port columns.
    SQLite allows ADD COLUMN with NOT NULL when a DEFAULT is provided and the
    table already exists — no rebuild needed here.

    Defaults reflect the bot-mailbox literals (smtp.yandex.ru:587).
    After migration callers should update these to the user's actual values
    if they differ from the defaults.

    ВНИМАНИЕ — column order divergence (A.M1):
    `ALTER TABLE ... ADD COLUMN` appends new columns to the end of the row.
    After migration the physical column order is:
        id, smtp_user, smtp_password, use_default, updated_at, smtp_host, smtp_port
    which DIFFERS from schema.sql declaration order:
        id, smtp_user, smtp_password, smtp_host, smtp_port, use_default, updated_at
    This is safe ONLY when access is by name (sqlite3.Row + SELECT with an
    explicit column list).  Any `SELECT *` followed by positional index access
    WILL break on a migrated database.  On a greenfield install the order
    matches schema.sql exactly.
    """
    conn.execute(
        "ALTER TABLE smtp_credentials ADD COLUMN smtp_host TEXT NOT NULL"
        " DEFAULT 'smtp.yandex.ru'"
    )
    conn.execute(
        "ALTER TABLE smtp_credentials ADD COLUMN smtp_port INTEGER NOT NULL"
        " DEFAULT 587 CHECK (smtp_port BETWEEN 1 AND 65535)"
    )
