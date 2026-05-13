"""SQLite implementation of `NotificationsRepository`.

State machine: pending → sent | permanent_fail.
PK: (lot_id, channel, recipient).

Each method opens and closes its own short ``BEGIN IMMEDIATE`` transaction.
No two methods share a transaction.  Network I/O (SMTP send) goes BETWEEN
``mark_attempt`` and ``mark_sent`` — never inside a tx.

See ADR-019, notifications.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fis_monitor.domain.interfaces import Clock
from fis_monitor.domain.models import NotificationRecord
from fis_monitor.infra.sqlite.connection import ConnectionProvider


def _row_to_record(row: tuple) -> NotificationRecord:
    """Convert a DB row (lot_id, channel, recipient, status, attempt_no,
    last_attempt_at, sent_at) to a ``NotificationRecord``."""
    lot_id, channel, recipient, status, attempt_no, last_attempt_at_raw, sent_at_raw = row

    last_attempt_at: datetime | None = None
    if last_attempt_at_raw is not None:
        parsed = datetime.fromisoformat(last_attempt_at_raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        last_attempt_at = parsed

    sent_at: datetime | None = None
    if sent_at_raw is not None:
        parsed = datetime.fromisoformat(sent_at_raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        sent_at = parsed

    return NotificationRecord(
        lot_id=lot_id,
        channel=channel,
        recipient=recipient,
        status=status,
        attempt_no=attempt_no,
        last_attempt_at=last_attempt_at,
        sent_at=sent_at,
    )


_SELECT_COLUMNS = (
    "lot_id, channel, recipient, status, attempt_no, last_attempt_at, sent_at"
)


class SqliteNotificationsRepository:
    """SQLite-backed ``NotificationsRepository``.

    Responsibilities (SRP): CRUD + tx-invariants for the ``notifications``
    table.  Business rules (retry backoff, cap) live in ``NotifierDispatcher``.

    DI via constructor:
        repo = SqliteNotificationsRepository(conn_provider, clock)

    ``clock`` must be UTC-aware (``Clock`` Protocol from domain/interfaces.py).
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API — NotificationsRepository Protocol
    # ------------------------------------------------------------------

    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
        """Create a pending slot if it does not exist (INSERT OR IGNORE).

        Returns ``True`` if a new row was created (slot is fresh),
        ``False`` if the slot already existed in any status.
        """
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notifications"
                " (lot_id, channel, recipient, status, attempt_no)"
                " VALUES (?, ?, ?, 'pending', 0)",
                (lot_id, channel, recipient),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return cur.rowcount == 1

    def status_of(
        self, lot_id: int, channel: str, recipient: str
    ) -> Literal["pending", "sent", "permanent_fail"] | None:
        """Return the current status, or ``None`` if no slot exists yet."""
        conn = self._conn_provider.get_connection()
        cur = conn.execute(
            "SELECT status FROM notifications"
            " WHERE lot_id = ? AND channel = ? AND recipient = ?",
            (lot_id, channel, recipient),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row[0]  # type: ignore[return-value]

    def mark_attempt(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> int | None:
        """Increment ``attempt_no`` and set ``last_attempt_at``.

        Returns the new ``attempt_no`` on success.
        Returns ``None`` if the row is already in a terminal status
        (``sent`` or ``permanent_fail``) — R4-C4 race; caller MUST skip send.
        """
        at_iso = at.isoformat()
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE notifications"
                " SET attempt_no = attempt_no + 1, last_attempt_at = ?"
                " WHERE lot_id = ? AND channel = ? AND recipient = ?"
                "   AND status = 'pending'"
                " RETURNING attempt_no",
                (at_iso, lot_id, channel, recipient),
            )
            row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if row is None:
            return None
        return row[0]

    def mark_sent(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> None:
        """Transition ``pending → sent``.  No-op if already in another status."""
        at_iso = at.isoformat()
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE notifications"
                " SET status = 'sent', sent_at = ?"
                " WHERE lot_id = ? AND channel = ? AND recipient = ?"
                "   AND status = 'pending'",
                (at_iso, lot_id, channel, recipient),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mark_permanent_fail(
        self, lot_id: int, channel: str, recipient: str
    ) -> None:
        """Transition ``pending → permanent_fail``.  No-op if already terminal."""
        conn = self._conn_provider.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE notifications"
                " SET status = 'permanent_fail'"
                " WHERE lot_id = ? AND channel = ? AND recipient = ?"
                "   AND status = 'pending'",
                (lot_id, channel, recipient),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
        """Recovery query: pending rows older than ``age``.

        Includes zombie-reserves (``last_attempt_at IS NULL``) — R4-C3.
        Cutoff = now() - age.
        """
        cutoff = (self._clock.now() - age).isoformat()
        conn = self._conn_provider.get_connection()
        cur = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notifications"
            " WHERE status = 'pending'"
            "   AND (last_attempt_at IS NULL OR last_attempt_at < ?)",
            (cutoff,),
        )
        rows = cur.fetchall()
        return [_row_to_record(row) for row in rows]

    def list_recent(self, limit: int) -> list[NotificationRecord]:
        """Return the most recently sent notifications (``status='sent'``),
        ordered by ``sent_at DESC``.
        """
        conn = self._conn_provider.get_connection()
        cur = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notifications"
            " WHERE status = 'sent'"
            " ORDER BY sent_at DESC"
            " LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        return [_row_to_record(row) for row in rows]
