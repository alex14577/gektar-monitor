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

Region-id / region-name mismatch
---------------------------------
``LotPublicDTO.region`` is a ``str`` (the region name as it appears in the
lot-list HTML), while ``FiltersConfig.rf_subjects`` is a ``list[int]``
(site-id namespace per ADR-031).

Adding ``region_id: int`` to ``LotPublicDTO`` would require a domain-model
change and a new DB column -- a disproportionate cost for a filtering hint.

Instead, ``RfSubjectFilterMatcher`` builds ``_NAME_TO_ID`` from
``SUBJECT_TITLE_BY_ID`` sourced from domain/regions.py SSOT (site-id namespace
per ADR-031).  If an unknown region name is encountered (not in the map) the
matcher **passes the lot through** (fail-open) so that new regions introduced
by the upstream site are never silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from fis_monitor.domain.interfaces import FilterMatcher
from fis_monitor.domain.models import FiltersConfig, LotPublicDTO
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID


class RfSubjectFilterMatcher:
    """Filter lots by RF-subject (region) code.

    Pass-through when ``filters.rf_subjects`` is empty (no filter configured).
    Otherwise the lot's region name is resolved to a site-id via
    ``SUBJECT_TITLE_BY_ID`` (domain/regions.py SSOT) and compared against the
    allowed list.

    Unknown region names (not in the SSOT map) are passed through (fail-open)
    to avoid silently dropping lots from new regions added by the upstream site
    without a corresponding SSOT update.
    """

    # Invert the SSOT mapping once at class load time so lookups are O(1).
    _NAME_TO_ID: ClassVar[dict[str, int]] = {
        name: id_ for id_, name in SUBJECT_TITLE_BY_ID.items()
    }

    def matches(self, lot: LotPublicDTO, filters: FiltersConfig) -> bool:
        """Return ``True`` if the lot's region is in the allowed RF-subject list.

        Algorithm:
          1. Empty ``rf_subjects`` -> True (pass-through, preserves pre-filter behaviour).
          2. Look up ``lot.region`` (str) in ``_NAME_TO_ID`` to get the integer code.
          3. If the name is not in the map -> True (fail-open for unknown regions).
          4. Return ``lot_region_id in filters.rf_subjects``.
        """
        if not filters.rf_subjects:
            return True

        lot_region_id = self._NAME_TO_ID.get(lot.region)
        if lot_region_id is None:
            # Unknown region name -- fail-open so future regions are never lost.
            return True

        return lot_region_id in filters.rf_subjects


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
