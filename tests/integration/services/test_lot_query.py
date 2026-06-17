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
_FIRST_SEEN_HOT = datetime(2026, 5, 14, 11, 30, 0, tzinfo=UTC)  # 30 min ago → hot
_FIRST_SEEN_WARM = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)  # 24 h ago → cold/warm boundary
_FIRST_SEEN_COLD = datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)  # >1 day ago → cold

_LOT_COLS = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, date_registry, "
    "lat, lon, has_boundaries, raw_json, parser_version, first_seen, last_seen, "
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
    date_create: datetime = _NOW,
) -> sqlite3.Row:
    """Build an in-memory sqlite3.Row matching the lots SELECT column list."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE lots ({_LOT_COLS})")
    ts = first_seen.isoformat()
    conn.execute(
        "INSERT INTO lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            lot_id,
            f"27:01:{lot_id:08d}:0001",  # cadastral_no
            area_sqm,
            region,
            "Тест",  # municipality
            None,  # land_category
            None,  # permitted_use
            None,  # ogv
            status,
            date_create.isoformat(),  # date_create
            None,  # date_update
            None,  # date_registry
            None,
            None,  # lat, lon
            None,  # has_boundaries
            "{}",  # raw_json
            1,  # parser_version
            ts,  # first_seen
            ts,  # last_seen
            None,  # detail_fetched_at
            "done",  # enrichment_status
            0,  # enrichment_retries
            None,  # enrichment_last_error
            ts,  # last_seen_at
            None,  # last_status
            None,  # last_status_at
            is_active,
            None,
            None,
            None,  # inactive_reason, inactive_since, inactive_confirmed_at
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
    repo.set_submitted(1, True, _NOW)
    repo.set_note(1, "hello")
    repo.mark_visited(_NOW)
    lv = repo.last_visit()
    assert lv is None

    assert repo.get_calls == [1]
    assert repo.get_many_calls == [[1, 99]]


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
    for lot_id, dt in [
        (1, _NOW),
        (42, _FIRST_SEEN_HOT),
        (999_999, _FIRST_SEEN_COLD),
    ]:
        encoded = _encode_cursor(dt, lot_id)
        date_iso, decoded_id = _decode_cursor(encoded)
        assert decoded_id == lot_id
        assert date_iso == dt.isoformat()


def test_decode_cursor_malformed_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid page cursor"):
        _decode_cursor("not-valid-base64!!!")


def test_decode_cursor_non_integer_content_raises_value_error() -> None:
    # Valid base64 but missing the colon separator → rfind returns -1 → ValueError
    bad = base64.urlsafe_b64encode(b"nodatecreate").decode()
    with pytest.raises(ValueError, match="Invalid page cursor"):
        _decode_cursor(bad)


# ---------------------------------------------------------------------------
# LotFilters validation
# ---------------------------------------------------------------------------


def test_lot_filters_regions_and_subject_display_names_raises() -> None:
    """Setting both regions and subject_display_names raises ValueError (AND-footgun guard)."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        LotFilters(regions=(1,), subject_display_names=("Мурманская область",))


def test_lot_filters_only_regions_ok() -> None:
    f = LotFilters(regions=(1, 2))
    assert f.regions == (1, 2)
    assert f.subject_display_names == ()


def test_lot_filters_only_subject_display_names_ok() -> None:
    f = LotFilters(subject_display_names=("Мурманская область",))
    assert f.subject_display_names == ("Мурманская область",)
    assert f.regions == ()


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


_CURSOR_42 = (_NOW.isoformat(), 42)  # (date_create_iso, lot_id) for cursor tests
_CURSOR_99 = (_NOW.isoformat(), 99)


@pytest.mark.parametrize(
    "filters,last_cursor,limit,expected_fragments,not_expected",
    [
        # No filters — only is_active + LIMIT; default sort is date_create DESC
        (
            LotFilters(),
            None,
            10,
            ["is_active = 1", "ORDER BY date_create DESC", "LIMIT ?"],
            ["region IN", "area_sqm >=", "area_sqm <=", "status =", "date_create <", "id <"],
        ),
        # Regions filter
        (
            LotFilters(regions=(1, 2, 3)),
            None,
            5,
            ["region IN (?, ?, ?)"],
            ["area_sqm >=", "area_sqm <=", "date_create <", "id <"],
        ),
        # area_sqm_min
        (
            LotFilters(area_sqm_min=Decimal("100")),
            None,
            5,
            ["area_sqm >= ?"],
            ["area_sqm <=", "date_create <", "id <"],
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
        # Cursor (desc) — composite keyset condition
        (
            LotFilters(),
            _CURSOR_42,
            5,
            ["date_create < ?", "date_create = ? AND id < ?"],
            ["date_create > ?", "id > ?"],
        ),
        # All filters combined (desc cursor)
        (
            LotFilters(
                regions=(7,),
                area_sqm_min=Decimal("50"),
                area_sqm_max=Decimal("1000"),
                status="Зарезервирован",
            ),
            _CURSOR_99,
            20,
            [
                "region IN (?)",
                "area_sqm >= ?",
                "area_sqm <= ?",
                "status = ?",
                "date_create < ?",
                "date_create = ? AND id < ?",
            ],
            [],
        ),
    ],
)
def test_build_query_sql_fragments(
    filters: LotFilters,
    last_cursor: tuple[str, int] | None,
    limit: int,
    expected_fragments: list[str],
    not_expected: list[str],
) -> None:
    svc = _make_service()
    sql, params = svc._build_query(filters, last_cursor=last_cursor, limit=limit)
    for fragment in expected_fragments:
        assert fragment in sql, f"Expected {fragment!r} in SQL: {sql!r}"
    for fragment in not_expected:
        assert fragment not in sql, f"Did NOT expect {fragment!r} in SQL: {sql!r}"
    # LIMIT param is always last
    assert params[-1] == limit


def test_build_query_regions_params() -> None:
    svc = _make_service()
    _sql, params = svc._build_query(LotFilters(regions=(10, 20)), last_cursor=None, limit=5)
    assert "10" in params
    assert "20" in params


def test_build_query_area_sqm_params() -> None:
    svc = _make_service()
    _, params = svc._build_query(
        LotFilters(area_sqm_min=Decimal("100.9"), area_sqm_max=Decimal("500.1")),
        last_cursor=None,
        limit=5,
    )
    assert 100 in params  # truncated to int
    assert 500 in params


# ---------------------------------------------------------------------------
# search() — integration with fakes (no real DB)
# ---------------------------------------------------------------------------


def _make_state(lot_id: int) -> LotUserState:
    return LotUserState(
        lot_id=lot_id,
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
    assert dto.submitted is False  # default
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
    assert page.items[0].submitted is False  # state merged from user_state_repo
    # get_many called once, not N get() calls
    assert len(repo.get_many_calls) == 1
    assert repo.get_calls == []  # no individual get() calls on hot-path


def test_search_has_more_true_when_extra_row() -> None:
    """Fetch page_size+1 rows → has_more=True, next_cursor encodes (date_create, id).

    Default sort is date_create DESC, id DESC. All 6 rows share the same date_create
    (NOW), so order is id DESC: 6,5,4,3,2,1. First page (5 items): 6,5,4,3,2.
    Last item id=2. Cursor must encode (date_create_iso, 2).
    """
    rows = [_make_db_row(lot_id=i) for i in range(1, 7)]  # 6 rows
    svc = _make_service(rows=rows)
    page = svc.search(LotFilters(), page_size=5)
    assert page.has_more is True
    assert page.next_cursor is not None
    assert len(page.items) == 5
    # next_cursor encodes the last item on the first page
    # With equal date_create values, ORDER BY id DESC → first page = [6,5,4,3,2], last=2
    date_iso, decoded_id = _decode_cursor(page.next_cursor)
    assert decoded_id == 2
    assert date_iso == _NOW.isoformat()


def test_search_has_more_false_exact_page() -> None:
    """Exactly page_size rows → has_more=False, no next_cursor."""
    rows = [_make_db_row(lot_id=i) for i in range(1, 6)]  # 5 rows
    svc = _make_service(rows=rows)
    page = svc.search(LotFilters(), page_size=5)
    assert page.has_more is False
    assert page.next_cursor is None
    assert len(page.items) == 5


def test_search_cursor_decoding_used() -> None:
    """Passing a cursor adds composite keyset condition and fetches correctly.

    We construct a cursor at (future_date, 99) in DESC direction, meaning we want
    rows with date_create < future_date OR (date_create = future_date AND id < 99).
    The row at id=10 with date_create=NOW satisfies date_create < future_date → returned.
    """
    from datetime import timedelta

    future = _NOW + timedelta(days=1)
    rows = [_make_db_row(lot_id=10)]
    svc = _make_service(rows=rows)
    # DESC cursor with date_create=future means: WHERE date_create < future OR ...
    # Row with date_create=NOW satisfies date_create < future → returned
    cursor = _encode_cursor(future, 99)
    page = svc.search(LotFilters(), cursor=cursor)
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


# ---------------------------------------------------------------------------
# Cursor-correctness regression tests (composite keyset on date_create, id)
# ---------------------------------------------------------------------------


def _walk_all_pages(
    svc: LotQueryService,
    filters: LotFilters,
    page_size: int = 3,
) -> list[int]:
    """Walk all pages using next_cursor and return the list of lot IDs in order."""
    all_ids: list[int] = []
    cursor: str | None = None
    while True:
        page = svc.search(filters, page_size=page_size, cursor=cursor)
        all_ids.extend(dto.id for dto in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
    return all_ids


def test_cursor_no_duplicates_no_gaps_distinct_dates() -> None:
    """Walk > page_size rows with DISTINCT date_create — every row appears exactly once (DESC).

    Invariant: unique == total and dupes == 0.
    """
    from datetime import timedelta

    base = datetime(2026, 1, 1, tzinfo=UTC)
    # 10 rows each with a distinct date_create, 1 day apart; id = 1..10
    rows = [_make_db_row(lot_id=i, date_create=base + timedelta(days=i)) for i in range(1, 11)]
    svc = _make_service(rows=rows)
    ids = _walk_all_pages(svc, LotFilters(), page_size=3)

    assert len(ids) == 10, f"Expected 10 ids, got {len(ids)}: {ids}"
    assert len(set(ids)) == 10, f"Duplicates found: {ids}"


def test_cursor_monotonic_order_distinct_dates() -> None:
    """Concatenated pages are monotonically ordered by date_create DESC."""
    from datetime import timedelta

    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [_make_db_row(lot_id=i, date_create=base + timedelta(days=i)) for i in range(1, 11)]
    svc = _make_service(rows=rows)
    ids = _walk_all_pages(svc, LotFilters(), page_size=3)

    # Recover date_create values for each id in returned order
    # date_create for id=i is base + timedelta(days=i)
    dates = [base + timedelta(days=i) for i in ids]
    assert dates == sorted(dates, reverse=True), f"Not monotone DESC: {dates}"


def test_cursor_no_duplicates_no_gaps_tied_dates() -> None:
    """Walk > page_size rows where ALL rows share the SAME date_create (DESC).

    Invariant: no row skipped or duplicated across page boundaries.
    The tie-breaking column is id, so pagination relies entirely on id ordering.
    """
    tied_date = datetime(2026, 3, 15, tzinfo=UTC)
    rows = [_make_db_row(lot_id=i, date_create=tied_date) for i in range(1, 11)]
    svc = _make_service(rows=rows)
    ids = _walk_all_pages(svc, LotFilters(), page_size=3)

    assert len(ids) == 10, f"Expected 10 ids, got {len(ids)}: {ids}"
    assert len(set(ids)) == 10, f"Duplicates found: {ids}"
