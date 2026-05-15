"""Unit tests for FilterMatcher implementations.

Covers:
  - ``RfSubjectFilterMatcher``: empty filter → True; matching region → True;
    non-matching region → False; unknown region name → True (fail-open).
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


def _make_lot(region_name: str) -> LotPublicDTO:
    """Return a minimal ``LotPublicDTO`` for the given region name string."""
    return LotPublicDTO(
        id=1,
        cadastral_no="77:01:0000001:1",
        area_sqm=1000,
        region=region_name,
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
    )


# ---------------------------------------------------------------------------
# RfSubjectFilterMatcher
# ---------------------------------------------------------------------------

class TestRfSubjectFilterMatcher:
    """Parametrised and edge-case tests for RfSubjectFilterMatcher."""

    def setup_method(self) -> None:
        self.matcher = RfSubjectFilterMatcher()

    def test_empty_filter_passes_all(self) -> None:
        """Empty rf_subjects → no filter set → pass everything through."""
        lot = _make_lot("Хабаровский край")  # site-id=89
        filters = FiltersConfig(rf_subjects=[])
        assert self.matcher.matches(lot, filters) is True

    def test_matching_region_passes(self) -> None:
        """Lot whose region name maps to a site-id in rf_subjects → True."""
        lot = _make_lot("Хабаровский край")  # site-id=89
        filters = FiltersConfig(rf_subjects=[89, 88])
        assert self.matcher.matches(lot, filters) is True

    def test_non_matching_region_blocked(self) -> None:
        """Lot whose region maps to a site-id NOT in rf_subjects → False."""
        lot = _make_lot("Хабаровский край")  # site-id=89
        filters = FiltersConfig(rf_subjects=[88, 90])  # Приморский + Амурская
        assert self.matcher.matches(lot, filters) is False

    def test_unknown_region_name_passes(self) -> None:
        """Region name not in the SSOT map → fail-open (True).

        This ensures new upstream regions are never silently dropped
        when the SSOT lags behind site updates.
        """
        lot = _make_lot("Несуществующий край")
        filters = FiltersConfig(rf_subjects=[88, 89, 90])
        assert self.matcher.matches(lot, filters) is True

    @pytest.mark.parametrize("region_name,region_id", [
        ("Приморский край", 88),
        ("Хабаровский край", 89),
        ("Амурская область", 90),
        ("Республика Саха (Якутия)", 87),
        ("Республика Карелия", 27),
        ("Мурманская область", 34),
    ])
    def test_known_regions_correct_id_mapping(
        self, region_name: str, region_id: int
    ) -> None:
        """Spot-check that known site-id region names map to their correct IDs."""
        lot = _make_lot(region_name)
        # Filter includes only the correct id → should pass.
        assert self.matcher.matches(lot, FiltersConfig(rf_subjects=[region_id])) is True
        # Filter excludes the id → should block.
        other_id = region_id + 1 if region_id < 96 else region_id - 1
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
        return _make_lot("Москва")

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
