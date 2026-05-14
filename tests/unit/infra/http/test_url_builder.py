"""Unit tests for TorgiUrlBuilder.

Tests:
- default base_url produces correct lot_list_url for a region
- custom base_url substitutes correctly
- lot_detail_url produces correct URL for a lot_id
- region=1 (int) formats correctly (not "[1]" etc.)
- frozen dataclass rejects attribute mutation
"""
from __future__ import annotations

import dataclasses

import pytest

from fis_monitor.infra.http.url_builder import TorgiUrlBuilder

_DEFAULT_BASE = "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
_CUSTOM_BASE = "http://localhost:8765"


class TestLotListUrl:
    """TorgiUrlBuilder.lot_list_url produces correct percent-encoded URLs."""

    def test_default_base_url_region_27(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=27)
        assert url == (
            "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
            "/cabinet/free-lot"
            "?FreeLotSearch%5BrfSubjectId%5D%5B%5D=27&use_filter_pocket=1"
        )

    def test_custom_base_url(self) -> None:
        builder = TorgiUrlBuilder(base_url=_CUSTOM_BASE)
        url = builder.lot_list_url(region=27)
        assert url == (
            "http://localhost:8765"
            "/cabinet/free-lot"
            "?FreeLotSearch%5BrfSubjectId%5D%5B%5D=27&use_filter_pocket=1"
        )

    def test_region_1_formats_as_scalar(self) -> None:
        """region=1 must appear as '1', not '[1]' or 'region=1&...' etc."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1)
        assert "FreeLotSearch%5BrfSubjectId%5D%5B%5D=1" in url
        assert "[1]" not in url
        assert "region=1" not in url  # no old-style key

    def test_use_filter_pocket_present(self) -> None:
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=27)
        assert "use_filter_pocket=1" in url


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
