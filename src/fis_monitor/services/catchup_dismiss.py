"""CatchupDismissService — persist and check the catchup banner dismissal state.

Architecture: services layer (Layer 2).
Depends on SettingsRepository (KV) and Clock via constructor injection.

The dismissal is stored as an ISO-8601 timestamp in the ``state`` table
under the key ``catchup_dismissed_until``.  While ``now < stored_ts`` the
banner is considered dismissed.

DI pattern: accepts ``SettingsRepository`` and ``Clock`` Protocol objects.
Both are satisfied by their respective SQLite implementations in production
and by in-memory fakes in tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import Clock, SettingsRepository

__all__ = ["CatchupDismissService"]

# KV key used in the ``state`` table.
_DISMISSED_UNTIL_KEY = "catchup_dismissed_until"


class CatchupDismissService:
    """Track catch-up banner dismissal in a durable KV store.

    DI via constructor:
        svc = CatchupDismissService(state_repo=..., clock=...)

    ``state_repo`` must satisfy ``SettingsRepository`` (get/set).
    ``clock`` must satisfy ``Clock`` (now()).

    Persistence window: ``hours`` parameter in ``dismiss()`` — defaults to 24.
    """

    def __init__(self, state_repo: SettingsRepository, clock: Clock) -> None:
        self._repo = state_repo
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dismiss(self, now: datetime, hours: int = 24) -> None:
        """Record dismissal; banner stays hidden until ``now + hours``.

        *now* must be timezone-aware.  The timestamp is stored as an
        ISO-8601 string so it survives process restarts.
        """
        until = now + timedelta(hours=hours)
        self._repo.set(_DISMISSED_UNTIL_KEY, until.isoformat())

    def is_dismissed(self, now: datetime) -> bool:
        """Return ``True`` if the banner is currently dismissed.

        False when: no dismissal has been recorded, or the window has expired.
        *now* must be timezone-aware (same TZ as stored value).

        Defensive timezone handling (P1-7): ``fromisoformat`` may return a
        naive datetime for legacy stored values; comparing naive and aware
        datetimes raises ``TypeError`` in Python 3.11+.  We apply the same
        UTC-coercion pattern as ``DndService.until()``.
        """
        raw = self._repo.get(_DISMISSED_UNTIL_KEY)
        if raw is None:
            return False
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return False
        # Ensure both sides are timezone-aware before comparison (defensive).
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now < until
