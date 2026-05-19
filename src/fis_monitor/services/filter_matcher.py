"""Notify-time lot filtering -- FilterMatcher implementations.

``FilterMatcher`` (defined in ``domain/interfaces.py``) is evaluated before
``event_bus.publish`` and ``notifier_dispatcher.dispatch`` for every lot that
would otherwise be emitted.

Design decisions:
  - ``RfSubjectFilterMatcher`` -- RF-subject (region) filter.
  - ``AllFiltersMatcher`` -- composite AND of an arbitrary sequence of matchers.
    Extension point: add future matchers (area, land-category, date-range) here.
  - Empty filter-set is always a pass-through to preserve current behaviour when
    no filter is configured (``FiltersConfig.rf_subjects == []`` -> True for all lots).

Region matching
---------------
``LotPublicDTO.region_id`` (``int | None``) is used directly.  This field is
populated from the DB column ``region_id`` set by the parser at ingest time and
is the SSOT for region identity (ADR-031, ADR-035).

If ``lot.region_id is None`` the matcher passes the lot through (fail-open) so
that new regions not yet catalogued are never silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

from fis_monitor.domain.interfaces import FilterMatcher
from fis_monitor.domain.models import FiltersConfig, LotPublicDTO


class RfSubjectFilterMatcher:
    """Filter lots by RF-subject (region) site-id.

    Pass-through when ``filters.rf_subjects`` is empty (no filter configured).
    Otherwise ``lot.region_id`` (``int | None``) is compared directly against
    the allowed list — no name→id string lookup required.

    ``lot.region_id is None`` is treated as fail-open: an unknown/unresolved
    region is never silently dropped (ADR-035 I2, I4).
    """

    def matches(self, lot: LotPublicDTO, filters: FiltersConfig) -> bool:
        """Return ``True`` if the lot's region_id is in the allowed RF-subject list.

        Algorithm:
          1. Empty ``rf_subjects`` -> True (pass-through, I4).
          2. ``lot.region_id is None`` -> True (fail-open for unresolved regions).
          3. Return ``lot.region_id in filters.rf_subjects``.
        """
        if not filters.rf_subjects:
            return True

        if lot.region_id is None:
            # Unresolved region -- fail-open so future regions are never lost.
            return True

        return lot.region_id in filters.rf_subjects


class AllFiltersMatcher:
    """Composite matcher: logical AND of a sequence of ``FilterMatcher`` implementations.

    Extension point for future per-lot filters (area_sqm range, land_category
    whitelist, date-create window, etc.).  Register them here without touching
    ``MonitorCycleService``.

    Empty matcher list -> True (no constraints = pass everything through).
    """

    def __init__(self, matchers: Sequence[FilterMatcher]) -> None:
        self._matchers = list(matchers)

    def matches(self, lot: LotPublicDTO, filters: FiltersConfig) -> bool:
        """Return ``True`` iff every matcher in the sequence returns ``True``."""
        return all(m.matches(lot, filters) for m in self._matchers)
