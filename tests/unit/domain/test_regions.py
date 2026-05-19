"""Unit tests for fis_monitor.domain.regions — SSOT region mapping."""

from __future__ import annotations

import pytest

from fis_monitor.domain.regions import (
    ALL_REGION_SLUGS,
    REGION_BY_SLUG,
    REGION_SLUG_BY_ID,
    REGION_TITLE_BY_SLUG,
    SUBJECT_TITLE_BY_ID,
    SUBJECTS_BY_MACRO,
    id_to_slug,
    ids_to_slugs,
    slug_to_id,
    slugs_to_ids,
    subject_id_by_title,
    subjects_for_macros,
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


class TestSubjectConstants:
    """Tests for SUBJECTS_BY_MACRO, SUBJECT_TITLE_BY_ID, and subjects_for_macros."""

    def test_subjects_by_macro_immutable(self) -> None:
        with pytest.raises(TypeError):
            SUBJECTS_BY_MACRO[999] = (1,)  # type: ignore[index]

    def test_subject_title_by_id_immutable(self) -> None:
        with pytest.raises(TypeError):
            SUBJECT_TITLE_BY_ID[999] = "test"  # type: ignore[index]

    def test_subjects_by_macro_dfo_has_11_entries(self) -> None:
        assert len(SUBJECTS_BY_MACRO[1]) == 11

    def test_subjects_by_macro_arctic_has_10_entries(self) -> None:
        assert len(SUBJECTS_BY_MACRO[2]) == 10

    def test_subject_title_by_id_has_19_entries(self) -> None:
        # 11 ДФО + 10 Арктика - 2 shared (87, 96) = 19 unique
        assert len(SUBJECT_TITLE_BY_ID) == 19

    def test_subjects_for_macros_dfo(self) -> None:
        result = subjects_for_macros([1])
        assert set(result) == set(SUBJECTS_BY_MACRO[1])
        assert len(result) == 11

    def test_subjects_for_macros_arctic(self) -> None:
        result = subjects_for_macros([2])
        assert set(result) == set(SUBJECTS_BY_MACRO[2])
        assert len(result) == 10

    def test_subjects_for_macros_both_deduplicates(self) -> None:
        """87 (Якутия) and 96 (Чукотка) appear in both — union must be 19 unique."""
        result = subjects_for_macros([1, 2])
        assert len(result) == 19
        # Dedup check: no duplicates in tuple
        assert len(set(result)) == len(result)
        # Both shared ids present exactly once
        assert result.count(87) == 1
        assert result.count(96) == 1

    def test_subjects_for_macros_unknown_silently_skipped(self) -> None:
        assert subjects_for_macros([999]) == ()

    def test_subjects_for_macros_empty(self) -> None:
        assert subjects_for_macros([]) == ()

    def test_subjects_for_macros_returns_tuple(self) -> None:
        result = subjects_for_macros([1])
        assert isinstance(result, tuple)

    def test_all_subject_ids_in_title_map(self) -> None:
        """Every site-id in SUBJECTS_BY_MACRO must have a title entry."""
        all_ids = {sid for sids in SUBJECTS_BY_MACRO.values() for sid in sids}
        assert all_ids == set(SUBJECT_TITLE_BY_ID.keys())


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


class TestSubjectIdByTitle:
    """Tests for subject_id_by_title — inverse map name → site-id (ADR-035 §I2, pc1g)."""

    def test_known_name_returns_site_id(self) -> None:
        """Canonical RF-subject name resolves to correct site-id."""
        assert subject_id_by_title("Республика Карелия") == 27

    def test_another_known_name(self) -> None:
        assert subject_id_by_title("Приморский край") == 88

    def test_unknown_name_returns_none(self) -> None:
        assert subject_id_by_title("Несуществующий регион") is None

    def test_empty_string_returns_none(self) -> None:
        assert subject_id_by_title("") is None

    def test_none_input_returns_none(self) -> None:
        assert subject_id_by_title(None) is None

    def test_inverse_roundtrip_for_all_catalog_entries(self) -> None:
        """Every (site_id, title) pair in SUBJECT_TITLE_BY_ID round-trips correctly."""
        for sid, title in SUBJECT_TITLE_BY_ID.items():
            assert subject_id_by_title(title) == sid
