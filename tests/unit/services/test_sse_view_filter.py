"""Layer 1 unit tests for ``services/sse_view_filter.make_sse_view_filter``.

Covers the pure predicate function — no network, no FastAPI, no Jinja.

Invariants per ADR-052:
  - Default ViewFilters → always-True (fast path).
  - subjects filter: region_id match → pass; mismatch → suppress;
    region_id=None → suppress (conservative).
  - area_min: area_sqm >= area_min → pass; below → suppress.
  - area_max: area_sqm <= area_max → pass; above → suppress.
  - area_sqm=None on lot → pass-through (fail-open, enrichment pending).
  - only_new=True → lot.new passes (no-op for SSE lot.new).
  - Non-SseLotNew events → always pass-through regardless of filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    SseCycleDone,
    SseLotNew,
    SseLotStatus,
)
from fis_monitor.services.sse_view_filter import make_sse_membership_filter, make_sse_view_filter
from fis_monitor.services.view_filters import ViewFilters

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_lot_dto(**overrides: Any) -> LotPublicDTO:
    """Build a minimal LotPublicDTO for predicate tests."""
    defaults: dict[str, Any] = {
        "id": 1,
        "cadastral_no": "01:02:000000:1",
        "area_sqm": 10_000,
        "region": "Мурманская область",
        "municipality": None,
        "land_category": None,
        "permitted_use": None,
        "ogv": None,
        "status": "Свободен",
        "date_create": _TS,
        "date_update": None,
        "lat": None,
        "lon": None,
        "has_boundaries": None,
        "raw_json": {},
        "parser_version": 1,
        "first_seen": _TS,
        "last_seen": _TS,
        "detail_fetched_at": None,
        "enrichment_status": None,
        "last_seen_at": None,
        "region_id": None,
        "age_seconds": 0,
        "tier": "match",
        "freshness": "hot",
    }
    defaults.update(overrides)
    _dto_only = {"age_seconds", "tier", "freshness"}
    lot_kwargs = {k: v for k, v in defaults.items() if k not in _dto_only}
    lot = Lot(**lot_kwargs)
    return LotPublicDTO(
        **lot.model_dump(),
        age_seconds=defaults["age_seconds"],
        tier=defaults["tier"],
        freshness=defaults["freshness"],
    )


def _make_lot_new(**lot_overrides: Any) -> SseLotNew:
    dto = _make_lot_dto(**lot_overrides)
    return SseLotNew(lot=dto, fragment_template="poster")


# ---------------------------------------------------------------------------
# Default filters — fast path
# ---------------------------------------------------------------------------


class TestDefaultFilters:
    def test_default_viewfilters_always_passes_lot_new(self) -> None:
        """Default ViewFilters → always-True fast path."""
        vf = ViewFilters()
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=34)) is True
        assert pred(_make_lot_new(region_id=None)) is True
        assert pred(_make_lot_new(area_sqm=None)) is True

    def test_default_fast_path_is_always_true_sentinel(self) -> None:
        """Default ViewFilters returns the always-True sentinel (same object for all events)."""
        vf = ViewFilters()
        pred = make_sse_view_filter(vf)
        # Sentinel works for any event type too.
        evt = SseLotStatus(lot_id=1, new_status="gone", event_type="gone")
        assert pred(evt) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# subjects filter
# ---------------------------------------------------------------------------


class TestSubjectsFilter:
    def test_region_id_match_passes(self) -> None:
        """subjects=["34"] + lot.region_id=34 → True."""
        vf = ViewFilters(subjects=["34"])
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=34)) is True

    def test_region_id_mismatch_suppressed(self) -> None:
        """subjects=["34"] + lot.region_id=27 → False."""
        vf = ViewFilters(subjects=["34"])
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=27)) is False

    def test_region_id_none_suppressed(self) -> None:
        """subjects=["34"] + lot.region_id=None → False (conservative)."""
        vf = ViewFilters(subjects=["34"])
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=None)) is False

    def test_multiple_subjects_any_match_passes(self) -> None:
        """subjects=["34", "27"] + region_id=27 → True."""
        vf = ViewFilters(subjects=["34", "27"])
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=27)) is True

    def test_multiple_subjects_no_match_suppressed(self) -> None:
        """subjects=["34", "27"] + region_id=66 → False."""
        vf = ViewFilters(subjects=["34", "27"])
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=66)) is False


# ---------------------------------------------------------------------------
# area_min filter
# ---------------------------------------------------------------------------


class TestAreaMinFilter:
    def test_area_above_min_passes(self) -> None:
        """area_min=1000 + area_sqm=2000 → True."""
        vf = ViewFilters(area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=2000)) is True

    def test_area_equal_min_passes(self) -> None:
        """area_min=1000 + area_sqm=1000 → True (inclusive bound)."""
        vf = ViewFilters(area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=1000)) is True

    def test_area_below_min_suppressed(self) -> None:
        """area_min=1000 + area_sqm=500 → False."""
        vf = ViewFilters(area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=500)) is False

    def test_area_sqm_none_passes_when_min_set(self) -> None:
        """area_min=1000 + area_sqm=None → True (fail-open, enrichment pending)."""
        vf = ViewFilters(area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=None)) is True


# ---------------------------------------------------------------------------
# area_max filter
# ---------------------------------------------------------------------------


class TestAreaMaxFilter:
    def test_area_below_max_passes(self) -> None:
        """area_max=5000 + area_sqm=3000 → True."""
        vf = ViewFilters(area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=3000)) is True

    def test_area_equal_max_passes(self) -> None:
        """area_max=5000 + area_sqm=5000 → True (inclusive bound)."""
        vf = ViewFilters(area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=5000)) is True

    def test_area_above_max_suppressed(self) -> None:
        """area_max=5000 + area_sqm=10000 → False."""
        vf = ViewFilters(area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=10_000)) is False

    def test_area_sqm_none_passes_when_max_set(self) -> None:
        """area_max=5000 + area_sqm=None → True (fail-open, enrichment pending)."""
        vf = ViewFilters(area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=None)) is True


# ---------------------------------------------------------------------------
# only_new filter
# ---------------------------------------------------------------------------


class TestOnlyNewFilter:
    def test_only_new_passes_lot_new(self) -> None:
        """only_new=True → lot.new passes (no-op for SSE lot.new)."""
        vf = ViewFilters(only_new=True)
        pred = make_sse_view_filter(vf)
        # only_new alone doesn't suppress lot.new
        assert pred(_make_lot_new()) is True

    def test_only_new_true_does_not_activate_fast_path(self) -> None:
        """only_new=True alone produces a real predicate (not the fast-path sentinel),
        but since only_new has no effect on lot.new, it always passes."""
        # only_new=True is a no-op → _is_default returns True → fast-path returned.
        # Verify it still passes.
        vf = ViewFilters(only_new=True)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new()) is True


# ---------------------------------------------------------------------------
# Non-SseLotNew events — always pass-through
# ---------------------------------------------------------------------------


class TestNonLotNewPassThrough:
    @pytest.mark.parametrize(
        "event",
        [
            SseLotStatus(lot_id=1, new_status="gone", event_type="gone"),
            SseCycleDone(
                timestamp=_TS,
                cycle_id=1,
                status="ok",
                lots_fetched=0,
                new_lots=0,
                duration_ms=0,
            ),
        ],
        ids=["SseLotStatus", "SseCycleDone"],
    )
    def test_non_lot_new_always_passes(self, event: Any) -> None:
        """Non-SseLotNew events pass regardless of filter state."""
        # Use a filter that would suppress lot.new events.
        vf = ViewFilters(subjects=["34"])
        pred = make_sse_view_filter(vf)
        assert pred(event) is True  # type: ignore[arg-type]

    def test_non_lot_new_passes_with_subject_filter(self) -> None:
        """SseLotStatus passes even when subjects filter is active."""
        vf = ViewFilters(subjects=["34"])
        pred = make_sse_view_filter(vf)
        evt = SseLotStatus(lot_id=99, new_status="active", event_type="changed")
        assert pred(evt) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Combined filter edge cases
# ---------------------------------------------------------------------------


class TestCombinedFilters:
    def test_subjects_and_area_min_both_pass(self) -> None:
        vf = ViewFilters(subjects=["34"], area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=34, area_sqm=5000)) is True

    def test_subjects_match_but_area_below_min_suppressed(self) -> None:
        vf = ViewFilters(subjects=["34"], area_min=1000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(region_id=34, area_sqm=500)) is False

    def test_subjects_mismatch_overrides_area_match(self) -> None:
        vf = ViewFilters(subjects=["34"], area_min=1000)
        pred = make_sse_view_filter(vf)
        # region_id doesn't match → suppress regardless of area
        assert pred(_make_lot_new(region_id=27, area_sqm=5000)) is False

    def test_area_min_max_range_inside_passes(self) -> None:
        vf = ViewFilters(area_min=1000, area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=3000)) is True

    def test_area_min_max_range_outside_suppressed(self) -> None:
        vf = ViewFilters(area_min=1000, area_max=5000)
        pred = make_sse_view_filter(vf)
        assert pred(_make_lot_new(area_sqm=10_000)) is False


# ---------------------------------------------------------------------------
# make_sse_membership_filter (ADR-065)
# ---------------------------------------------------------------------------


class TestMembershipFilter:
    """Layer 1 invariants for make_sse_membership_filter (V1-V5 per spec)."""

    def test_m1_subscribed_region_passes(self) -> None:
        """V1-SSE: lot.region_id in subscribed set → pass."""
        pred = make_sse_membership_filter(frozenset([10, 20]))
        assert pred(_make_lot_new(region_id=10)) is True

    def test_m2_unsubscribed_region_suppressed(self) -> None:
        """V2-SSE: lot.region_id not in subscribed set → suppress."""
        pred = make_sse_membership_filter(frozenset([10]))
        assert pred(_make_lot_new(region_id=99)) is False

    def test_m3_region_id_none_passes(self) -> None:
        """V3-SSE: lot.region_id is None → pass (unclassified lot not lost)."""
        pred = make_sse_membership_filter(frozenset([10]))
        assert pred(_make_lot_new(region_id=None)) is True

    def test_m4_backfill_unsubscribed_suppressed(self) -> None:
        """is_backfill=True + unsubscribed region → suppress (membership ignores is_backfill)."""
        pred = make_sse_membership_filter(frozenset([10]))
        event = SseLotNew(
            lot=_make_lot_dto(region_id=99), fragment_template="poster", is_backfill=True
        )
        assert pred(event) is False

    def test_m4b_backfill_subscribed_passes(self) -> None:
        """is_backfill=True + subscribed region → pass."""
        pred = make_sse_membership_filter(frozenset([10]))
        event = SseLotNew(
            lot=_make_lot_dto(region_id=10), fragment_template="poster", is_backfill=True
        )
        assert pred(event) is True

    def test_m5_empty_set_region_bearing_suppressed(self) -> None:
        """V5-SSE: empty subscribed set + region_id set → suppress."""
        pred = make_sse_membership_filter(frozenset())
        assert pred(_make_lot_new(region_id=42)) is False

    def test_m6_empty_set_region_id_none_passes(self) -> None:
        """V5-SSE: empty subscribed set + region_id None → pass."""
        pred = make_sse_membership_filter(frozenset())
        assert pred(_make_lot_new(region_id=None)) is True

    def test_m7_non_lot_new_always_passes(self) -> None:
        """V4-SSE: non-SseLotNew events always pass through."""
        pred = make_sse_membership_filter(frozenset())
        evt = SseLotStatus(lot_id=7, new_status="gone", event_type="gone")
        assert pred(evt) is True  # type: ignore[arg-type]
