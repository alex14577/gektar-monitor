# ruff: noqa: RUF001
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
(official Russian Federal Subject codes, 1-89).

Adding ``region_id: int`` to ``LotPublicDTO`` would require a domain-model
change and a new DB column -- a disproportionate cost for a filtering hint.

Instead, ``RfSubjectFilterMatcher`` uses a **hardcoded mapping** from RF-subject
code to canonical region name string.  The mapping was derived from the official
OKATO/OKTMO classifier and the site's actual HTML region labels.  If an unknown
region name is encountered (not in the map) the matcher **passes the lot through**
(fail-open) so that new regions introduced by the upstream site are never silently
dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from fis_monitor.domain.interfaces import FilterMatcher
from fis_monitor.domain.models import FiltersConfig, LotPublicDTO

# ---------------------------------------------------------------------------
# RF-subject code -> region name as it appears in lot-list HTML.
# Source: official Russian Federal Subjects (OKTMO), supplemented by
# site-observed values.  Only subjects that appear on the FIS auction
# platform are included; gaps in the numbering sequence are intentional.
# ---------------------------------------------------------------------------
_RF_SUBJECT_NAMES: dict[int, str] = {
    1: "Республика Адыгея",
    2: "Республика Башкортостан",
    3: "Республика Бурятия",
    4: "Республика Алтай",
    5: "Республика Дагестан",
    6: "Республика Ингушетия",
    7: "Кабардино-Балкарская Республика",
    8: "Республика Калмыкия",
    9: "Карачаево-Черкесская Республика",
    10: "Республика Карелия",
    11: "Республика Коми",
    12: "Республика Марий Эл",
    13: "Республика Мордовия",
    14: "Республика Саха (Якутия)",
    15: "Республика Северная Осетия — Алания",
    16: "Республика Татарстан",
    17: "Республика Тыва",
    18: "Удмуртская Республика",
    19: "Республика Хакасия",
    20: "Чеченская Республика",
    21: "Чувашская Республика",
    22: "Алтайский край",
    23: "Краснодарский край",
    24: "Красноярский край",
    25: "Приморский край",
    26: "Ставропольский край",
    27: "Хабаровский край",
    28: "Амурская область",
    29: "Архангельская область",
    30: "Астраханская область",
    31: "Белгородская область",
    32: "Брянская область",
    33: "Владимирская область",
    34: "Волгоградская область",
    35: "Вологодская область",
    36: "Воронежская область",
    37: "Ивановская область",
    38: "Иркутская область",
    39: "Калининградская область",
    40: "Калужская область",
    41: "Камчатский край",
    42: "Кемеровская область",
    43: "Кировская область",
    44: "Костромская область",
    45: "Курганская область",
    46: "Курская область",
    47: "Ленинградская область",
    48: "Липецкая область",
    49: "Магаданская область",
    50: "Московская область",
    51: "Мурманская область",
    52: "Нижегородская область",
    53: "Новгородская область",
    54: "Новосибирская область",
    55: "Омская область",
    56: "Оренбургская область",
    57: "Орловская область",
    58: "Пензенская область",
    59: "Пермский край",
    60: "Псковская область",
    61: "Ростовская область",
    62: "Рязанская область",
    63: "Самарская область",
    64: "Саратовская область",
    65: "Сахалинская область",
    66: "Свердловская область",
    67: "Смоленская область",
    68: "Тамбовская область",
    69: "Тверская область",
    70: "Томская область",
    71: "Тульская область",
    72: "Тюменская область",
    73: "Ульяновская область",
    74: "Челябинская область",
    75: "Забайкальский край",
    76: "Ярославская область",
    77: "Москва",
    78: "Санкт-Петербург",
    79: "Еврейская автономная область",
    83: "Ненецкий автономный округ",
    86: "Ханты-Мансийский автономный округ — Югра",
    87: "Чукотский автономный округ",
    89: "Ямало-Ненецкий автономный округ",
}


class RfSubjectFilterMatcher:
    """Filter lots by RF-subject (region) code.

    Pass-through when ``filters.rf_subjects`` is empty (no filter configured).
    Otherwise the lot's region name is resolved to an RF-subject code via
    ``_RF_SUBJECT_NAMES`` and compared against the allowed list.

    Unknown region names (not in the hardcoded map) are passed through
    (fail-open) to avoid silently dropping lots from new regions added by the
    upstream site without a corresponding code update here.
    """

    # Invert the module-level mapping once at class load time so lookups are O(1).
    _NAME_TO_ID: ClassVar[dict[str, int]] = {
        name: id_ for id_, name in _RF_SUBJECT_NAMES.items()
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
