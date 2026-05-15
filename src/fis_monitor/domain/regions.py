"""SSOT for region slug ↔ domain-ID mapping.

The web layer operates on human-readable slugs (``'dfo'``, ``'arctic'``).
The domain layer and database store integer IDs.  This module is the single
source of truth for that mapping so that no other module ever hard-codes the
correspondence.

Design constraints
------------------
- **High cohesion**: one responsibility — region mapping.
- **Low coupling**: pure functions, no imports from other project modules.
- **Immutability**: public dicts are :class:`~types.MappingProxyType` so
  accidental mutation raises ``TypeError`` immediately.
- **Fail modes**: ``slug_to_id`` is *strict* (raises ``KeyError`` on unknown
  input — fast failure at call site); ``id_to_slug`` is *lenient* (returns
  ``None`` — display-only callers tolerate unknown IDs gracefully).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

__all__ = [
    "ALL_REGION_SLUGS",
    "REGION_BY_SLUG",
    "REGION_SLUG_BY_ID",
    "REGION_TITLE_BY_SLUG",
    "REGION_TITLE_NOMINATIVE_BY_SLUG",
    "SUBJECTS_BY_MACRO",
    "SUBJECT_TITLE_BY_ID",
    "id_to_slug",
    "ids_to_slugs",
    "slug_to_id",
    "slugs_to_ids",
    "subjects_for_macros",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: All known region slugs in canonical order.
ALL_REGION_SLUGS: tuple[str, ...] = ("dfo", "arctic")

#: Slug → domain integer ID (immutable).
REGION_BY_SLUG: Mapping[str, int] = MappingProxyType(
    {
        "dfo": 1,
        "arctic": 2,
    }
)

#: Domain integer ID → slug (immutable, inverse of REGION_BY_SLUG).
REGION_SLUG_BY_ID: Mapping[int, str] = MappingProxyType(
    {v: k for k, v in REGION_BY_SLUG.items()}
)

#: Slug → display name used in the UI in **instrumental** case
#: (e.g. "Наблюдаю за <Арктикой>") — onboarding-banner phrasing.
REGION_TITLE_BY_SLUG: Mapping[str, str] = MappingProxyType(
    {
        "dfo": "ДФО",
        "arctic": "Арктикой",
    }
)

#: Slug → display name in **nominative** case — used for checkbox labels,
#: section headings, and sidebar chips where a noun stands on its own.
REGION_TITLE_NOMINATIVE_BY_SLUG: Mapping[str, str] = MappingProxyType(
    {
        "dfo": "ДФО",
        "arctic": "Арктическая зона",
    }
)

def slug_to_id(slug: str) -> int:
    """Convert a region slug to its domain integer ID.

    :raises KeyError: if *slug* is not a known region.
    """
    try:
        return REGION_BY_SLUG[slug]
    except KeyError:
        raise KeyError(f"Unknown region slug: {slug!r}") from None


def id_to_slug(id_: int) -> str | None:
    """Convert a domain integer ID to its region slug.

    Returns ``None`` for unknown IDs so display-only callers degrade
    gracefully instead of crashing.
    """
    return REGION_SLUG_BY_ID.get(id_)


# ---------------------------------------------------------------------------
# Subject (site-id) constants — SSOT per ADR-031
# ---------------------------------------------------------------------------

# site-id values per macro-region as observed in the select element of the
# FIS auction platform HTML.  87 (Якутия) and 96 (Чукотка) appear in both
# ДФО and Арктика because they straddle both administrative groupings on the
# site.
SUBJECTS_BY_MACRO: Mapping[int, tuple[int, ...]] = MappingProxyType({
    1: (72, 85, 87, 88, 89, 90, 91, 93, 94, 95, 96),   # ДФО
    2: (27, 28, 29, 30, 34, 68, 69, 76, 87, 96),         # Арктика
})

#: site-id → display name as it appears in lot-list HTML (immutable).
SUBJECT_TITLE_BY_ID: Mapping[int, str] = MappingProxyType({
    27: "Республика Карелия",
    28: "Республика Коми",
    29: "Архангельская область",
    30: "Ненецкий автономный округ",
    34: "Мурманская область",
    68: "Ханты-Мансийский автономный округ",
    69: "Ямало-Ненецкий автономный округ",
    72: "Республика Бурятия",
    76: "Красноярский край",
    85: "Забайкальский край",
    87: "Республика Саха (Якутия)",
    88: "Приморский край",
    89: "Хабаровский край",
    90: "Амурская область",
    91: "Камчатский край",
    93: "Магаданская область",
    94: "Сахалинская область",
    95: "Еврейская автономная область",
    96: "Чукотский автономный округ",
})


def subjects_for_macros(macro_ids: Sequence[int]) -> tuple[int, ...]:
    """Return a deduplicated union of subject site-ids for the given macro_ids.

    Unknown macro_ids are silently skipped (mirrors ``id_to_slug`` leniency).
    Order follows insertion order of macro_ids × their subject tuples; 87/96
    appear once even when both macro-regions are requested.
    """
    seen: dict[int, None] = {}
    for mid in macro_ids:
        for sid in SUBJECTS_BY_MACRO.get(mid, ()):
            seen[sid] = None
    return tuple(seen)


# ---------------------------------------------------------------------------
# Region helper functions
# ---------------------------------------------------------------------------


def slugs_to_ids(slugs: list[str]) -> list[int]:
    """Convert a list of region slugs to domain integer IDs.

    :raises KeyError: if any slug is not a known region.
    """
    return [slug_to_id(s) for s in slugs]


def ids_to_slugs(ids: list[int]) -> list[str]:
    """Convert a list of domain integer IDs to region slugs.

    Unknown IDs are silently skipped (lenient — mirrors ``id_to_slug``).
    """
    result: list[str] = []
    for id_ in ids:
        slug = id_to_slug(id_)
        if slug is not None:
            result.append(slug)
    return result
