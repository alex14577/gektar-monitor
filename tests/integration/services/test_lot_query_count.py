"""Layer 3 integration: LotQueryService.count() on real SQLite (ddpf+hke7).

Invariants covered:
  (1) count == len(search result) for unfiltered active lots (count ≠ page size when total > 200).
  (2) count respects region filter (subject_display_names).
  (3) count respects area filter (area_sqm_min / area_sqm_max).
  (4) count with apply_subscription_cutoff=True is SQL-valid and respects the cutoff join.

docs/architecture/09-test-strategy.md Layer 3:
  Integration: real SQLite (:memory:), real LotQueryService.
  Forbidden: import sqlite3 outside integration/infra → see §Layer location rule.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from fis_monitor.domain.models import Lot, LotUpsertResult, TrackedField
from fis_monitor.services.lot_query import LotFilters, LotQueryService

_NOW = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)

_LOT_COLS = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, date_registry, "
    "lat, lon, has_boundaries, raw_json, parser_version, first_seen, last_seen, "
    "detail_fetched_at, enrichment_status, enrichment_retries, "
    "enrichment_last_error, last_seen_at, last_status, last_status_at, "
    "is_active, inactive_reason, inactive_since, inactive_confirmed_at, "
    "region_id"
)


# ---------------------------------------------------------------------------
# In-memory SQLite DB with multiple lots
# ---------------------------------------------------------------------------


def _make_conn(
    lots: list[tuple[int, str, int, int]],  # (id, region, area_sqm, is_active)
) -> sqlite3.Connection:
    """Create an in-memory SQLite connection with a 'lots' table populated from lots."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE lots ({_LOT_COLS})")
    for lot_id, region, area_sqm, is_active in lots:
        conn.execute(
            "INSERT INTO lots VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lot_id,
                f"27:01:{lot_id:08d}:1",
                area_sqm,
                region,
                None,
                None,
                None,
                None,
                "Свободен",
                _NOW.isoformat(),
                None,
                None,
                None,
                None,
                None,
                "{}",
                1,
                _NOW.isoformat(),
                _NOW.isoformat(),
                None,
                "done",
                0,
                None,
                _NOW.isoformat(),
                None,
                None,
                is_active,
                None,
                None,
                None,
                None,
            ),
        )
    conn.commit()
    return conn


class _FakeConnectionProvider:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self) -> Any:
        return self._conn

    def close_all(self) -> None:
        pass


class _FakeUserStateRepo:
    def get(self, lot_id: int) -> None:
        return None

    def get_many(self, ids: Sequence[int]) -> dict:
        return {}

    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None:
        pass

    def set_note(self, lot_id: int, note: str | None) -> None:
        pass

    def mark_visited(self, at: datetime) -> None:
        pass

    def last_visit(self) -> datetime | None:
        return None


class _FakeLotRepo:
    def upsert(self, lot: Lot, *, tracked: Sequence[TrackedField]) -> LotUpsertResult:
        return LotUpsertResult(was_new=True, changes=[])

    def get(self, lot_id: int) -> Lot | None:
        return None

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return []

    def get_last_known_id(self, region: int) -> int | None:
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        pass

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


def _make_service(lots: list[tuple[int, str, int, int]]) -> LotQueryService:
    conn = _make_conn(lots)
    return LotQueryService(
        lot_repo=_FakeLotRepo(),
        user_state_repo=_FakeUserStateRepo(),
        conn_provider=_FakeConnectionProvider(conn),
        clock=_FakeClock(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REGION_A = "Хабаровский край"
_REGION_B = "Мурманская область"

# 5 active lots in region A (area 1000–5000), 3 in region B (area 500–1500), 1 inactive A
_ALL_LOTS: list[tuple[int, str, int, int]] = [
    (1, _REGION_A, 1000, 1),
    (2, _REGION_A, 2000, 1),
    (3, _REGION_A, 3000, 1),
    (4, _REGION_A, 4000, 1),
    (5, _REGION_A, 5000, 1),
    (6, _REGION_A, 9000, 0),  # inactive — must not be counted
    (7, _REGION_B, 500, 1),
    (8, _REGION_B, 1000, 1),
    (9, _REGION_B, 1500, 1),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters,expected_count",
    [
        # (1) No filter → all active lots
        (LotFilters(), 8),
        # (2) Region A filter
        (LotFilters(subject_display_names=(_REGION_A,)), 5),
        # (2) Region B filter
        (LotFilters(subject_display_names=(_REGION_B,)), 3),
        # (3) Area filter: region A lots with area >= 3000 → lots 3,4,5
        (LotFilters(subject_display_names=(_REGION_A,), area_sqm_min=Decimal("3000")), 3),
        # (3) Area filter: all lots with area <= 1000 → lots 1, 7, 8
        (LotFilters(area_sqm_max=Decimal("1000")), 3),
    ],
    ids=[
        "no_filter_all_active",
        "region_a",
        "region_b",
        "region_a_area_min",
        "area_max_all_regions",
    ],
)
def test_lot_query_count(filters: LotFilters, expected_count: int) -> None:
    """Invariants (1), (2), (3): count returns correct total, respects filters.

    Inactive lots (is_active=0) are never counted.
    """
    svc = _make_service(_ALL_LOTS)
    result = svc.count(filters)
    assert result == expected_count, f"filters={filters!r}: expected {expected_count}, got {result}"


def test_lot_query_count_matches_full_search_result() -> None:
    """Invariant (1): count() == len(search result) when total < page_size.

    Regression guard: when total fits in one page, count and search must agree.
    """
    svc = _make_service(_ALL_LOTS)
    filters = LotFilters(subject_display_names=(_REGION_B,))
    count = svc.count(filters)
    page = svc.search(filters, page_size=200)
    assert count == len(page.items), f"count={count} != len(search)={len(page.items)}"


def test_lot_query_count_with_subscription_cutoff() -> None:
    """Invariant (4): count(apply_subscription_cutoff=True) is SQL-valid and respects cutoff.

    Lot 1: region_id=10, date_create = _NOW (2026-05-01). Subscription for
    region 10 started 2026-05-01 (same day) → passes cutoff (>=).
    Lot 2: region_id=10, date_create = day BEFORE subscription → filtered out.
    Lot 3: region_id=None → no subscription row, passes cutoff (NULL guard).

    Expected count: 2 (lots 1 and 3).
    """
    from datetime import timedelta

    before_now = (_NOW - timedelta(days=1)).isoformat()
    subscribed_at = _NOW.isoformat()
    # lots: (id, region, area_sqm, is_active, region_id)
    lots_with_rid: list[tuple[int, str, int, int, int | None]] = [
        (1, _REGION_A, 1000, 1, 10),  # passes: date_create >= subscribed_at (same day)
        (2, _REGION_A, 2000, 1, 10),  # filtered: date_create < subscribed_at
        (3, _REGION_B, 500, 1, None),  # passes: region_id IS NULL → no cutoff
    ]

    # Patch lot 2 to have date_create = yesterday by building the INSERT manually.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE lots ({_LOT_COLS})")
    conn.execute(
        "CREATE TABLE region_subscriptions "
        "(region_id INTEGER PRIMARY KEY, subscribed_at TEXT NOT NULL)"
    )
    for lot_id, region, area_sqm, is_active, region_id in lots_with_rid:
        date_create = before_now if lot_id == 2 else _NOW.isoformat()
        conn.execute(
            "INSERT INTO lots VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lot_id,
                f"27:01:{lot_id:08d}:1",
                area_sqm,
                region,
                None,
                None,
                None,
                None,
                "Свободен",
                date_create,
                None,
                None,
                None,
                None,
                None,
                "{}",
                1,
                _NOW.isoformat(),
                _NOW.isoformat(),
                None,
                "done",
                0,
                None,
                _NOW.isoformat(),
                None,
                None,
                is_active,
                None,
                None,
                None,
                region_id,
            ),
        )
    conn.execute(
        "INSERT INTO region_subscriptions (region_id, subscribed_at) VALUES (?, ?)",
        (10, subscribed_at),
    )
    conn.commit()

    svc = LotQueryService(
        lot_repo=_FakeLotRepo(),
        user_state_repo=_FakeUserStateRepo(),
        conn_provider=_FakeConnectionProvider(conn),
        clock=_FakeClock(),
    )
    result = svc.count(LotFilters(apply_subscription_cutoff=True))
    assert result == 2, (
        f"apply_subscription_cutoff=True: expected 2, got {result}. "
        "Lot 2 (date_create before subscribed_at) must be excluded; "
        "lot 3 (region_id=None) must be included."
    )
