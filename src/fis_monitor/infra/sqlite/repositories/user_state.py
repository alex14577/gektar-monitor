"""SqliteUserStateRepository — per-lot user interaction state.

Architecture: infra/sqlite layer (Layer 3).
Implements ``domain.interfaces.UserStateRepository`` Protocol.

Tables:
- ``lot_user_state`` — per-lot flags, notes, timestamps.
- ``state`` (KV) — global ``last_visit_at`` key.

Write methods use BEGIN IMMEDIATE (ADR-016).
Read methods run without an explicit transaction (snapshot isolation).

See:
    - docs/decisions/ADR-016-repository-invariants-begin-immediate.md
    - docs/architecture/03-protocols.md §UserStateRepository
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from fis_monitor.domain.interfaces import Clock, ConnectionProvider
from fis_monitor.domain.models import LotUserState

# KV key for the global last-visit timestamp.
_LAST_VISIT_KEY = "last_visit_at"

# Maximum number of ids to pass in a single IN (...) query.
# SQLite default SQLITE_LIMIT_VARIABLE_NUMBER is 999.
_CHUNK_SIZE = 500


def _parse_datetime(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string from SQLite into an aware datetime.

    If the stored value lacks timezone info (e.g. written by a legacy path),
    it is treated as UTC.  Returns ``None`` when *raw* is ``None``.
    """
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _row_to_state(row: sqlite3.Row) -> LotUserState:
    """Convert a ``lot_user_state`` DB row to a ``LotUserState`` model."""
    return LotUserState(
        lot_id=row["lot_id"],
        submitted=bool(row["submitted"]),
        submitted_at=_parse_datetime(row["submitted_at"]),
        note=row["note"],
        seen_at=_parse_datetime(row["seen_at"]),
        updated_at=_parse_datetime(row["updated_at"]),  # type: ignore[arg-type]
    )


class SqliteUserStateRepository:
    """SQLite-backed repository for per-lot user state.

    Implements the ``UserStateRepository`` Protocol (domain/interfaces.py).

    DI via constructor:
        repo = SqliteUserStateRepository(conn_provider=..., clock=...)

    ``clock`` must return UTC-aware datetimes (``Clock`` Protocol).
    ``conn_provider`` returns per-thread connections (``ConnectionProvider``).

    Write methods (``set_*``, ``mark_visited``) use BEGIN IMMEDIATE to
    prevent lost-update races (ADR-016). Read methods are non-transactional.

    ``get_many`` supports up to ``_CHUNK_SIZE`` (500) ids per query; for
    larger id sets the method chunks internally.  Callers passing very large
    id sets should be aware of this overhead.
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = self._conn_provider.get()
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Read methods (no BEGIN IMMEDIATE)
    # ------------------------------------------------------------------

    def get(self, lot_id: int) -> LotUserState | None:
        """Return ``LotUserState`` for *lot_id*, or ``None`` if absent."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT lot_id, submitted, submitted_at, note, seen_at, updated_at"
            " FROM lot_user_state WHERE lot_id = ?",
            (lot_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    def get_many(self, ids: Sequence[int]) -> dict[int, LotUserState]:
        """Return mapping ``lot_id → LotUserState`` for every id in *ids*.

        Missing ids are absent from the result (no KeyError).  Empty *ids*
        returns ``{}`` immediately without touching the DB (empty ``IN ()``
        is a SQL syntax error in SQLite).

        Internally chunks *ids* into batches of at most ``_CHUNK_SIZE`` (500)
        to stay within SQLite's variable-number limit.
        """
        if not ids:
            return {}

        result: dict[int, LotUserState] = {}
        id_list = list(ids)

        for offset in range(0, len(id_list), _CHUNK_SIZE):
            chunk = id_list[offset : offset + _CHUNK_SIZE]
            placeholders = ",".join("?" * len(chunk))
            conn = self._get_conn()
            rows = conn.execute(
                f"SELECT lot_id, submitted, submitted_at, note, seen_at, updated_at"
                f" FROM lot_user_state WHERE lot_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                state = _row_to_state(row)
                result[state.lot_id] = state

        return result

    def last_visit(self) -> datetime | None:
        """Return the timestamp of the last ``mark_visited()`` call.

        Returns ``None`` if the dashboard has never been visited.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?", (_LAST_VISIT_KEY,)
        ).fetchone()
        if row is None:
            return None
        return _parse_datetime(row["value"])

    # ------------------------------------------------------------------
    # Write methods (BEGIN IMMEDIATE, ADR-016)
    # ------------------------------------------------------------------

    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None:
        """Set the ``submitted`` flag and optional ``submitted_at`` for *lot_id*.

        Uses UPSERT — only ``submitted``, ``submitted_at``, and ``updated_at``
        are touched; other columns retain their current or DEFAULT values.
        """
        conn = self._get_conn()
        now = self._clock.now().isoformat()
        submitted_at_iso = at.isoformat() if at is not None else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO lot_user_state (lot_id, submitted, submitted_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lot_id) DO UPDATE
                    SET submitted    = excluded.submitted,
                        submitted_at = excluded.submitted_at,
                        updated_at   = excluded.updated_at
                """,
                (lot_id, int(value), submitted_at_iso, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def set_note(self, lot_id: int, note: str | None) -> None:
        """Set (or clear) the free-text ``note`` for *lot_id*.

        Passing ``None`` clears any existing note.
        Uses UPSERT — only ``note`` and ``updated_at`` are affected.
        """
        conn = self._get_conn()
        now = self._clock.now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO lot_user_state (lot_id, note, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(lot_id) DO UPDATE
                    SET note       = excluded.note,
                        updated_at = excluded.updated_at
                """,
                (lot_id, note, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def mark_visited(self, at: datetime) -> None:
        """Record *at* as the timestamp of the most recent dashboard visit.

        *at* MUST be timezone-aware (UTC recommended).  Passing a naive
        ``datetime`` raises ``ValueError`` — this matches the contract used
        by ``SqliteNotificationsRepository`` (akv.6).

        The ``at`` argument comes from the caller (e.g. ``clock.now()`` in the
        UI route) — this method does NOT read ``self._clock`` internally.
        """
        if at.tzinfo is None:
            raise ValueError(
                "mark_visited() requires a timezone-aware datetime; got naive datetime."
            )
        conn = self._get_conn()
        now = self._clock.now().isoformat()
        value = at.isoformat()
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
                (_LAST_VISIT_KEY, value, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
