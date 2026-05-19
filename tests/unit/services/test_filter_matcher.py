"""Unit tests for FilterMatcher implementations.

Covers:
  - ``RfSubjectFilterMatcher``: empty filter → True; region_id in list → True;
    region_id not in list → False; region_id=None → True (fail-open).
  - ``AllFiltersMatcher``: empty list → True; all-True → True; one-False → False.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import FiltersConfig, LotPublicDTO
from fis_monitor.services.filter_matcher import AllFiltersMatcher, RfSubjectFilterMatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_lot(region_id: int | None) -> LotPublicDTO:
    """Return a minimal ``LotPublicDTO`` for the given region_id."""
    return LotPublicDTO(
        id=1,
        cadastral_no="77:01:0000001:1",
        area_sqm=1000,
        region="Тестовый регион",
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
        enrichment_status="done",
        last_seen_at=_NOW,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
        age_seconds=0,
        tier="match",
        freshness="hot",
        region_id=region_id,
    )


# ---------------------------------------------------------------------------
# RfSubjectFilterMatcher
# ---------------------------------------------------------------------------

class TestRfSubjectFilterMatcher:
    """Tests for RfSubjectFilterMatcher using region_id directly."""

    def setup_method(self) -> None:
        self.matcher = RfSubjectFilterMatcher()

    def test_empty_filter_passes_all(self) -> None:
        """Empty rf_subjects → no filter set → pass everything through (I4)."""
        lot = _make_lot(region_id=89)
        filters = FiltersConfig(rf_subjects=[])
        assert self.matcher.matches(lot, filters) is True

    def test_region_id_in_filter_passes(self) -> None:
        """Lot whose region_id is in rf_subjects → True."""
        lot = _make_lot(region_id=89)
        filters = FiltersConfig(rf_subjects=[89, 88])
        assert self.matcher.matches(lot, filters) is True

    def test_region_id_not_in_filter_blocked(self) -> None:
        """Lot whose region_id is NOT in rf_subjects → False."""
        lot = _make_lot(region_id=89)
        filters = FiltersConfig(rf_subjects=[88, 90])
        assert self.matcher.matches(lot, filters) is False

    def test_region_id_none_fails_open(self) -> None:
        """region_id=None (unresolved) → fail-open → True (ADR-035 I2).

        Ensures lots from new/unrecognised regions are never silently dropped
        when the SSOT lags behind site updates.
        """
        lot = _make_lot(region_id=None)
        filters = FiltersConfig(rf_subjects=[88, 89, 90])
        assert self.matcher.matches(lot, filters) is True

    @pytest.mark.parametrize("region_id", [88, 89, 90, 87, 27, 34])
    def test_single_id_in_filter(self, region_id: int) -> None:
        """Spot-check: lot with a given region_id passes when that id is in filter."""
        lot = _make_lot(region_id=region_id)
        assert self.matcher.matches(lot, FiltersConfig(rf_subjects=[region_id])) is True

    @pytest.mark.parametrize("region_id", [88, 89, 90, 87, 27, 34])
    def test_single_id_not_in_filter(self, region_id: int) -> None:
        """Spot-check: lot with a given region_id is blocked when that id is absent."""
        other_id = region_id + 1 if region_id < 96 else region_id - 1
        lot = _make_lot(region_id=region_id)
        assert self.matcher.matches(lot, FiltersConfig(rf_subjects=[other_id])) is False


# ---------------------------------------------------------------------------
# AllFiltersMatcher
# ---------------------------------------------------------------------------

class _AlwaysTrue:
    """Fake FilterMatcher that always returns True."""

    def matches(self, lot: LotPublicDTO, filters: FiltersConfig) -> bool:
        return True


class _AlwaysFalse:
    """Fake FilterMatcher that always returns False."""

    def matches(self, lot: LotPublicDTO, filters: FiltersConfig) -> bool:
        return False


class TestAllFiltersMatcher:
    """Tests for AllFiltersMatcher composite AND semantics."""

    def _lot(self) -> LotPublicDTO:
        return _make_lot(region_id=89)

    def _filters(self) -> FiltersConfig:
        return FiltersConfig()

    def test_empty_list_passes(self) -> None:
        """No matchers → no constraints → True."""
        matcher = AllFiltersMatcher([])
        assert matcher.matches(self._lot(), self._filters()) is True

    def test_all_true_matchers_passes(self) -> None:
        """All matchers return True → True."""
        matcher = AllFiltersMatcher([_AlwaysTrue(), _AlwaysTrue()])
        assert matcher.matches(self._lot(), self._filters()) is True

    def test_one_false_blocks(self) -> None:
        """Any single False matcher blocks the lot → False."""
        matcher = AllFiltersMatcher([_AlwaysTrue(), _AlwaysFalse(), _AlwaysTrue()])
        assert matcher.matches(self._lot(), self._filters()) is False

    def test_all_false_blocks(self) -> None:
        """All False matchers → False."""
        matcher = AllFiltersMatcher([_AlwaysFalse(), _AlwaysFalse()])
        assert matcher.matches(self._lot(), self._filters()) is False

    def test_single_true_passes(self) -> None:
        """Single True matcher → True."""
        matcher = AllFiltersMatcher([_AlwaysTrue()])
        assert matcher.matches(self._lot(), self._filters()) is True

    def test_single_false_blocks(self) -> None:
        """Single False matcher → False."""
        matcher = AllFiltersMatcher([_AlwaysFalse()])
        assert matcher.matches(self._lot(), self._filters()) is False

    def test_rf_subject_matcher_false_path_via_composite(self) -> None:
        """AllFiltersMatcher wrapping RfSubjectFilterMatcher blocks excluded region_id.

        Per project CLAUDE.md: Protocol tests must invoke every method of fake-impls.
        Here we verify the real RfSubjectFilterMatcher is reached through the composite
        and that the false-branch (region_id excluded) propagates correctly.
        """
        rf_matcher = RfSubjectFilterMatcher()
        composite = AllFiltersMatcher([rf_matcher])
        # Lot has region_id=88 (Приморский край) but filter allows only region_id=27.
        lot = _make_lot(region_id=88)
        filters = FiltersConfig(rf_subjects=[27])
        assert composite.matches(lot, filters) is False
