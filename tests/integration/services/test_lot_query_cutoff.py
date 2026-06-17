"""Layer 3 integration test — LotQueryService with apply_subscription_cutoff=True.

Tests:
- SQL executes without ambiguous-column error (regression: region_id in both
  lots and region_subscriptions when JOIN is active).
- Equivalence: returned lot-id set equals the Python-predicate filter applied
  to the same rows, proving SQL mirrors passes_subscription_cutoff().
- /lots path unaffected: apply_subscription_cutoff=False returns ALL active lots.
- gn89 regression: same-day lot (date_create midnight, subscribed_at 10:30)
  passes in both SQL and Python predicate.

Schema follows migrations_v3_to_v4: lots(region_id INTEGER, ...) +
region_subscriptions(region_id INTEGER PRIMARY KEY, subscribed_at TEXT NOT NULL).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.models import (
    Lot,
    LotUpsertResult,
    LotUserState,
    TrackedField,
)
from fis_monitor.domain.subscription_cutoff import passes_subscription_cutoff
from fis_monitor.services.lot_query import LotFilters, LotQueryService

# ---------------------------------------------------------------------------
# Column list — matches _LOT_SELECT in lot_query.py exactly
# ---------------------------------------------------------------------------

_LOT_COLS = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, date_registry, "
    "lat, lon, has_boundaries, raw_json, parser_version, first_seen, last_seen, "
    "detail_fetched_at, enrichment_status, enrichment_retries, "
    "enrichment_last_error, last_seen_at, last_status, last_status_at, "
    "is_active, inactive_reason, inactive_since, inactive_confirmed_at, "
    "region_id"
)

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures — real in-memory SQLite DB with both tables
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with lots + region_subscriptions tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE lots ({_LOT_COLS})")
    conn.execute(
        "CREATE TABLE region_subscriptions ("
        "  region_id     INTEGER PRIMARY KEY,"
        "  subscribed_at TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def _insert_lot(
    conn: sqlite3.Connection,
    *,
    lot_id: int,
    date_create: datetime,
    region_id: int | None,
    is_active: int = 1,
) -> None:
    """Insert a minimal lot row."""
    ts = _NOW.isoformat()
    conn.execute(
        "INSERT INTO lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            lot_id,
            f"27:01:{lot_id:08d}:0001",  # cadastral_no
            5000,                          # area_sqm
            "Тест",                        # region
            "Тест МО",                     # municipality
            None,                          # land_category
            None,                          # permitted_use
            None,                          # ogv
            "Свободен",                    # status
            date_create.isoformat(),       # date_create
            None,                          # date_update
            None,                          # date_registry
            None, None,                    # lat, lon
            None,                          # has_boundaries
            "{}",                          # raw_json
            1,                             # parser_version
            ts,                            # first_seen
            ts,                            # last_seen
            None,                          # detail_fetched_at
            "done",                        # enrichment_status
            0,                             # enrichment_retries
            None,                          # enrichment_last_error
            ts,                            # last_seen_at
            None,                          # last_status
            None,                          # last_status_at
            is_active,
            None, None, None,              # inactive_reason, inactive_since, inactive_confirmed_at
            region_id,
        ),
    )


def _insert_subscription(
    conn: sqlite3.Connection,
    *,
    region_id: int,
    subscribed_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO region_subscriptions (region_id, subscribed_at) VALUES (?, ?)",
        (region_id, subscribed_at.isoformat()),
    )


# ---------------------------------------------------------------------------
# Fake infrastructure — minimal, no mocking libraries
# ---------------------------------------------------------------------------


class _FakeConn:
    """Thin wrapper so the existing sqlite3 connection satisfies the interface."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._conn.execute(sql, params)


class FakeConnectionProvider:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._inner = _FakeConn(conn)

    def get(self) -> Any:
        return self._inner

    def close_all(self) -> None:
        pass


class FakeUserStateRepository:
    def get(self, lot_id: int) -> LotUserState | None:
        return None

    def get_many(self, ids: Sequence[int]) -> dict[int, LotUserState]:
        return {}

    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None:
        pass

    def set_note(self, lot_id: int, note: str | None) -> None:
        pass

    def mark_visited(self, at: datetime) -> None:
        pass

    def last_visit(self) -> datetime | None:
        return None


class FakeLotRepository:
    def upsert(
        self, lot: Lot, *, tracked: Sequence[TrackedField], is_backfill: bool = False
    ) -> LotUpsertResult:
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


class FakeClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


def _make_service(conn: sqlite3.Connection) -> LotQueryService:
    return LotQueryService(
        lot_repo=FakeLotRepository(),
        user_state_repo=FakeUserStateRepository(),
        conn_provider=FakeConnectionProvider(conn),
        clock=FakeClock(),
    )


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

# subscription calendar date: 2026-05-15 (same day as _NOW)
_SUBSCRIBED_AT = datetime(2026, 5, 15, 10, 30, 0, tzinfo=UTC)  # 10:30 on the 15th

# Lot dates relative to _SUBSCRIBED_AT calendar date (2026-05-15)
_DATE_SAME_DAY = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)   # same calendar day → PASS
_DATE_BEFORE = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)     # one day before → SUPPRESS
_DATE_AFTER = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)      # day after → PASS

_REGION_WITH_SUB = 1     # region_id that has a subscription row
_REGION_WITHOUT_SUB = 2  # region_id with NO subscription row → pass (fail-open)


def _populate_db(conn: sqlite3.Connection) -> dict[int, tuple[datetime, int | None]]:
    """Insert test lots and one subscription; return {lot_id: (date_create, region_id)}."""
    # Lot 1: region with subscription, date_create == subscribed_at day → PASS
    _insert_lot(conn, lot_id=1, date_create=_DATE_SAME_DAY, region_id=_REGION_WITH_SUB)
    # Lot 2: region with subscription, date_create day BEFORE subscribed_at → SUPPRESS
    _insert_lot(conn, lot_id=2, date_create=_DATE_BEFORE, region_id=_REGION_WITH_SUB)
    # Lot 3: region with subscription, date_create day AFTER → PASS
    _insert_lot(conn, lot_id=3, date_create=_DATE_AFTER, region_id=_REGION_WITH_SUB)
    # Lot 4: region_id set but NO subscription row → PASS (fail-open)
    _insert_lot(conn, lot_id=4, date_create=_DATE_BEFORE, region_id=_REGION_WITHOUT_SUB)
    # Lot 5: region_id NULL → PASS (fail-open)
    _insert_lot(conn, lot_id=5, date_create=_DATE_BEFORE, region_id=None)

    _insert_subscription(conn, region_id=_REGION_WITH_SUB, subscribed_at=_SUBSCRIBED_AT)
    conn.commit()

    return {
        1: (_DATE_SAME_DAY, _REGION_WITH_SUB),
        2: (_DATE_BEFORE, _REGION_WITH_SUB),
        3: (_DATE_AFTER, _REGION_WITH_SUB),
        4: (_DATE_BEFORE, _REGION_WITHOUT_SUB),
        5: (_DATE_BEFORE, None),
    }


def _python_expected_ids(
    lot_rows: dict[int, tuple[datetime, int | None]],
    subscriptions: dict[int, datetime],
) -> set[int]:
    """Apply Python predicate to derive the expected passing lot IDs."""
    result: set[int] = set()
    for lot_id, (date_create, region_id) in lot_rows.items():
        subscribed_at = subscriptions.get(region_id) if region_id is not None else None
        if passes_subscription_cutoff(date_create, subscribed_at, region_id=region_id):
            result.add(lot_id)
    return result


def test_sql_executes_no_ambiguous_column_error(db_conn: sqlite3.Connection) -> None:
    """Regression: apply_subscription_cutoff=True must not raise ambiguous column name.

    Previously ``SELECT ... region_id FROM lots LEFT JOIN region_subscriptions ...``
    caused sqlite3.OperationalError: ambiguous column name: region_id.
    """
    _populate_db(db_conn)
    svc = _make_service(db_conn)
    # This must not raise — the query executes against real SQLite
    page = svc.search(LotFilters(apply_subscription_cutoff=True), page_size=50)
    # At least some results expected (lots 1, 3, 4, 5 pass)
    assert len(page.items) >= 1


def test_sql_predicate_equivalence(db_conn: sqlite3.Connection) -> None:
    """SQL cutoff filter returns exactly the same lot-id set as the Python predicate.

    This is the guard against SQL/predicate divergence. Both must agree on every
    test scenario: same-day, historical, day-after, no-subscription, null-region.
    """
    lot_rows = _populate_db(db_conn)
    subscriptions = {_REGION_WITH_SUB: _SUBSCRIBED_AT}

    svc = _make_service(db_conn)
    page = svc.search(LotFilters(apply_subscription_cutoff=True), page_size=50)
    sql_ids = {dto.id for dto in page.items}

    expected_ids = _python_expected_ids(lot_rows, subscriptions)
    assert sql_ids == expected_ids, (
        f"SQL filter returned {sql_ids}, Python predicate expected {expected_ids}"
    )


def test_without_cutoff_all_active_lots_returned(db_conn: sqlite3.Connection) -> None:
    """apply_subscription_cutoff=False (the /lots API path) returns ALL active lots.

    Proves the JOIN-free path is unaffected and historical lots are only hidden
    when the flag is True.
    """
    _populate_db(db_conn)
    svc = _make_service(db_conn)
    page = svc.search(LotFilters(apply_subscription_cutoff=False), page_size=50)
    assert {dto.id for dto in page.items} == {1, 2, 3, 4, 5}


def test_gn89_same_day_regression(db_conn: sqlite3.Connection) -> None:
    """gn89 regression: a lot whose date_create is midnight UTC on the subscription
    calendar day passes even though subscribed_at is 10:30 UTC the same day.

    Full-timestamp comparison would give 00:00:00 < 10:30:00 → suppress (wrong).
    Day-precision comparison gives 2026-05-15 >= 2026-05-15 → pass (correct).
    """
    # Same-day lot: date_create 2026-05-15T00:00:00+00:00, subscribed_at 2026-05-15T10:30:00+00:00
    _insert_lot(db_conn, lot_id=10, date_create=_DATE_SAME_DAY, region_id=_REGION_WITH_SUB)
    _insert_subscription(db_conn, region_id=_REGION_WITH_SUB, subscribed_at=_SUBSCRIBED_AT)
    db_conn.commit()

    svc = _make_service(db_conn)
    page = svc.search(LotFilters(apply_subscription_cutoff=True), page_size=50)
    assert 10 in {dto.id for dto in page.items}, (
        "Lot 10 (same-day, midnight) must pass the subscription cutoff (gn89)"
    )
