"""Unit tests for LotQueryService.

Coverage targets (per task spec a4t.8 B1):

- ``_encode_cursor`` / ``_decode_cursor`` round-trip + malformed → ValueError
- ``_build_query`` for each filter combination (parametrised, no real SQL)
- ``search()`` with FakeConnectionProvider + FakeUserStateRepository (in-memory)
- ``has_more=True`` / ``next_cursor`` correct when len(rows) == page_size + 1
- ``fts_query`` → NotImplementedError
- page_size out of range → ValueError
- invalid status in LotFilters → ValueError
- Fake compliance: single test calls ALL methods on each fake (anti-mock §6)

Fakes follow the Protocol structural type — NO mocking libraries used.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from fis_monitor.domain.models import (
    Lot,
    LotUpsertResult,
    LotUserState,
    TrackedField,
)
from fis_monitor.services.lot_query import (
    LotFilters,
    LotQueryService,
    _decode_cursor,
    _encode_cursor,
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_FIRST_SEEN_HOT = datetime(2026, 5, 14, 11, 30, 0, tzinfo=UTC)   # 30 min ago → hot
_FIRST_SEEN_WARM = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)   # 24 h ago → cold/warm boundary
_FIRST_SEEN_COLD = datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)    # >1 day ago → cold

_LOT_COLS = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, lat, lon, "
    "has_boundaries, raw_json, parser_version, first_seen, last_seen, "
    "detail_fetched_at, enrichment_status, enrichment_retries, "
    "enrichment_last_error, last_seen_at, last_status, last_status_at, "
    "is_active, inactive_reason, inactive_since, inactive_confirmed_at, "
    "region_id"
)


def _make_db_row(
    lot_id: int = 1,
    area_sqm: int = 5000,
    region: str = "01",
    status: str = "Свободен",
    first_seen: datetime = _FIRST_SEEN_HOT,
    is_active: int = 1,
    region_id: int | None = None,
) -> sqlite3.Row:
    """Build an in-memory sqlite3.Row matching the lots SELECT column list."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE lots ({_LOT_COLS})"
    )
    ts = first_seen.isoformat()
    now_ts = _NOW.isoformat()
    conn.execute(
        "INSERT INTO lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            lot_id,
            f"27:01:{lot_id:08d}:0001",  # cadastral_no
            area_sqm,
            region,
            "Тест",          # municipality
            None,            # land_category
            None,            # permitted_use
            None,            # ogv
            status,
            now_ts,          # date_create
            None,            # date_update
            None, None,      # lat, lon
            None,            # has_boundaries
            "{}",            # raw_json
            1,               # parser_version
            ts,              # first_seen
            ts,              # last_seen
            None,            # detail_fetched_at
            "done",          # enrichment_status
            0,               # enrichment_retries
            None,            # enrichment_last_error
            ts,              # last_seen_at
            None,            # last_status
            None,            # last_status_at
            is_active,
            None, None, None,  # inactive_reason, inactive_since, inactive_confirmed_at
            region_id,
        ),
    )
    conn.commit()
    cur = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,))
    row = cur.fetchone()
    cur.close()
    return row


# ---------------------------------------------------------------------------
# Fake ConnectionProvider
# ---------------------------------------------------------------------------


class _FakeConn:
    """In-memory sqlite3 connection wrapper."""

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        # Build a real in-memory DB so execute() works
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"CREATE TABLE lots ({_LOT_COLS})")
        for row in rows:
            keys = row.keys()
            vals = tuple(row[col] for col in keys)
            placeholders = ",".join("?" * len(vals))
            self._conn.execute(f"INSERT INTO lots VALUES ({placeholders})", vals)
        self._conn.commit()

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._conn.execute(sql, params)


class FakeConnectionProvider:
    """Satisfies ConnectionProvider Protocol — returns an in-memory SQLite DB."""

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._conn = _FakeConn(rows)
        self.get_called = 0
        self.close_all_called = 0

    def get(self) -> Any:
        self.get_called += 1
        return self._conn

    def close_all(self) -> None:
        self.close_all_called += 1


# ---------------------------------------------------------------------------
# Fake UserStateRepository
# ---------------------------------------------------------------------------


class FakeUserStateRepository:
    """In-memory UserStateRepository — all Protocol methods implemented."""

    def __init__(self, states: dict[int, LotUserState] | None = None) -> None:
        self._states: dict[int, LotUserState] = states or {}
        # call-tracking for anti-mock test
        self.get_calls: list[int] = []
        self.get_many_calls: list[Sequence[int]] = []
        self.set_starred_calls: list[tuple[int, bool]] = []
        self.set_submitted_calls: list[Any] = []
        self.set_note_calls: list[Any] = []
        self.mark_visited_calls: list[datetime] = []
        self.last_visit_calls = 0

    def get(self, lot_id: int) -> LotUserState | None:
        self.get_calls.append(lot_id)
        return self._states.get(lot_id)

    def get_many(self, ids: Sequence[int]) -> dict[int, LotUserState]:
        self.get_many_calls.append(list(ids))
        return {i: self._states[i] for i in ids if i in self._states}

    def set_starred(self, lot_id: int, value: bool) -> None:
        self.set_starred_calls.append((lot_id, value))

    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None:
        self.set_submitted_calls.append((lot_id, value, at))

    def set_note(self, lot_id: int, note: str | None) -> None:
        self.set_note_calls.append((lot_id, note))

    def mark_visited(self, at: datetime) -> None:
        self.mark_visited_calls.append(at)

    def last_visit(self) -> datetime | None:
        self.last_visit_calls += 1
        return None


# ---------------------------------------------------------------------------
# Fake LotRepository (minimal — not hot-path for LotQueryService)
# ---------------------------------------------------------------------------


class FakeLotRepository:
    """Satisfies LotRepository Protocol structurally."""

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


# ---------------------------------------------------------------------------
# Fake Clock
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Anti-mock: ALL fake methods called in a single test
# ---------------------------------------------------------------------------


def test_fake_user_state_repo_all_methods_called() -> None:
    """Verify every method of FakeUserStateRepository is exercised.

    Rationale: per orchestrator-playbook §6, fakes must expose runtime bugs
    (e.g. wrong signatures) — an isinstance() check alone won't catch them.
    """
    repo = FakeUserStateRepository()
    state = LotUserState(
        lot_id=1,
        starred=True,
        submitted=False,
        submitted_at=None,
        note="n",
        seen_at=None,
        updated_at=_NOW,
    )
    repo._states[1] = state

    assert repo.get(1) is state
    result = repo.get_many([1, 99])
    assert result == {1: state}
    repo.set_starred(1, False)
    repo.set_submitted(1, True, _NOW)
    repo.set_note(1, "hello")
    repo.mark_visited(_NOW)
    lv = repo.last_visit()
    assert lv is None

    assert repo.get_calls == [1]
    assert repo.get_many_calls == [[1, 99]]
    assert repo.set_starred_calls == [(1, False)]


def test_fake_connection_provider_all_methods_called() -> None:
    """Verify both ConnectionProvider methods are exercised."""
    row = _make_db_row(lot_id=42)
    provider = FakeConnectionProvider([row])
    conn = provider.get()
    assert conn is not None
    provider.close_all()
    assert provider.get_called == 1
    assert provider.close_all_called == 1


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_encode_decode_cursor_round_trip() -> None:
    for lot_id in (1, 42, 999_999):
        encoded = _encode_cursor(lot_id)
        assert _decode_cursor(encoded) == lot_id


def test_decode_cursor_malformed_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid page cursor"):
        _decode_cursor("not-valid-base64!!!")


def test_decode_cursor_non_integer_content_raises_value_error() -> None:
    # Valid base64 but not an integer
    bad = base64.urlsafe_b64encode(b"abc").decode()
    with pytest.raises(ValueError, match="Invalid page cursor"):
        _decode_cursor(bad)


# ---------------------------------------------------------------------------
# LotFilters validation
# ---------------------------------------------------------------------------


def test_lot_filters_unknown_status_raises() -> None:
    with pytest.raises(ValueError, match="Unknown lot status"):
        LotFilters(status="НеизвестныйСтатус")


def test_lot_filters_known_status_ok() -> None:
    f = LotFilters(status="Свободен")
    assert f.status == "Свободен"


def test_lot_filters_none_status_ok() -> None:
    f = LotFilters()
    assert f.status is None


# ---------------------------------------------------------------------------
# _build_query — parametrised without SQL execution
# ---------------------------------------------------------------------------


def _make_service(rows: list[sqlite3.Row] | None = None) -> LotQueryService:
    return LotQueryService(
        lot_repo=FakeLotRepository(),
        user_state_repo=FakeUserStateRepository(),
        conn_provider=FakeConnectionProvider(rows or []),
        clock=FakeClock(),
    )


@pytest.mark.parametrize(
    "filters,last_id,limit,expected_fragments,not_expected",
    [
        # No filters — only is_active + LIMIT
        (
            LotFilters(),
            None,
            10,
            ["is_active = 1", "ORDER BY id ASC LIMIT ?"],
            ["region IN", "area_sqm >=", "area_sqm <=", "status =", "id >"],
        ),
        # Regions filter
        (
            LotFilters(regions=(1, 2, 3)),
            None,
            5,
            ["region IN (?, ?, ?)"],
            ["area_sqm >=", "area_sqm <=", "id >"],
        ),
        # area_sqm_min
        (
            LotFilters(area_sqm_min=Decimal("100")),
            None,
            5,
            ["area_sqm >= ?"],
            ["area_sqm <=", "id >"],
        ),
        # area_sqm_max
        (
            LotFilters(area_sqm_max=Decimal("5000")),
            None,
            5,
            ["area_sqm <= ?"],
            ["area_sqm >="],
        ),
        # Both area bounds
        (
            LotFilters(area_sqm_min=Decimal("100"), area_sqm_max=Decimal("5000")),
            None,
            5,
            ["area_sqm >= ?", "area_sqm <= ?"],
            [],
        ),
        # Status filter
        (
            LotFilters(status="Свободен"),
            None,
            5,
            ["status = ?"],
            [],
        ),
        # Cursor (last_id)
        (
            LotFilters(),
            42,
            5,
            ["id > ?"],
            [],
        ),
        # All filters combined
        (
            LotFilters(
                regions=(7,),
                area_sqm_min=Decimal("50"),
                area_sqm_max=Decimal("1000"),
                status="Зарезервирован",
            ),
            99,
            20,
            ["region IN (?)", "area_sqm >= ?", "area_sqm <= ?", "status = ?", "id > ?"],
            [],
        ),
    ],
)
def test_build_query_sql_fragments(
    filters: LotFilters,
    last_id: int | None,
    limit: int,
    expected_fragments: list[str],
    not_expected: list[str],
) -> None:
    svc = _make_service()
    sql, params = svc._build_query(filters, last_id=last_id, limit=limit)
    for fragment in expected_fragments:
        assert fragment in sql, f"Expected {fragment!r} in SQL: {sql!r}"
    for fragment in not_expected:
        assert fragment not in sql, f"Did NOT expect {fragment!r} in SQL: {sql!r}"
    # LIMIT param is always last
    assert params[-1] == limit


def test_build_query_regions_params() -> None:
    svc = _make_service()
    _sql, params = svc._build_query(
        LotFilters(regions=(10, 20)), last_id=None, limit=5
    )
    assert "10" in params
    assert "20" in params


def test_build_query_area_sqm_params() -> None:
    svc = _make_service()
    _, params = svc._build_query(
        LotFilters(area_sqm_min=Decimal("100.9"), area_sqm_max=Decimal("500.1")),
        last_id=None,
        limit=5,
    )
    assert 100 in params   # truncated to int
    assert 500 in params


# ---------------------------------------------------------------------------
# search() — integration with fakes (no real DB)
# ---------------------------------------------------------------------------


def _make_state(lot_id: int) -> LotUserState:
    return LotUserState(
        lot_id=lot_id,
        starred=True,
        submitted=False,
        submitted_at=None,
        note=None,
        seen_at=None,
        updated_at=_NOW,
    )


def test_search_empty_result() -> None:
    svc = _make_service(rows=[])
    page = svc.search(LotFilters())
    assert page.items == ()
    assert page.next_cursor is None
    assert page.has_more is False


def test_search_single_lot_no_user_state() -> None:
    row = _make_db_row(lot_id=1)
    svc = _make_service(rows=[row])
    page = svc.search(LotFilters())
    assert len(page.items) == 1
    dto = page.items[0]
    assert dto.id == 1
    assert dto.starred is False  # default
    assert dto.freshness == "hot"  # first_seen 30 min ago
    assert page.has_more is False


def test_search_lot_with_user_state() -> None:
    row = _make_db_row(lot_id=5)
    state = _make_state(5)
    repo = FakeUserStateRepository(states={5: state})
    svc = LotQueryService(
        lot_repo=FakeLotRepository(),
        user_state_repo=repo,
        conn_provider=FakeConnectionProvider([row]),
        clock=FakeClock(),
    )
    page = svc.search(LotFilters())
    assert len(page.items) == 1
    assert page.items[0].starred is True
    # get_many called once, not N get() calls
    assert len(repo.get_many_calls) == 1
    assert repo.get_calls == []  # no individual get() calls on hot-path


def test_search_has_more_true_when_extra_row() -> None:
    """Fetch page_size+1 rows → has_more=True, next_cursor set to last returned id."""
    rows = [_make_db_row(lot_id=i) for i in range(1, 7)]  # 6 rows
    svc = _make_service(rows=rows)
    page = svc.search(LotFilters(), page_size=5)
    assert page.has_more is True
    assert page.next_cursor is not None
    assert len(page.items) == 5
    # next_cursor encodes the last item id (5)
    assert _decode_cursor(page.next_cursor) == 5


def test_search_has_more_false_exact_page() -> None:
    """Exactly page_size rows → has_more=False, no next_cursor."""
    rows = [_make_db_row(lot_id=i) for i in range(1, 6)]  # 5 rows
    svc = _make_service(rows=rows)
    page = svc.search(LotFilters(), page_size=5)
    assert page.has_more is False
    assert page.next_cursor is None
    assert len(page.items) == 5


def test_search_cursor_decoding_used() -> None:
    """Passing a cursor adds id > ? to the query and fetches from there."""
    rows = [_make_db_row(lot_id=10)]
    svc = _make_service(rows=rows)
    cursor = _encode_cursor(5)
    page = svc.search(LotFilters(), cursor=cursor)
    # Row with id=10 > 5, so it should be returned
    assert len(page.items) == 1
    assert page.items[0].id == 10


def test_search_malformed_cursor_raises_value_error() -> None:
    svc = _make_service()
    with pytest.raises(ValueError, match="Invalid page cursor"):
        svc.search(LotFilters(), cursor="!!!bad!!!")


def test_search_fts_query_raises_not_implemented() -> None:
    svc = _make_service()
    with pytest.raises(NotImplementedError):
        svc.search(LotFilters(fts_query="farm"))


def test_search_page_size_too_small_raises() -> None:
    svc = _make_service()
    with pytest.raises(ValueError, match="page_size"):
        svc.search(LotFilters(), page_size=0)


def test_search_page_size_too_large_raises() -> None:
    svc = _make_service()
    with pytest.raises(ValueError, match="page_size"):
        svc.search(LotFilters(), page_size=201)


def test_search_page_size_boundary_values_ok() -> None:
    svc = _make_service(rows=[])
    page1 = svc.search(LotFilters(), page_size=1)
    page200 = svc.search(LotFilters(), page_size=200)
    assert page1.items == ()
    assert page200.items == ()


# ---------------------------------------------------------------------------
# Freshness computation
# ---------------------------------------------------------------------------


def test_freshness_hot() -> None:
    row = _make_db_row(lot_id=1, first_seen=_FIRST_SEEN_HOT)
    svc = _make_service(rows=[row])
    page = svc.search(LotFilters())
    assert page.items[0].freshness == "hot"
    assert page.items[0].age_seconds < 3600


def test_freshness_cold_old_lot() -> None:
    row = _make_db_row(lot_id=2, first_seen=_FIRST_SEEN_COLD)
    svc = _make_service(rows=[row])
    page = svc.search(LotFilters())
    assert page.items[0].freshness == "cold"
    assert page.items[0].age_seconds >= 86400


def test_search_region_id_propagates_to_dto() -> None:
    """region_id from DB row must appear in LotUserDTO (read-path fix)."""
    row = _make_db_row(lot_id=10, region_id=1)
    svc = _make_service(rows=[row])
    page = svc.search(LotFilters())
    assert len(page.items) == 1
    assert page.items[0].region_id == 1
