"""Pure diff function for lot field changes.

Computes the delta between two Lot snapshots for a caller-specified set of
tracked fields. Results are written to `lots_history` by the repository
(see docs/decisions/ADR-016-repository-invariants-begin-immediate.md /
docs/architecture/03-protocols.md §3.1).

Design contract:
- No I/O, no logging, no mutation.
- `old=None` means INSERT — `lots_history` is intentionally not written for
  new lots (see `LotUpsertResult._check_new_implies_no_changes` invariant).
- `auction` / `list_presence` exist in `TrackedField` for forward-compat but
  are not yet attributes of `Lot`. They fail fast with `NotImplementedError`
  rather than leaking `AttributeError` out of a `BEGIN IMMEDIATE` tx.
"""

from __future__ import annotations

import typing
from collections.abc import Sequence

from fis_monitor.domain.models import FieldChange, Lot, TrackedField

# SSOT: derived from the `TrackedField` Literal so the whitelist cannot
# drift from the type. Defence-in-depth against SQL-identifier injection
# via callers that bypass static type checking.
ALLOWED_TRACKED_FIELDS: frozenset[str] = frozenset(typing.get_args(TrackedField))

# Fields declared for forward-compatibility but not yet present on `Lot`.
_UNIMPLEMENTED_FIELDS: frozenset[str] = frozenset({"auction", "list_presence"})


def compute_changes(
    old: Lot | None,
    new: Lot,
    tracked: Sequence[TrackedField],
) -> list[FieldChange]:
    """Return one FieldChange per tracked field where old != new.

    Args:
        old:     Previous lot snapshot, or None for a brand-new INSERT.
        new:     Current lot snapshot (always provided).
        tracked: Ordered sequence of field names to compare. Result order
                 matches this sequence.

    Returns:
        Empty list when `old` is None (INSERT — no history written per ADR-016)
        or when no tracked field changed. Otherwise a list of `FieldChange`
        instances in `tracked` order.

    Raises:
        ValueError: If any element of `tracked` is not in ALLOWED_TRACKED_FIELDS.
        NotImplementedError: If `tracked` contains a forward-compat field
            that is not yet an attribute of `Lot` (`auction`, `list_presence`).
    """
    for field in tracked:
        if field not in ALLOWED_TRACKED_FIELDS:
            raise ValueError(
                f"Unknown tracked field {field!r}. "
                f"Allowed: {sorted(ALLOWED_TRACKED_FIELDS)}"
            )
        if field in _UNIMPLEMENTED_FIELDS:
            raise NotImplementedError(
                f"Tracked field {field!r} is reserved for forward-compat "
                f"and not yet an attribute of Lot."
            )

    # INSERT path — lots_history is not written for new lots (ADR-016).
    if old is None:
        return []

    changes: list[FieldChange] = []
    for field in tracked:
        old_value = getattr(old, field)
        new_value = getattr(new, field)
        if old_value != new_value:
            changes.append(FieldChange(field=field, old_value=old_value, new_value=new_value))

    return changes
