"""SSE view-filter predicate factory.

Responsibility (SRP): build a per-connection callable that decides whether
an ``SseEvent`` should be forwarded to a specific subscriber, based on the
subscriber's ``ViewFilters`` cookie state captured at connection time.

Design:
  - ``make_sse_view_filter(vf: ViewFilters) -> Callable[[SseEvent], bool]``
    is a pure factory: no I/O, no side-effects, easy to test.
  - The returned predicate is stateless (closure over frozen filter state).
  - ``SseStreamer`` accepts the predicate as ``event_filter`` — it does NOT
    import ``ViewFilters`` itself, keeping coupling low (open/closed for new
    filter fields without touching ``SseStreamer``).
  - **Pass-through for non-SseLotNew events**: only ``lot.new`` events carry
    the lot payload that view-filters act upon. Other event types (cycle.done,
    status, session.expired, …) always pass through so the UI stays live.
  - **Fast path**: if all filter fields are at default (no subjects, no area
    bounds, only_new=False) the factory returns an always-True sentinel —
    avoids per-event isinstance checks on idle tabs.

Filter semantics per ADR-052:
  - ``subjects`` — list of site-id strings from the cookie; converted to an
    ``int`` set once at factory time.  Match: ``event.lot.region_id in ids``.
    ``region_id=None`` on the lot → **suppress** (conservative: we don't know
    the region so we can't confirm it matches).
  - ``area_min`` / ``area_max`` — ``int | None`` bounds.  When a bound is set,
    ``event.lot.area_sqm`` must satisfy it.  ``area_sqm=None`` on the lot →
    **pass-through** (enrichment pending; fail-open so the lot isn't silently
    dropped before area is fetched).
  - ``only_new=True`` — always pass for ``lot.new`` (by definition, every
    ``lot.new`` event *is* a new lot).  No-op filter for this field.

See ADR-052 for rationale, alternatives, and deferred scope (live cookie sync).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

from fis_monitor.domain.models import SseEvent, SseLotNew
from fis_monitor.services.view_filters import ViewFilters

__all__ = ["make_sse_view_filter"]

_log = logging.getLogger(__name__)

# Sentinel: returned when all filters are default → every event passes.
_ALWAYS_TRUE: Callable[[SseEvent], bool] = lambda _: True  # noqa: E731


def _is_default(vf: ViewFilters) -> bool:
    """Return True if *vf* has no active filter constraints.

    A ViewFilters is "default" when it would never suppress any lot.new event:
      - no subjects restriction
      - no area bounds
      - only_new has no effect on lot.new (always pass-through), so ignored.
    """
    return (
        not vf.subjects
        and vf.area_min is None
        and vf.area_max is None
    )


def make_sse_view_filter(vf: ViewFilters) -> Callable[[SseEvent], bool]:
    """Return a predicate ``event -> bool`` that implements *vf* for SSE events.

    Returns ``True`` (pass) when the event should be forwarded to the client,
    ``False`` (suppress) when it should be silently dropped.

    Args:
        vf: ``ViewFilters`` captured at SSE connection time.  The predicate
            closure holds a snapshot; live cookie changes are NOT reflected
            (deferred per ADR-052 §Deferred scope).

    Returns:
        A callable ``(SseEvent) -> bool``.  Always-True when all filter
        fields are at default values (fast path).
    """
    if _is_default(vf):
        return _ALWAYS_TRUE

    # Pre-compute subjects as an int set once (not per event).
    subject_ids: frozenset[int] = frozenset()
    if vf.subjects:
        ids: set[int] = set()
        for s in vf.subjects:
            with contextlib.suppress(TypeError, ValueError):
                ids.add(int(s))
        subject_ids = frozenset(ids)

    # Snapshot scalar bounds (immutable ints or None).
    area_min: int | None = vf.area_min
    area_max: int | None = vf.area_max
    # only_new=True → always pass for lot.new → captured but never used to suppress.

    def _predicate(event: SseEvent) -> bool:
        # Pass-through for all non-SseLotNew event types.
        if not isinstance(event, SseLotNew):
            return True

        lot = event.lot

        # subjects filter: if set, lot must have a matching region_id.
        if subject_ids and (lot.region_id is None or lot.region_id not in subject_ids):
            _log.debug(
                "sse.event.filtered",
                extra={
                    "reason": "subject_mismatch",
                    "lot_id": lot.id,
                    "lot_region_id": lot.region_id,
                },
            )
            return False

        # area_min filter: lot.area_sqm must be >= area_min.
        # area_sqm=None → pass-through (enrichment pending, fail-open).
        if area_min is not None and lot.area_sqm is not None and lot.area_sqm < area_min:
            _log.debug(
                "sse.event.filtered",
                extra={
                    "reason": "area_min",
                    "lot_id": lot.id,
                    "area_sqm": lot.area_sqm,
                    "area_min": area_min,
                },
            )
            return False

        # area_max filter: lot.area_sqm must be <= area_max.
        # area_sqm=None → pass-through (enrichment pending, fail-open).
        if area_max is not None and lot.area_sqm is not None and lot.area_sqm > area_max:
            _log.debug(
                "sse.event.filtered",
                extra={
                    "reason": "area_max",
                    "lot_id": lot.id,
                    "area_sqm": lot.area_sqm,
                    "area_max": area_max,
                },
            )
            return False

        return True

    return _predicate
