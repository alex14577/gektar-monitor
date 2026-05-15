"""SqliteSmtpCredentialsRepository — singleton SMTP credentials (id=1).

Architecture: infra/sqlite layer (Layer 3).
Implements ``domain.interfaces.SmtpCredentialsRepository`` Protocol.

ADR-020: smtp_host / smtp_port SSOT = state.db (not config.json).
ADR-017: smtp_password stored as plaintext in DB (ACL-protected), wrapped
         in SecretStr on load; extracted via get_secret_value() on save.

CRITICAL — column order divergence (A.M1 from migrations_v1_to_v2.py):
    After ALTER TABLE ADD COLUMN migration the physical column order is:
        id, smtp_user, smtp_password, use_default, updated_at, smtp_host, smtp_port
    which differs from schema.sql declaration order. Access MUST be by name
    only. NEVER use positional index access on rows from this table.
    We always SELECT with an explicit named column list.

See:
    - docs/decisions/ADR-020-smtp-host-port-ssot-state-db.md
    - docs/decisions/ADR-016-repository-invariants-begin-immediate.md
    - docs/decisions/ADR-017-secrets-secretstr-crash-dump-exclusion.md
"""

from __future__ import annotations

import sqlite3

from pydantic import SecretStr

from fis_monitor.domain.interfaces import Clock, ConnectionProvider
from fis_monitor.domain.models import SmtpCredentials

# Singleton row id — enforced by CHECK (id = 1) in schema.sql.
_SINGLETON_ID = 1


class SqliteSmtpCredentialsRepository:
    """SQLite-backed repository for the singleton SMTP credentials row.

    DI: accepts ``ConnectionProvider`` and ``Clock`` via constructor.
    Thread-safe: ConnectionProvider returns per-thread connections.
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> SmtpCredentials | None:
        """Return the singleton SMTP credentials row, or ``None`` if absent.

        Columns accessed by name (migration A.M1 divergence guard).
        smtp_password is wrapped in SecretStr (ADR-017).
        use_default is stored as INTEGER 0/1 — converted to bool on load.
        """
        conn: sqlite3.Connection = self._conn_provider.get()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT smtp_user,
                   smtp_password,
                   smtp_host,
                   smtp_port,
                   use_default,
                   smtp_from_name
            FROM smtp_credentials
            WHERE id = ?
            """,
            (_SINGLETON_ID,),
        ).fetchone()
        if row is None:
            return None
        return SmtpCredentials(
            smtp_user=row["smtp_user"],
            smtp_password=SecretStr(row["smtp_password"]),  # ADR-017
            smtp_host=row["smtp_host"],
            smtp_port=row["smtp_port"],
            use_default=bool(row["use_default"]),
            from_name=row["smtp_from_name"] or None,
        )

    def save(self, creds: SmtpCredentials) -> None:
        """Atomically replace the singleton row (BEGIN IMMEDIATE; INSERT OR REPLACE).

        All 4 SMTP fields (host, port, user, password) are written in one
        transaction for atomicity — no race window (ADR-020).
        smtp_password: get_secret_value() for plaintext storage (ADR-017).
        use_default: bool → int (0/1) for SQLite CHECK constraint.
        """
        conn: sqlite3.Connection = self._conn_provider.get()
        now = self._clock.now().isoformat()
        # ADR-017: get_secret_value() — only place where plaintext is extracted.
        password_plain = creds.smtp_password.get_secret_value()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO smtp_credentials
                    (id, smtp_user, smtp_password, smtp_host, smtp_port,
                     use_default, smtp_from_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _SINGLETON_ID,
                    creds.smtp_user,
                    password_plain,
                    creds.smtp_host,
                    creds.smtp_port,
                    int(creds.use_default),
                    creds.from_name or None,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
