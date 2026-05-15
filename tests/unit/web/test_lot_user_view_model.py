"""Tests for LotUserViewModel.in_subscribed_subjects (Layer 3 — web/template).

Invariants:
- chip shown when lot.region_id (int, macro) is in settings.regions
- chip hidden when lot.region_id is not subscribed
- chip hidden when lot.region_id is None (legacy row, graceful)
"""
from __future__ import annotations

from fis_monitor.domain.models import LotUserDTO
from fis_monitor.web.sse_encoder import LotUserViewModel
from tests.factories import make_lot

_DEFAULT_NOW = make_lot().first_seen


def _make_dto(**overrides: object) -> LotUserDTO:
    base = make_lot(**overrides)
    return LotUserDTO(
        **base.model_dump(),
        age_seconds=60,
        tier="match",
        freshness="warm",
        starred=False,
    )


def test_in_subscribed_subjects_true_when_region_id_matches() -> None:
    dto = _make_dto(region_id=1)
    vm = LotUserViewModel(dto, subscribed_regions=frozenset({1, 2}))
    assert vm.in_subscribed_subjects is True


def test_in_subscribed_subjects_false_when_region_id_not_in_subscribed() -> None:
    dto = _make_dto(region_id=3)
    vm = LotUserViewModel(dto, subscribed_regions=frozenset({1, 2}))
    assert vm.in_subscribed_subjects is False


def test_in_subscribed_subjects_false_when_region_id_none() -> None:
    dto = _make_dto(region_id=None)
    vm = LotUserViewModel(dto, subscribed_regions=frozenset({1, 2}))
    assert vm.in_subscribed_subjects is False


def test_in_subscribed_subjects_false_when_subscribed_regions_empty() -> None:
    dto = _make_dto(region_id=1)
    vm = LotUserViewModel(dto, subscribed_regions=frozenset())
    assert vm.in_subscribed_subjects is False
