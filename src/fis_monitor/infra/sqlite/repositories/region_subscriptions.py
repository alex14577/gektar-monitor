"""SQLite implementation of ``RegionSubscriptionRepository`` (ADR-039).

Stores per-region subscription timestamps in the ``region_subscriptions`` table.
Used by ``notifier_dispatcher`` to suppress lots older than the cutoff, and by
``WatchdogConfigSource`` to record new regions on config reload.

See:
    docs/decisions/ADR-039-subscribed-at-region-cutoff.md
    docs/decisions/ADR-016-repository-invariants-begin-immediate.md
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.infra.sqlite.connection import ConnectionProvider


def _parse_dt(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class SqliteRegionSubscriptionRepository:
    """SQLite-backed ``RegionSubscriptionRepository``.

    DI via constructor::

        repo = SqliteRegionSubscriptionRepository(conn_provider=provider)

    Write methods (``set_if_absent``, ``delete``) use ``BEGIN IMMEDIATE``
    (ADR-016).  ``get_subscribed_at`` is read-only.
    """

    def __init__(self, conn_provider: ConnectionProvider) -> None:
        self._conn_provider = conn_provider

    def get_subscribed_at(self, region_id: int) -> datetime | None:
        """Return the ``subscribed_at`` timestamp, or ``None`` if not present."""
        conn = self._conn_provider.get()
        cur = conn.execute(
            "SELECT subscribed_at FROM region_subscriptions WHERE region_id = ?",
            (region_id,),
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        return _parse_dt(row[0])

    def set_if_absent(self, region_id: int, subscribed_at: datetime) -> bool:
        """Insert ``(region_id, subscribed_at)`` only if no row exists yet.

        Uses ``INSERT OR IGNORE`` — idempotent, never overwrites.

        Returns:
            ``True`` if a new row was inserted, ``False`` if it already existed.
        """
        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO region_subscriptions (region_id, subscribed_at)"
                " VALUES (?, ?)",
                (region_id, subscribed_at.isoformat()),
            )
            inserted = cur.rowcount == 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return inserted

    def delete(self, region_id: int) -> None:
        """Remove the subscription record for ``region_id`` (idempotent)."""
        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM region_subscriptions WHERE region_id = ?",
                (region_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def list_subscribed_region_ids(self) -> frozenset[int]:
        """Return the set of all currently subscribed region IDs."""
        conn = self._conn_provider.get()
        cur = conn.execute("SELECT region_id FROM region_subscriptions")
        result = frozenset(row[0] for row in cur.fetchall())
        cur.close()
        return result
