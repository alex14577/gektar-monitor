"""Feed context builder — shared between GET / and POST /filters/view.

Extracts ``build_feed_context`` and its private adapter ``_view_filters_to_lot_filters``
out of the main route module so that ``filters.py`` can import from a leaf module
instead of from another route module (route → route is an anti-pattern).

Both route modules import ``build_feed_context`` from here; neither imports
from the other.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fis_monitor.domain.models import Settings
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID
from fis_monitor.services.lot_query import LotFilters, LotQueryService
from fis_monitor.services.view_filters import ViewFilters
from fis_monitor.web.sse_encoder import LotUserViewModel

__all__ = ["build_feed_context"]

# Single-page feed cap: at most this many active lots are loaded into the
# initial HTML.  Lots beyond this fall into the archive count only.
_FEED_PAGE_SIZE = 200


def _view_filters_to_lot_filters(vf: ViewFilters) -> LotFilters:
    """Adapt the sidebar ``ViewFilters`` to the storage-level ``LotFilters``.

    ``ViewFilters.subjects`` are RF subject site-ids serialised as strings (the
    sidebar form sends them as ``<input value="34">``).  Each site-id is looked
    up in ``SUBJECT_TITLE_BY_ID`` to obtain the display name that matches the
    TEXT value stored in ``lots.region`` (e.g. 34 → "Мурманская область").
    These names are passed as ``LotFilters.subject_display_names`` so the SQL layer
    generates ``WHERE region IN ('Мурманская область', ...)``.

    Non-numeric or unknown site-ids are silently dropped — defensive against a
    stale cookie surviving a catalog bump.

    ``only_new`` is a user-state predicate not available at the SQL layer
    and is applied as an in-memory post-filter in ``_assemble_feed_zones``.
    """
    subject_display_names: list[str] = []
    for s in vf.subjects:
        try:
            site_id = int(s)
        except (TypeError, ValueError):
            continue
        name = SUBJECT_TITLE_BY_ID.get(site_id)
        if name is not None:
            subject_display_names.append(name)
    return LotFilters(
        subject_display_names=tuple(subject_display_names),
        area_sqm_min=Decimal(vf.area_min) if vf.area_min is not None else None,
        area_sqm_max=Decimal(vf.area_max) if vf.area_max is not None else None,
        sort_dir=vf.sort_dir,
    )


def _filters_are_active(filters: ViewFilters) -> bool:
    """Any non-default selection means the user has narrowed the feed."""
    return bool(
        filters.subjects
        or filters.area_min is not None
        or filters.area_max is not None
        or filters.only_new
    )


def _assemble_feed_zones(
    items: tuple,
    *,
    view_filters: ViewFilters,
) -> tuple[SimpleNamespace, int]:
    """Group ``LotUserDTO`` items into the template's feed zones.

    All lots that pass the user-state post-filters are placed into
    ``zones.today`` regardless of age; ``zones.hot`` is always an empty
    tuple (kept for backward-compat with any callers that read the field).
    ``archive_count`` is always 0 because ``lot_query.search`` already caps
    results at ``_FEED_PAGE_SIZE`` — there is nothing left over.

    Applies the user-state post-filter (``only_new``) that the SQL layer
    cannot express.  Each surfaced lot is wrapped in ``LotUserViewModel``
    so it can be consumed by the existing partials.
    """
    today: list[LotUserViewModel] = []

    for dto in items:
        if view_filters.only_new and dto.seen_at is not None:
            continue

        today.append(LotUserViewModel(dto))

    return SimpleNamespace(hot=(), today=tuple(today)), 0


def _build_filters_context(filters: ViewFilters) -> SimpleNamespace:
    """Map ``ViewFilters`` onto the field names the sidebar template expects."""
    return SimpleNamespace(
        subjects=filters.subjects,
        area_min=filters.area_min if filters.area_min is not None else "",
        area_max=filters.area_max if filters.area_max is not None else "",
        area_min_label=str(filters.area_min) if filters.area_min is not None else "0",
        area_max_label=str(filters.area_max) if filters.area_max is not None else "∞",
        only_new=filters.only_new,
        sort_dir=filters.sort_dir,
    )


def _build_scope_context(settings: Settings) -> SimpleNamespace:
    """Derive sidebar scope chips from configured regions."""
    return SimpleNamespace(
        macro_regions=list(settings.regions),
        subjects_count=len(SUBJECT_TITLE_BY_ID),
    )


def build_feed_context(
    *,
    filters: ViewFilters,
    lot_query: LotQueryService,
    settings: Settings,
    active_lot_count: int,
) -> dict[str, object]:
    """Build the template context dict for the feed zones partial.

    Shared by GET / (full page render) and POST /filters/view (partial swap).
    Callers provide the already-resolved dependencies so this function stays
    a pure computation with no FastAPI Depends coupling.

    Returns a dict ready to merge into a TemplateResponse context; keys:
    ``zones``, ``archive_count``, ``filters_active``, ``health``,
    ``filters`` (sidebar filter state), ``scope`` (subjects count).
    The ``health`` entry uses ``active_lot_count`` so the caller need not
    compute it again.
    """
    lot_filters = _view_filters_to_lot_filters(filters)
    page = lot_query.search(lot_filters, page_size=_FEED_PAGE_SIZE)
    zones, archive_count = _assemble_feed_zones(
        page.items,
        view_filters=filters,
    )
    return {
        "zones": zones,
        "archive_count": archive_count,
        "filters_active": _filters_are_active(filters),
        "health": SimpleNamespace(
            last_cycle_human="—",
            total_lots=active_lot_count,
            last_new_human="—",
        ),
        "filters": _build_filters_context(filters),
        "scope": _build_scope_context(settings),
    }
