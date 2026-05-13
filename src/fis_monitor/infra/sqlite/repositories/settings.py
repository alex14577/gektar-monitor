"""SqliteSettingsRepository — key/value store on the ``state`` table.

Architecture: infra/sqlite layer (Layer 3).
Implements ``domain.interfaces.SettingsRepository`` Protocol.

BEGIN IMMEDIATE is used only for write operations (``set``, ``set_onboarding``).
Read operations (``get``, ``get_onboarding``) run without an explicit
transaction — SQLite snapshot isolation gives them a consistent view.

See:
    - docs/decisions/ADR-016-repository-invariants-begin-immediate.md
    - docs/architecture/03-protocols.md §SettingsRepository
"""

from __future__ import annotations

import sqlite3

from fis_monitor.domain.interfaces import Clock, ConnectionProvider
from fis_monitor.domain.models import OnboardingState

# Key used to persist the onboarding FSM state.
_ONBOARDING_KEY = "onboarding_state"


class SqliteSettingsRepository:
    """SQLite-backed key/value repository over the ``state`` table.

    DI: accepts ``ConnectionProvider`` and ``Clock`` via constructor.
    Thread-safe: ConnectionProvider returns per-thread connections.
    """

    def __init__(self, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Generic k/v
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the value for *key*, or ``None`` if not present."""
        conn: sqlite3.Connection = self._conn_provider.get_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    def set(self, key: str, value: str) -> None:
        """Upsert *key*/*value* inside a BEGIN IMMEDIATE transaction (ADR-016)."""
        conn: sqlite3.Connection = self._conn_provider.get_connection()
        now = self._clock.now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO state(key, value, updated_at)
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

    # ------------------------------------------------------------------
    # Onboarding FSM helpers
    # ------------------------------------------------------------------

    def get_onboarding(self) -> OnboardingState:
        """Return the current onboarding FSM state.

        Defaults to ``OnboardingState.NOT_STARTED`` when no row is present.
        """
        raw = self.get(_ONBOARDING_KEY)
        if raw is None:
            return OnboardingState.NOT_STARTED
        return OnboardingState(raw)

    def set_onboarding(self, st: OnboardingState) -> None:
        """Persist *st* as the current onboarding FSM state."""
        self.set(_ONBOARDING_KEY, st.value)
