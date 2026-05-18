"""LotUserStateService — coordinates per-lot user state mutations.

Architecture: services layer (Layer 3-4).
Depends only on Protocol interfaces (LotRepository, UserStateRepository).

Responsibilities:
- Provide (Lot, LotUserState | None) for the details view.
- Toggle archived (set_submitted) flags.
- Persist free-text notes (max 4096 chars — validated here, not in route).

This service has NO business rules beyond the note length cap and 404 guard.
All persistence is delegated to the repositories.

See docs/architecture.md §Services layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import LotRepository, UserStateRepository
    from fis_monitor.domain.models import Lot, LotUserState

NOTE_MAX_LENGTH = 4096


class LotUserStateService:
    """Coordinate per-lot user-state reads and mutations.

    DI via constructor — accepts Protocol interfaces only, no concrete classes.

    Args:
        lot_repo: LotRepository Protocol implementation.
        user_state_repo: UserStateRepository Protocol implementation.
    """

    def __init__(
        self,
        lot_repo: LotRepository,
        user_state_repo: UserStateRepository,
    ) -> None:
        self._lot_repo = lot_repo
        self._user_state_repo = user_state_repo

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def details(self, lot_id: int) -> tuple[Lot, LotUserState | None] | None:
        """Return (Lot, LotUserState | None) for the given lot_id.

        Returns None when the lot does not exist (caller maps to 404).
        LotUserState is None when the user has never interacted with this lot.
        """
        lot = self._lot_repo.get(lot_id)
        if lot is None:
            return None
        state = self._user_state_repo.get(lot_id)
        return lot, state

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def toggle_archive(self, lot_id: int) -> None:
        """Toggle the archived flag for the given lot.

        SEMANTIC NOTE: "archived" (UX) is persisted via ``set_submitted`` /
        ``LotUserState.submitted`` — there is no separate ``archived`` column.
        This intentional overload is documented in
        ``docs/decisions/ADR-042-toggle-archive-submitted-semantic-overload.md``.
        If a distinct "submitted to deal" concept is ever needed, split the
        column per Option B in that ADR.

        Raises LotNotFoundError if the lot does not exist.
        """
        self._require_lot(lot_id)
        current = self._user_state_repo.get(lot_id)
        new_value = not current.submitted if current is not None else True
        self._user_state_repo.set_submitted(lot_id, new_value, at=None)

    def set_note(self, lot_id: int, note: str | None) -> None:
        """Persist note for the given lot.

        Args:
            lot_id: Target lot identifier.
            note: Free-text note (max 4096 chars). Pass None to clear.

        Raises:
            LotNotFoundError: If the lot does not exist.
            ValueError: If note exceeds NOTE_MAX_LENGTH characters.
        """
        if note is not None and len(note) > NOTE_MAX_LENGTH:
            raise ValueError(
                f"Note exceeds maximum length of {NOTE_MAX_LENGTH} characters "
                f"(got {len(note)})."
            )
        self._require_lot(lot_id)
        self._user_state_repo.set_note(lot_id, note)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_lot(self, lot_id: int) -> None:
        """Raise LotNotFoundError if lot_id is not in LotRepository."""
        if self._lot_repo.get(lot_id) is None:
            raise LotNotFoundError(lot_id)


class LotNotFoundError(Exception):
    """Raised when a requested lot_id is absent from LotRepository."""

    def __init__(self, lot_id: int) -> None:
        super().__init__(f"Lot {lot_id} not found.")
        self.lot_id = lot_id
