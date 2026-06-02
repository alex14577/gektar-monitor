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

**Pagination:** cursor = opaque base64(``<date_create_iso>:<lot_id>``).
The sort key is ``(date_create, id)``; the composite keyset cursor carries
both values so pages remain correct even when many lots share the same
``date_create``.  Order is always DESC (newest first):

- ``WHERE (date_create < ?) OR (date_create = ? AND id < ?)``

``date_create`` is stored as ISO-8601 text (via ``_iso()`` in the
repository), which makes lexicographic and chronological order equivalent.
The column is ``NOT NULL`` in the schema, so no sentinel handling is needed.

**FTS:** ``LotFilters.fts_query`` raises ``NotImplementedError``.
Full-text search is deferred to a follow-up task (P2).

**Area filter naming:** ``area_sqm_min`` / ``area_sqm_max`` map to the
``lots.area_sqm`` column — the only numeric dimension available in MVP.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
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
_AGE_HOT_SECS = 3_600  # < 1 hour  -> hot
_AGE_WARM_SECS = 86_400  # < 1 day   -> warm
# >= 1 day -> cold

# Lot status whitelist (matches known values in the ``lots`` table).
_KNOWN_STATUSES: frozenset[str] = frozenset({"Свободен", "Зарезервирован"})

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LotFilters:
    """Server-side filter criteria for ``LotQueryService.search``.

    All fields are optional -- omitting them means "no restriction on this
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

    ``fts_query``: raises ``NotImplementedError`` -- deferred to P2.
    """

    regions: tuple[int, ...] = ()
    subject_display_names: tuple[str, ...] = ()
    area_sqm_min: Decimal | None = None
    area_sqm_max: Decimal | None = None
    status: str | None = None
    fts_query: str | None = None
    apply_subscription_cutoff: bool = False
    filter_subscribed_subjects: bool = False

    def __post_init__(self) -> None:
        if self.regions and self.subject_display_names:
            raise ValueError(
                "LotFilters.regions and .subject_display_names are mutually exclusive: "
                "both filter the same 'region' column using incompatible value types "
                "(int codes vs. display names), so ANDing them always yields zero rows. "
                "Pass only one of the two."
            )
        if self.apply_subscription_cutoff and self.filter_subscribed_subjects:
            raise ValueError(
                "apply_subscription_cutoff and filter_subscribed_subjects are mutually exclusive"
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


def _encode_cursor(date_create: datetime, lot_id: int) -> str:
    """Encode a ``(date_create, lot_id)`` pair into an opaque base64 cursor string.

    The payload format is ``<date_create_iso>:<lot_id>`` -- both components are
    needed for the composite keyset cursor (sort key is ``(date_create, id)``).

    Invariant: ``date_create.isoformat()`` must be byte-identical to the TEXT
    stored in the ``lots.date_create`` column; the tie-branch comparison
    ``date_create = ?`` silently skips boundary rows otherwise.  This holds
    because ``list_parser._parse_date`` always produces UTC-aware midnight values
    (microsecond=0).  See that function's docstring before adding new write paths.
    """
    payload = f"{date_create.isoformat()}:{lot_id}"
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    """Decode an opaque cursor back to ``(date_create_iso, lot_id)``.

    Returns a ``(date_create_iso, lot_id)`` pair where ``date_create_iso`` is the
    ISO-8601 string as stored in the DB (lexicographically sortable).

    Raises ``ValueError`` if the cursor is malformed or the payload format is invalid.
    """
    try:
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        # Split on the LAST colon so the ISO datetime part (which may contain '+')
        # is preserved intact.
        sep = payload.rfind(":")
        if sep == -1:
            raise ValueError("missing separator")
        date_iso = payload[:sep]
        lot_id = int(payload[sep + 1 :])
        return date_iso, lot_id
    except Exception as exc:
        raise ValueError(f"Invalid page cursor: {cursor!r}") from exc


# ---------------------------------------------------------------------------
# Lot -> DTO helpers
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

# Table-qualified version of _LOT_SELECT used when a JOIN is active.
# Required because region_id also exists in region_subscriptions; without
# qualification SQLite raises "ambiguous column name: region_id".
# Column order is preserved byte-for-byte so row_to_lot() positional mapping
# is unaffected. Unqualified _LOT_SELECT is kept for the JOIN-free path so
# the /lots API path is unchanged.
_LOT_SELECT_QUALIFIED = ", ".join(f"lots.{col.strip()}" for col in _LOT_SELECT.split(","))


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

    Dependencies (all injected via constructor -- DI, SOLID-D):
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

        last_cursor = _decode_cursor(cursor) if cursor is not None else None
        sql, params = self._build_query(filters, last_cursor=last_cursor, limit=page_size + 1)

        conn = self._conn_provider.get()
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cur.close()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        lots = [row_to_lot(r) for r in page_rows]

        # Single get_many() call -- eliminates N+1 query per lot.
        lot_ids = [lot.id for lot in lots]
        user_states: dict[int, LotUserState] = (
            self._user_state_repo.get_many(lot_ids) if lot_ids else {}
        )

        now_ts = self._clock.now().timestamp()
        items = tuple(_lot_to_user_dto(lot, user_states.get(lot.id), now_ts=now_ts) for lot in lots)

        next_cursor: str | None = None
        if has_more and lots:
            last_lot = lots[-1]
            next_cursor = _encode_cursor(last_lot.date_create, last_lot.id)

        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self, filters: LotFilters) -> int:
        """Return the total number of active lots matching ``filters``.

        Unlike ``search()``, this method applies no cursor, ORDER BY, or LIMIT —
        it issues a single ``SELECT COUNT(*)`` so the result reflects the true
        total even when ``page_size=200`` would cap a ``search()`` call.

        ``only_new`` is a user-state predicate (in-memory post-filter) that
        cannot be expressed at the SQL layer; it is therefore intentionally
        NOT applied here.  The counter represents the region+area+subscription
        scope, matching the same filter dimensions used by ``search()``.

        Args:
            filters: filter criteria (regions, area range, subscription cutoff).
                     ``fts_query`` raises ``NotImplementedError`` if set.

        Returns:
            Non-negative integer count of matching active lots.
        """
        if filters.fts_query is not None:
            raise NotImplementedError(
                "Full-text search (fts_query) is deferred to P2. "
                "Leave fts_query=None for count()."
            )

        where, params, from_clause = self._build_where(filters)
        sql = f"SELECT COUNT(*) FROM {from_clause} WHERE {where}"
        conn = self._conn_provider.get()
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return int(row[0])

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _build_where(
        self,
        filters: LotFilters,
    ) -> tuple[str, list[Any], str]:
        """Build the WHERE clause and FROM clause shared by ``search`` and ``count``.

        Returns ``(where_str, params, from_clause)`` where:
        - ``where_str``: space-separated AND conditions (no cursor / ORDER / LIMIT).
        - ``params``: bound parameters for the WHERE conditions only.
        - ``from_clause``: ``"lots"`` or ``"lots LEFT JOIN region_subscriptions ..."``
          depending on ``apply_subscription_cutoff``.

        This method is the DRY core used by both ``_build_query`` (adds cursor +
        ORDER + LIMIT) and ``count`` (adds SELECT COUNT(*) only).
        """
        join_clause, cutoff_condition = self._subscription_cutoff_fragment(filters)

        conditions: list[str] = ["lots.is_active = 1"] if join_clause else ["is_active = 1"]
        params: list[Any] = []

        # Table-qualify column references when a JOIN is present to avoid ambiguity.
        col = "lots." if join_clause else ""

        if filters.regions:
            placeholders = ", ".join("?" * len(filters.regions))
            conditions.append(f"{col}region IN ({placeholders})")
            params.extend(str(r) for r in filters.regions)

        if filters.subject_display_names:
            placeholders = ", ".join("?" * len(filters.subject_display_names))
            conditions.append(f"{col}region IN ({placeholders})")
            params.extend(filters.subject_display_names)

        if filters.area_sqm_min is not None:
            conditions.append(f"{col}area_sqm >= ?")
            params.append(int(filters.area_sqm_min))

        if filters.area_sqm_max is not None:
            conditions.append(f"{col}area_sqm <= ?")
            params.append(int(filters.area_sqm_max))

        if filters.status is not None:
            conditions.append(f"{col}status = ?")
            params.append(filters.status)

        if cutoff_condition:
            conditions.append(cutoff_condition)

        if filters.filter_subscribed_subjects:
            conditions.append(
                f"({col}region_id IS NULL OR {col}region_id IN "
                "(SELECT region_id FROM region_subscriptions))"
            )

        where = " AND ".join(conditions)
        from_clause = f"lots{join_clause}"
        return where, params, from_clause

    def _build_query(
        self,
        filters: LotFilters,
        *,
        last_cursor: tuple[str, int] | None,
        limit: int,
    ) -> tuple[str, list[Any]]:
        """Build the parameterised SELECT statement for ``search``.

        Returns ``(sql, params)`` ready for ``conn.execute(sql, params)``.

        Filter mapping delegated to ``_build_where``; this method adds cursor,
        ORDER BY, and LIMIT on top.

        - ``cursor``: composite keyset on ``(date_create, id)``:
            DESC: ``(date_create < ?) OR (date_create = ? AND id < ?)``
        - ``apply_subscription_cutoff``: when True, LEFT JOIN region_subscriptions
            and add WHERE predicate mirroring ``passes_subscription_cutoff``
            (ADR-039 day-precision rule).  When False, query is unchanged.
        - ``filter_subscribed_subjects``: when True, restricts results to lots
            whose ``region_id`` is present in ``region_subscriptions``, or whose
            ``region_id`` IS NULL.

        ``_build_where`` already calls ``_subscription_cutoff_fragment`` internally
        and returns the complete ``from_clause`` (including any JOIN).  We derive
        the column-qualifier prefix and SELECT column list from ``from_clause``
        without a second call to ``_subscription_cutoff_fragment``.
        """
        where, params, from_clause = self._build_where(filters)
        # Determine column qualifier from from_clause instead of calling
        # _subscription_cutoff_fragment again (DRY — single call per query path).
        # NOTE: this heuristic holds only while _build_where is the SOLE source
        # of JOINs in from_clause. If a future JOIN is added, derive has_join
        # explicitly rather than string-matching here.
        has_join = "LEFT JOIN" in from_clause
        col = "lots." if has_join else ""
        select_cols = _LOT_SELECT_QUALIFIED if has_join else _LOT_SELECT

        # Extend WHERE with cursor condition (not part of _build_where — cursor
        # is search-only, count() must not include it).
        if last_cursor is not None:
            last_date_iso, last_id = last_cursor
            where = (
                where
                + f" AND ({col}date_create < ? OR ({col}date_create = ? AND {col}id < ?))"
            )
            params = [*params, last_date_iso, last_date_iso, last_id]

        sql = (
            f"SELECT {select_cols} FROM {from_clause} WHERE {where} "
            f"ORDER BY {col}date_create DESC, {col}id DESC LIMIT ?"
        )
        params = [*params, limit]

        return sql, params

    @staticmethod
    def _subscription_cutoff_fragment(filters: LotFilters) -> tuple[str, str]:
        """Return ``(join_clause, where_condition)`` for the subscription cutoff.

        When ``filters.apply_subscription_cutoff`` is False both strings are
        empty — the query is structurally identical to the pre-cutoff version.

        When True:
        - ``join_clause``: ``" LEFT JOIN region_subscriptions rs ON lots.region_id = rs.region_id"``
        - ``where_condition``: SQL mirror of ``passes_subscription_cutoff`` predicate
          (ADR-039 day-precision rule):
          ``(lots.region_id IS NULL OR rs.subscribed_at IS NULL
          OR date(lots.date_create) >= date(rs.subscribed_at))``

        SQLite ``date()`` semantics: ``date('2026-05-15T00:00:00+00:00')`` →
        ``'2026-05-15'`` — SQLite truncates at the ``T`` boundary, so ISO-8601
        UTC isoformat strings stored in both columns are handled correctly.
        Same-day comparison is non-strict (``>=``), matching the Python predicate.
        """
        if not filters.apply_subscription_cutoff:
            return "", ""
        join_clause = " LEFT JOIN region_subscriptions rs ON lots.region_id = rs.region_id"
        where_condition = (
            "(lots.region_id IS NULL OR rs.subscribed_at IS NULL"
            " OR date(lots.date_create) >= date(rs.subscribed_at))"
        )
        return join_clause, where_condition
