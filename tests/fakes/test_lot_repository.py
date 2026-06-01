"""Smoke-test for canonical FakeLotRepository (ADR-041 §Fake signature canon).

Ensures every public method on the fake is callable without runtime errors —
guards against signature drift between Protocol and fake implementation.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.models import Lot
from tests.fakes.lot_repository import FakeLotRepository


def _sample_lot() -> Lot:
    """Build a minimal Lot for smoke purposes via model_construct (bypasses validators)."""
    now = datetime.now(UTC)
    return Lot.model_construct(
        id=1,
        cadastral_no="01:01:0000001:1",
        area_sqm=None,
        region="Республика Адыгея",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=now,
        date_update=None,
        date_registry=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=now,
        last_seen=now,
        detail_fetched_at=None,
        enrichment_status="pending",
        last_seen_at=now,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
        region_id=None,
    )


def test_fake_lot_repository_all_methods_callable() -> None:
    repo = FakeLotRepository(count_active_value=7)
    lot = _sample_lot()

    result = repo.upsert(lot, tracked=[])
    assert result.was_new is False

    assert repo.get(lot.id) == lot
    assert repo.list_active(limit=10, offset=0) == []
    assert repo.get_last_known_id(region=1) is None
    repo.set_last_known_id(region=1, value=42)
    assert repo.get_last_known_id(region=1) == 42

    now = datetime.now(UTC)
    repo.mark_seen(lot_ids=[lot.id], at=now)
    repo.mark_inactive(lot_id=lot.id, reason="test", at=now)
    assert repo.needing_enrichment(limit=5) == []

    assert repo.count_active() == 7
    assert repo.count_active(region_ids=(72,)) == 7
    assert repo.count_active_calls == [(), (72,)]
