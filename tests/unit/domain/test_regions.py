"""Unit tests for fis_monitor.domain.regions — SSOT region mapping."""

from __future__ import annotations

import pytest

from fis_monitor.domain.regions import (
    ALL_REGION_SLUGS,
    REGION_BY_SLUG,
    REGION_SLUG_BY_ID,
    REGION_TITLE_BY_SLUG,
    id_to_slug,
    ids_to_slugs,
    slug_to_id,
    slugs_to_ids,
)


class TestConstants:
    def test_all_region_slugs_matches_region_by_slug_keys(self) -> None:
        assert set(ALL_REGION_SLUGS) == set(REGION_BY_SLUG.keys())

    def test_all_region_slugs_is_non_empty_tuple(self) -> None:
        assert isinstance(ALL_REGION_SLUGS, tuple)
        assert len(ALL_REGION_SLUGS) > 0

    def test_region_by_slug_immutable(self) -> None:
        with pytest.raises(TypeError):
            REGION_BY_SLUG["x"] = 999  # type: ignore[index]

    def test_region_slug_by_id_immutable(self) -> None:
        with pytest.raises(TypeError):
            REGION_SLUG_BY_ID[999] = "x"  # type: ignore[index]

    def test_region_title_by_slug_immutable(self) -> None:
        with pytest.raises(TypeError):
            REGION_TITLE_BY_SLUG["x"] = "Y"  # type: ignore[index]

    def test_region_by_slug_dfo(self) -> None:
        assert REGION_BY_SLUG["dfo"] == 1

    def test_region_by_slug_arctic(self) -> None:
        assert REGION_BY_SLUG["arctic"] == 2

    def test_region_title_by_slug_covers_all_slugs(self) -> None:
        assert set(REGION_TITLE_BY_SLUG.keys()) == set(ALL_REGION_SLUGS)

    def test_region_title_values_non_empty(self) -> None:
        for slug, title in REGION_TITLE_BY_SLUG.items():
            assert title, f"Empty title for slug {slug!r}"


class TestSlugToId:
    def test_dfo(self) -> None:
        assert slug_to_id("dfo") == 1

    def test_arctic(self) -> None:
        assert slug_to_id("arctic") == 2

    def test_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="unknown"):
            slug_to_id("unknown")

    def test_empty_string_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            slug_to_id("")


class TestIdToSlug:
    def test_1_returns_dfo(self) -> None:
        assert id_to_slug(1) == "dfo"

    def test_2_returns_arctic(self) -> None:
        assert id_to_slug(2) == "arctic"

    def test_unknown_returns_none(self) -> None:
        assert id_to_slug(999) is None

    def test_zero_returns_none(self) -> None:
        assert id_to_slug(0) is None


class TestBidirectionalRoundtrip:
    def test_slug_to_id_to_slug(self) -> None:
        for slug in ALL_REGION_SLUGS:
            id_ = slug_to_id(slug)
            assert id_to_slug(id_) == slug

    def test_id_to_slug_to_id(self) -> None:
        for id_, slug in REGION_SLUG_BY_ID.items():
            assert slug_to_id(slug) == id_


class TestSlugsToIds:
    def test_both_slugs(self) -> None:
        assert slugs_to_ids(["dfo", "arctic"]) == [1, 2]

    def test_single_dfo(self) -> None:
        assert slugs_to_ids(["dfo"]) == [1]

    def test_empty_list(self) -> None:
        assert slugs_to_ids([]) == []

    def test_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            slugs_to_ids(["dfo", "nonexistent"])


class TestIdsToSlugs:
    def test_both_ids(self) -> None:
        assert ids_to_slugs([1, 2]) == ["dfo", "arctic"]

    def test_single_id(self) -> None:
        assert ids_to_slugs([1]) == ["dfo"]

    def test_empty_list(self) -> None:
        assert ids_to_slugs([]) == []

    def test_unknown_ids_skipped(self) -> None:
        """Unknown IDs are silently dropped (lenient, display-only)."""
        assert ids_to_slugs([1, 999, 2]) == ["dfo", "arctic"]

    def test_all_unknown_returns_empty(self) -> None:
        assert ids_to_slugs([999, 1000]) == []
