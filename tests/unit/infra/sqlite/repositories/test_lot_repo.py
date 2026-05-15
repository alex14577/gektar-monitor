"""Unit tests for SqliteLotRepository additions.

Coverage:
  1. upsert basic — new lot insertion, second upsert, kwarg-free call.
  2. count_active — returns 0 on empty DB, correct count after inserts.

Note: The ``notify`` parameter was removed from upsert (P1-3) — it was a dead
parameter that was never read by the repository implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.models import Lot
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.lots import SqliteLotRepository

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FixedClock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


def _make_lot(lot_id: int = 1, *, is_active: bool = True) -> Lot:
    return Lot(
        id=lot_id,
        cadastral_no=f"77:01:{lot_id:06d}:1",
        area_sqm=500,
        region="77",
        municipality="Тест",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=_NOW,
        date_update=_NOW,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=_NOW,
        last_seen=_NOW,
        detail_fetched_at=None,
        enrichment_status="pending",
        last_seen_at=_NOW,
        is_active=is_active,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
    )


def _make_repo(conn_provider: ConnectionProvider) -> SqliteLotRepository:
    return SqliteLotRepository(conn_provider=conn_provider, clock=_FixedClock())


# ---------------------------------------------------------------------------
# Test 1: upsert returns correct result (P1-3: notify parameter removed)
# ---------------------------------------------------------------------------

class TestUpsertBasic:
    def test_upsert_new_lot_returns_was_new_true(self, tmp_db: ConnectionProvider) -> None:
        """New lot insertion returns was_new=True and an empty changes list."""
        repo = _make_repo(tmp_db)
        lot = _make_lot(1)

        result = repo.upsert(lot, tracked=("status",))

        assert result.was_new is True
        assert isinstance(result.changes, list)

    def test_upsert_second_upsert_returns_was_new_false(
        self, tmp_db: ConnectionProvider
    ) -> None:
        """Second upsert for same lot returns was_new=False and lot is persisted."""
        repo = _make_repo(tmp_db)
        lot = _make_lot(2)

        r1 = repo.upsert(lot, tracked=("status",))
        assert r1.was_new is True

        r2 = repo.upsert(lot, tracked=("status",))
        assert r2.was_new is False

        # Verify the lot is in DB
        fetched = repo.get(2)
        assert fetched is not None
        assert fetched.id == 2

    def test_upsert_without_notify_kwarg_works(self, tmp_db: ConnectionProvider) -> None:
        """Calling upsert without any extra kwargs works — notify param was removed (P1-3)."""
        repo = _make_repo(tmp_db)
        lot = _make_lot(3)

        result = repo.upsert(lot, tracked=("status",))

        assert result.was_new is True


# ---------------------------------------------------------------------------
# Test 2: count_active
# ---------------------------------------------------------------------------

class TestCountActive:
    def test_count_active_empty_db(self, tmp_db: ConnectionProvider) -> None:
        """count_active returns 0 when no lots have been inserted."""
        repo = _make_repo(tmp_db)
        assert repo.count_active() == 0

    def test_count_active_with_active_lots(self, tmp_db: ConnectionProvider) -> None:
        """count_active returns the number of lots where is_active=1."""
        repo = _make_repo(tmp_db)

        for lot_id in [1, 2, 3]:
            repo.upsert(_make_lot(lot_id, is_active=True), tracked=("status",))

        assert repo.count_active() == 3

    def test_count_active_excludes_inactive(self, tmp_db: ConnectionProvider) -> None:
        """Inactive lots are not counted."""
        repo = _make_repo(tmp_db)

        repo.upsert(_make_lot(1, is_active=True), tracked=("status",))
        repo.upsert(_make_lot(2, is_active=True), tracked=("status",))
        repo.upsert(_make_lot(3, is_active=False), tracked=("status",))

        assert repo.count_active() == 2

    def test_count_active_after_mark_inactive(self, tmp_db: ConnectionProvider) -> None:
        """count_active decreases after mark_inactive."""
        repo = _make_repo(tmp_db)

        repo.upsert(_make_lot(1, is_active=True), tracked=("status",))
        repo.upsert(_make_lot(2, is_active=True), tracked=("status",))
        assert repo.count_active() == 2

        repo.mark_inactive(1, reason="test", at=_NOW)
        assert repo.count_active() == 1
