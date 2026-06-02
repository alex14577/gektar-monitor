"""Layer 3 integration test — LotQueryService with filter_subscribed_subjects=True.

Tests:
- M1: subscribed subject, date_create BEFORE subscribed_at → SHOWN (core fix).
- M2: subscribed subject lots dated before/same/after subscribed_at → all SHOWN.
- M3: subject with NO subscription row → HIDDEN (core fix).
- M4: NULL region_id lot → SHOWN.
- M5: svc.count(filters) == len(svc.search(...).items) for filter_subscribed_subjects=True.
- M6: filter_subscribed_subjects=False → ALL active lots returned (default parity).

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
# Test data
# ---------------------------------------------------------------------------

_SUBSCRIBED_AT = datetime(2026, 5, 15, 10, 30, 0, tzinfo=UTC)

_DATE_BEFORE = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)   # before subscription
_DATE_SAME = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)     # same calendar day
_DATE_AFTER = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)    # after subscription

_REGION_SUBSCRIBED = 1    # has a row in region_subscriptions
_REGION_NO_SUB = 2        # no row in region_subscriptions


@pytest.fixture()
def populated(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    """Insert lots covering all M1–M6 scenarios; return the connection."""
    # Lot 1: subscribed subject, date_create BEFORE subscribed_at → must SHOW (M1/M2)
    _insert_lot(db_conn, lot_id=1, date_create=_DATE_BEFORE, region_id=_REGION_SUBSCRIBED)
    # Lot 2: subscribed subject, date_create SAME DAY as subscribed_at → must SHOW (M2)
    _insert_lot(db_conn, lot_id=2, date_create=_DATE_SAME, region_id=_REGION_SUBSCRIBED)
    # Lot 3: subscribed subject, date_create AFTER subscribed_at → must SHOW (M2)
    _insert_lot(db_conn, lot_id=3, date_create=_DATE_AFTER, region_id=_REGION_SUBSCRIBED)
    # Lot 4: NO subscription row for this region → must HIDE (M3)
    _insert_lot(db_conn, lot_id=4, date_create=_DATE_BEFORE, region_id=_REGION_NO_SUB)
    # Lot 5: region_id NULL → must SHOW (M4)
    _insert_lot(db_conn, lot_id=5, date_create=_DATE_BEFORE, region_id=None)

    _insert_subscription(db_conn, region_id=_REGION_SUBSCRIBED, subscribed_at=_SUBSCRIBED_AT)
    db_conn.commit()
    return db_conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_FILTERS = LotFilters(filter_subscribed_subjects=True)


def test_m1_subscribed_historical_lot_shown(populated: sqlite3.Connection) -> None:
    """M1: subscribed subject lot with date_create BEFORE subscribed_at is shown."""
    svc = _make_service(populated)
    ids = {dto.id for dto in svc.search(_FILTERS, page_size=50).items}
    assert 1 in ids, "Lot 1 (subscribed region, date_create before subscribed_at) must be shown"


def test_m2_all_dates_of_subscribed_subject_shown(populated: sqlite3.Connection) -> None:
    """M2: all lots of a subscribed subject (before/same/after) are shown."""
    svc = _make_service(populated)
    ids = {dto.id for dto in svc.search(_FILTERS, page_size=50).items}
    assert {1, 2, 3}.issubset(ids), (
        f"All lots of subscribed region must appear; got ids={ids}"
    )


def test_m3_unsubscribed_subject_hidden(populated: sqlite3.Connection) -> None:
    """M3: lot whose region has no subscription row is hidden."""
    svc = _make_service(populated)
    ids = {dto.id for dto in svc.search(_FILTERS, page_size=50).items}
    assert 4 not in ids, "Lot 4 (no subscription row) must be hidden"


def test_m4_null_region_id_shown(populated: sqlite3.Connection) -> None:
    """M4: lot with NULL region_id is always shown (parity with ADR-039)."""
    svc = _make_service(populated)
    ids = {dto.id for dto in svc.search(_FILTERS, page_size=50).items}
    assert 5 in ids, "Lot 5 (region_id NULL) must be shown"


def test_m5_count_matches_search(populated: sqlite3.Connection) -> None:
    """M5: svc.count(filters) equals len(svc.search(...).items)."""
    svc = _make_service(populated)
    page = svc.search(_FILTERS, page_size=50)
    total = svc.count(_FILTERS)
    assert total == len(page.items), (
        f"count()={total} must match len(search().items)={len(page.items)}"
    )


def test_m6_no_filter_returns_all_active(populated: sqlite3.Connection) -> None:
    """M6: filter_subscribed_subjects=False returns ALL active lots (default parity)."""
    svc = _make_service(populated)
    ids = {dto.id for dto in svc.search(LotFilters(), page_size=50).items}
    assert ids == {1, 2, 3, 4, 5}, f"All 5 active lots must appear without filter; got {ids}"


def test_both_subscription_flags_mutually_exclusive() -> None:
    """LotFilters rejects apply_subscription_cutoff + filter_subscribed_subjects together."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        LotFilters(apply_subscription_cutoff=True, filter_subscribed_subjects=True)
