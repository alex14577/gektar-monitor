"""LotQueryService — read-side use case for the web layer.

Implements cursor-based pagination and server-side filtering for the lot
catalog.  Follows CQRS-style read separation: inline SQL is used here
instead of extending ``LotRepository`` — see note below.

**Design decision (MVP-Wave-7):** filtering SQL lives in this service rather
than in a new ``LotRepository.list_filtered()`` method.  Rationale:

- ``LotQueryService`` is a *read-side* use case; its SQL never writes.
- Adding a filter method to the domain ``LotRepository`` Protocol would
  couple the interface to query-service concerns (cohesion violation).
- The inline SELECT is clearly isolated here; migration to a dedicated
  repo method is straightforward when needed (mark as "TODO: extract to
  LotRepository.list_filtered when FTS is added").

**Pagination:** cursor = opaque base64(str(lot_id)).  ``id ASC`` order gives
stable keyset pagination — inserting new lots at higher IDs never shifts
pages already consumed.

**FTS:** ``LotFilters.fts_query`` raises ``NotImplementedError``.
Full-text search is deferred to a follow-up task (P2).

**Area filter naming:** ``area_sqm_min`` / ``area_sqm_max`` map to the
``lots.area_sqm`` column — the only numeric dimension available in MVP.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from fis_monitor.domain.interfaces import (
    Clock,
    ConnectionProvider,
    LotRepository,
    UserStateRepository,
)
from fis_monitor.domain.models import (
    Lot,
    LotUserDTO,
    LotUserState,
)
from fis_monitor.infra.sqlite.repositories.lots import row_to_lot

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAGE_SIZE_MIN = 1
_PAGE_SIZE_MAX = 200

# Age thresholds for freshness/tier computation.
_AGE_HOT_SECS = 3_600  # < 1 hour  → hot
_AGE_WARM_SECS = 86_400  # < 1 day   → warm
# ≥ 1 day → cold

# Lot status whitelist (matches known values in the ``lots`` table).
_KNOWN_STATUSES: frozenset[str] = frozenset({"Свободен", "Зарезервирован"})

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LotFilters:
    """Server-side filter criteria for ``LotQueryService.search``.

    All fields are optional — omitting them means "no restriction on this
    dimension".

    ``regions``: whitelist of integer region codes.  The ``lots.region``
    column stores TEXT strings; codes are compared as strings
    (``str(region_code)``).  Pass an empty tuple to disable the filter.

    ``subject_display_names``: whitelist of RF subject display names as they
    appear in the ``lots.region`` TEXT column (e.g. "Мурманская область").
    Used by the subject-filter UI.  Pass an empty tuple to disable.

    ``area_sqm_min`` / ``area_sqm_max``: filter by ``lots.area_sqm``.
    Values are truncated to ``int`` for SQL.

    ``status``: must be one of the known status strings or ``None``.
    Pass ``None`` to show all statuses.

    ``fts_query``: raises ``NotImplementedError`` — deferred to P2.
    """

    regions: tuple[int, ...] = ()
    subject_display_names: tuple[str, ...] = ()
    area_sqm_min: Decimal | None = None
    area_sqm_max: Decimal | None = None
    status: str | None = None
    fts_query: str | None = None

    def __post_init__(self) -> None:
        if self.regions and self.subject_display_names:
            raise ValueError(
                "LotFilters.regions and .subject_display_names are mutually exclusive: "
                "both filter the same 'region' column using incompatible value types "
                "(int codes vs. display names), so ANDing them always yields zero rows. "
                "Pass only one of the two."
            )
        if self.status is not None and self.status not in _KNOWN_STATUSES:
            raise ValueError(
                f"Unknown lot status {self.status!r}. Allowed: {sorted(_KNOWN_STATUSES)}"
            )


@dataclass(frozen=True)
class Page[T]:
    """A single page of results with cursor for the next page.

    ``next_cursor`` is ``None`` when there are no more pages.
    ``has_more`` mirrors whether ``next_cursor`` is not None (convenience).
    """

    items: tuple[T, ...]
    next_cursor: str | None
    has_more: bool


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(lot_id: int) -> str:
    """Encode a lot_id into an opaque base64 cursor string."""
    return base64.urlsafe_b64encode(str(lot_id).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor back to a lot_id.

    Raises ``ValueError`` if the cursor is malformed.
    """
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise ValueError(f"Invalid page cursor: {cursor!r}") from exc


# ---------------------------------------------------------------------------
# Lot → DTO helpers
# ---------------------------------------------------------------------------

_LOT_SELECT = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, date_registry, lat, lon, "
    "has_boundaries, raw_json, parser_version, first_seen, last_seen, "
    "detail_fetched_at, enrichment_status, enrichment_retries, "
    "enrichment_last_error, last_seen_at, last_status, last_status_at, "
    "is_active, inactive_reason, inactive_since, inactive_confirmed_at, "
    "region_id"
)


def _compute_freshness(
    age_seconds: int,
) -> Literal["hot", "warm", "cool", "cold"]:
    """Map ``age_seconds`` to a freshness label."""
    if age_seconds < _AGE_HOT_SECS:
        return "hot"
    if age_seconds < _AGE_WARM_SECS:
        return "warm"
    return "cold"


def _lot_to_user_dto(
    lot: Lot,
    user_state: LotUserState | None,
    *,
    now_ts: float,
) -> LotUserDTO:
    """Merge a ``Lot`` with optional per-user state into a ``LotUserDTO``.

    ``now_ts`` is ``clock.now().timestamp()`` passed in by the caller so
    we avoid repeated clock calls inside a loop.

    Presentation hints (``age_seconds``, ``tier``, ``freshness``) are derived
    from ``lot.first_seen`` relative to ``now_ts``.
    """
    age_seconds = max(0, int(now_ts - lot.first_seen.timestamp()))
    freshness = _compute_freshness(age_seconds)

    public_kwargs: dict[str, Any] = lot.model_dump()
    public_kwargs["age_seconds"] = age_seconds
    public_kwargs["tier"] = "match"  # single-user MVP: every active lot matches
    public_kwargs["freshness"] = freshness

    user_kwargs: dict[str, Any] = {}
    if user_state is not None:
        user_kwargs["starred"] = user_state.starred
        user_kwargs["submitted"] = user_state.submitted
        user_kwargs["submitted_at"] = user_state.submitted_at
        user_kwargs["note"] = user_state.note
        user_kwargs["seen_at"] = user_state.seen_at

    return LotUserDTO(**public_kwargs, **user_kwargs)


# ---------------------------------------------------------------------------
# LotQueryService
# ---------------------------------------------------------------------------


class LotQueryService:
    """Read-side use case: filtered, paginated lot catalog for the web layer.

    All writes are done by other services (``MonitorCycleService``, etc.).
    This service only reads.

    Dependencies (all injected via constructor — DI, SOLID-D):
    - ``lot_repo``: used as a fallback for ``get()`` calls (currently unused
      in the hot path; kept for future single-lot enrichment).
    - ``user_state_repo``: per-lot user state merged into ``LotUserDTO``.
      Fetched with a single ``get_many()`` call to avoid N+1 queries.
    - ``conn_provider``: direct SQL for filtered queries (read-only).
    - ``clock``: provides ``now()`` for age / freshness computation.
    """

    def __init__(
        self,
        *,
        lot_repo: LotRepository,
        user_state_repo: UserStateRepository,
        conn_provider: ConnectionProvider,
        clock: Clock,
    ) -> None:
        self._lot_repo = lot_repo
        self._user_state_repo = user_state_repo
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_by_id(self, lot_id: int) -> LotUserDTO | None:
        """Return a single lot by ID merged with user state, or None if not found.

        Delegates to lot_repo.get() (Protocol method).  Presentation hints are
        computed the same way as in search() so the shape is consistent.
        """
        lot = self._lot_repo.get(lot_id)
        if lot is None:
            return None
        user_states = self._user_state_repo.get_many([lot_id])
        now_ts = self._clock.now().timestamp()
        return _lot_to_user_dto(lot, user_states.get(lot_id), now_ts=now_ts)

    def search(
        self,
        filters: LotFilters,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> Page[LotUserDTO]:
        """Return a filtered, paginated page of active lots.

        Args:
            filters: filter criteria (regions, area range, status, fts_query).
            page_size: max items per page (default 50; must be 1-200).
            cursor: opaque page cursor from a previous ``Page.next_cursor``.

        Returns:
            ``Page[LotUserDTO]`` with ``next_cursor`` set when more pages exist.

        Raises:
            NotImplementedError: if ``filters.fts_query`` is not None.
            ValueError: if ``cursor`` is malformed or ``page_size`` is out of range.
        """
        if not (_PAGE_SIZE_MIN <= page_size <= _PAGE_SIZE_MAX):
            raise ValueError(
                f"page_size must be between {_PAGE_SIZE_MIN} and {_PAGE_SIZE_MAX}, got {page_size}"
            )

        if filters.fts_query is not None:
            raise NotImplementedError(
                "Full-text search (fts_query) is deferred to P2. "
                "Leave fts_query=None for MVP filtering."
            )

        last_id = _decode_cursor(cursor) if cursor is not None else None
        sql, params = self._build_query(filters, last_id=last_id, limit=page_size + 1)

        conn = self._conn_provider.get()
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cur.close()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        lots = [row_to_lot(r) for r in page_rows]

        # Single get_many() call — eliminates N+1 query per lot.
        lot_ids = [lot.id for lot in lots]
        user_states: dict[int, LotUserState] = (
            self._user_state_repo.get_many(lot_ids) if lot_ids else {}
        )

        now_ts = self._clock.now().timestamp()
        items = tuple(_lot_to_user_dto(lot, user_states.get(lot.id), now_ts=now_ts) for lot in lots)

        next_cursor: str | None = None
        if has_more and lots:
            next_cursor = _encode_cursor(lots[-1].id)

        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _build_query(
        self,
        filters: LotFilters,
        *,
        last_id: int | None,
        limit: int,
    ) -> tuple[str, list[Any]]:
        """Build the parameterised SELECT statement for ``search``.

        Returns ``(sql, params)`` ready for ``conn.execute(sql, params)``.

        Filter mapping:
        - ``regions``: ``lots.region IN (?, ...)`` — region codes cast to str.
        - ``subject_display_names``: ``lots.region IN (?, ...)`` — display names
          matched directly against the TEXT ``lots.region`` column.
        - ``area_sqm_min`` / ``area_sqm_max``: mapped to ``lots.area_sqm``.
        - ``status``: exact ``lots.status = ?`` match.
        - ``cursor``: ``lots.id > ?`` keyset condition.
        """
        conditions: list[str] = ["is_active = 1"]
        params: list[Any] = []

        if filters.regions:
            placeholders = ", ".join("?" * len(filters.regions))
            conditions.append(f"region IN ({placeholders})")
            params.extend(str(r) for r in filters.regions)

        if filters.subject_display_names:
            placeholders = ", ".join("?" * len(filters.subject_display_names))
            conditions.append(f"region IN ({placeholders})")
            params.extend(filters.subject_display_names)

        if filters.area_sqm_min is not None:
            conditions.append("area_sqm >= ?")
            params.append(int(filters.area_sqm_min))

        if filters.area_sqm_max is not None:
            conditions.append("area_sqm <= ?")
            params.append(int(filters.area_sqm_max))

        if filters.status is not None:
            conditions.append("status = ?")
            params.append(filters.status)

        if last_id is not None:
            conditions.append("id > ?")
            params.append(last_id)

        where = " AND ".join(conditions)
        sql = f"SELECT {_LOT_SELECT} FROM lots WHERE {where} ORDER BY id ASC LIMIT ?"
        params.append(limit)

        return sql, params
