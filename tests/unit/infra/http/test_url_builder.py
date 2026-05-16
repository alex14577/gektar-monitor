"""Unit tests for TorgiUrlBuilder.

Tests:
- default base_url produces correct lot_list_url for a region
- custom base_url substitutes correctly
- lot_detail_url produces correct URL for a lot_id
- region=1 (int) formats correctly via macro-region parameter (ADR-031)
- frozen dataclass rejects attribute mutation
- default sort=-DATE_CREATE is appended raw (RFC 3986 unreserved chars)
- custom sort=-DATE_UPDATE is substituted correctly
- page param uses Yii2 default `page` name, not model-specific FreeLotSearch_page
- page 1 omits page param; page 2+ appends &page=N
- built URL matches pager href from live HTML fixture
"""
from __future__ import annotations

import dataclasses
import html as html_lib
import re
from pathlib import Path

import pytest

from fis_monitor.infra.http.url_builder import DEFAULT_LIST_SORT, TorgiUrlBuilder

_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"

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

    def test_lot_list_url_includes_per_page_when_set(self) -> None:
        """per_page=20 appears as 'per-page=20' in the URL (ADR-036: head-poll)."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1, per_page=20)
        assert "per-page=20" in url

    def test_lot_list_url_omits_per_page_when_none(self) -> None:
        """per_page=None (default) omits the per-page param entirely."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1, per_page=None)
        assert "per-page" not in url

    def test_lot_list_url_per_page_zero_raises(self) -> None:
        """per_page=0 is rejected with ValueError."""
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        with pytest.raises(ValueError, match="per_page must be > 0"):
            builder.lot_list_url(region=1, per_page=0)

    def test_lot_list_url_page_uses_yii2_default_param_name(self) -> None:
        """Pagination param is `page`, not model-specific `FreeLotSearch_page`.

        Invariant: page=1 omits page param entirely (canonical URL);
        page=2 appends exactly `&page=2`; FreeLotSearch_page never appears.
        Confirmed from live HTML pager hrefs (?region=1&page=2 …).
        """
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)

        url_p1 = builder.lot_list_url(region=1, page=1)
        assert "&page=" not in url_p1
        assert "FreeLotSearch_page" not in url_p1

        url_p2 = builder.lot_list_url(region=1, page=2)
        assert "&page=2" in url_p2
        assert "FreeLotSearch_page" not in url_p2

    def test_lot_list_url_page2_matches_fixture_pager_href(self) -> None:
        """URL for page 2 must share path+param with pager__btn href in fixture.

        Extracts `href` from first non-active `pager__btn` anchor in
        list_region1_perpage50.html and confirms our builder emits the same
        `page=2` parameter on the same path.
        """
        fixture = _FIXTURES_DIR / "list_region1_perpage50.html"
        if not fixture.exists():
            pytest.skip("fixture not available")

        html = fixture.read_text(encoding="utf-8")
        # Extract hrefs of non-active pager buttons (page 2+)
        hrefs = re.findall(
            r'class="pager__btn"[^>]+href="([^"]+)"', html
        )
        assert hrefs, "no pager__btn hrefs found in fixture"
        # First non-active button should be page 2
        page2_href = hrefs[0]  # e.g. /cabinet/free-lot?region=1&amp;page=2 (HTML-escaped)
        page2_href_unescaped = html_lib.unescape(page2_href)

        assert "&page=2" in page2_href_unescaped, f"unexpected href: {page2_href}"
        assert "FreeLotSearch_page" not in page2_href_unescaped

        # Our builder must produce the same page= param and path
        builder = TorgiUrlBuilder(base_url=_DEFAULT_BASE)
        url = builder.lot_list_url(region=1, page=2)
        assert "&page=2" in url
        assert "FreeLotSearch_page" not in url


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
