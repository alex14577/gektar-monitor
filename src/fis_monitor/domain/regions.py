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

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "ALL_REGION_SLUGS",
    "REGION_BY_SLUG",
    "REGION_SLUG_BY_ID",
    "REGION_TITLE_BY_SLUG",
    "id_to_slug",
    "ids_to_slugs",
    "slug_to_id",
    "slugs_to_ids",
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

#: Slug → display name used in the UI (immutable).
REGION_TITLE_BY_SLUG: Mapping[str, str] = MappingProxyType(
    {
        "dfo": "ДФО",
        "arctic": "Арктикой",
    }
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


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
