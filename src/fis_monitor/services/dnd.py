"""DndService — Do-Not-Disturb business logic.

Persists a single ``dnd_until`` ISO-UTC timestamp in the ``state`` KV table
via ``SettingsRepository``.  All time-zone handling is explicit: values are
always stored and read as UTC-aware datetimes.

Architecture: services layer (Layer 2).
Depends on: ``domain.interfaces.SettingsRepository`` (Protocol), no infra.

Design:
  - SRP: only DnD logic, nothing else.
  - DI via constructor — no global state.
  - Pure predicate methods (``is_active``, ``until``) accept ``now`` so callers
    inject the clock and tests stay deterministic.

Dispatcher integration note (for orchestrator):
  Call ``dnd_service.is_active(clock.now())`` at the top of
  ``NotifierDispatcher._dispatch_all_channels()`` and return early when True.
  This suppresses all channel deliveries for the DnD window.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.interfaces import SettingsRepository

__all__ = ["DndService"]

# Key used in the ``state`` KV table.
_DND_UNTIL_KEY = "dnd_until"

# Validation limits (minutes).
_MIN_MINUTES = 1
_MAX_MINUTES = 1440 * 7  # 7 days


class DndService:
    """Do-Not-Disturb service.

    Args:
        settings_repo: KV repository backed by the ``state`` table.

    Methods:
        set_dnd_until(now, minutes) — activate DnD for ``minutes`` minutes.
        is_active(now)              — True while the DnD window is open.
        until(now)                  — the expiry datetime, or None if inactive.
    """

    def __init__(self, settings_repo: SettingsRepository) -> None:
        self._repo = settings_repo

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def set_dnd_until(self, now: datetime, minutes: int) -> None:
        """Activate DnD for *minutes* minutes from *now*.

        Args:
            now:     Current UTC-aware datetime (supplied by the caller/clock).
            minutes: Duration in minutes. Must be in [1, 10080].

        Raises:
            ValueError: if ``minutes`` is outside the valid range.
        """
        if minutes < _MIN_MINUTES or minutes > _MAX_MINUTES:
            raise ValueError(
                f"minutes must be between {_MIN_MINUTES} and {_MAX_MINUTES}, got {minutes}"
            )
        # Ensure now is UTC-aware for consistent ISO serialisation.
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        from datetime import timedelta

        until = now + timedelta(minutes=minutes)
        self._repo.set(_DND_UNTIL_KEY, until.isoformat())

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def is_active(self, now: datetime) -> bool:
        """Return True if the DnD window has not yet expired.

        Args:
            now: Current UTC-aware datetime.
        """
        expiry = self.until(now)
        return expiry is not None and now < expiry

    def until(self, now: datetime) -> datetime | None:
        """Return the DnD expiry datetime if the window is open, else None.

        Returns None when:
          - no ``dnd_until`` key has been written yet, or
          - the stored expiry is in the past relative to *now*.

        Args:
            now: Current UTC-aware datetime.
        """
        raw = self._repo.get(_DND_UNTIL_KEY)
        if raw is None:
            return None

        try:
            expiry = datetime.fromisoformat(raw)
        except ValueError:
            return None

        # Ensure the stored value is timezone-aware (defensive: legacy writes).
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)

        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        return expiry if now < expiry else None
