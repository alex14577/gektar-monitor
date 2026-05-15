"""Unit tests for TorgiUrlBuilder.

Tests:
- default base_url produces correct lot_list_url for a region
- custom base_url substitutes correctly
- lot_detail_url produces correct URL for a lot_id
- region=1 (int) formats correctly via macro-region parameter (ADR-031)
- frozen dataclass rejects attribute mutation
- default sort=-DATE_CREATE is appended raw (RFC 3986 unreserved chars)
- custom sort=-DATE_UPDATE is substituted correctly
"""
from __future__ import annotations

import dataclasses

import pytest

from fis_monitor.infra.http.url_builder import DEFAULT_LIST_SORT, TorgiUrlBuilder

_DEFAULT_BASE = "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
_CUSTOM_BASE = "http://localhost:8765"


class TestLotListUrl:
    """TorgiUrlBuilder.lot_list_url produces correct URLs."""

    def test_default_base_url_region_1(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert url == (
            "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
            "/cabinet/free-lot"
            "?region=1&use_filter_pocket=1&sort=-DATE_CREATE"
        )

    def test_custom_base_url(self) -> None:
        builder = TorgiUrlBuilder(base_url=_CUSTOM_BASE)
        url = builder.lot_list_url(region=1)
        assert url == (
            "http://localhost:8765"
            "/cabinet/free-lot"
            "?region=1&use_filter_pocket=1&sort=-DATE_CREATE"
        )

    def test_region_param_uses_macro_id(self) -> None:
        """region= macro-param must appear; rfSubjectId must not appear (ADR-035)."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert "region=1" in url
        assert "FreeLotSearch%5BrfSubjectId%5D%5B%5D=1" not in url

    def test_use_filter_pocket_present(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert "use_filter_pocket=1" in url

    def test_default_sort_is_date_create_desc(self) -> None:
        """Default sort=DEFAULT_LIST_SORT ("-DATE_CREATE") appears raw in URL."""
        assert DEFAULT_LIST_SORT == "-DATE_CREATE"
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert "sort=-DATE_CREATE" in url

    def test_custom_sort_date_update(self) -> None:
        """Custom sort='-DATE_UPDATE' substitutes correctly."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=2, sort="-DATE_UPDATE")
        assert "sort=-DATE_UPDATE" in url
        assert "DATE_CREATE" not in url

    def test_no_rfsubjectid_param_in_url(self) -> None:
        """lot_list_url must never emit rfSubjectId params (ADR-035: fetch = macro only)."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert "rfSubjectId" not in url
        assert "FreeLotSearch" not in url

    def test_region_2_arctic(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=2)
        assert "region=2" in url


class TestLotDetailUrl:
    """TorgiUrlBuilder.lot_detail_url produces correct URLs."""

    def test_lot_detail_url_default_base(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_detail_url(lot_id=9990)
        assert url == (
            "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
            "/cabinet/free-lot-view?id=9990"
        )

    def test_lot_detail_url_custom_base(self) -> None:
        builder = TorgiUrlBuilder(base_url=_CUSTOM_BASE)
        url = builder.lot_detail_url(lot_id=42)
        assert url == "http://localhost:8765/cabinet/free-lot-view?id=42"


class TestFrozenDataclass:
    """TorgiUrlBuilder is a frozen dataclass — mutation must raise."""

    def test_frozen_raises_on_assignment(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            builder.base_url = "http://evil.example.com"  # type: ignore[misc]
