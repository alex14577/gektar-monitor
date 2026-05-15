"""Unit tests for PaginatedListFetcher.

Coverage:
  1. Stops on empty page (end of catalogue).
  2. stop_event aborts iteration mid-stream.
  3. Page limit (1000) halts infinite pagination.
  4. UpstreamError on a page stops iteration for that region.
  5. ParseBugError on a page stops iteration for that region.
  6. Multiple pages are iterated in order.
  7. sleep_between_pages=0.0 does not block tests.
"""

from __future__ import annotations

import threading
from typing import Any

from fis_monitor.domain.errors import ParseBugError, UpstreamError
from fis_monitor.domain.models import HttpResponse, ParsedListPage, ParsedListRow
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.services.paginated_list_fetcher import PaginatedListFetcher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://example.com"
_REGION = 77

_URL_BUILDER = TorgiUrlBuilder(base_url=_BASE_URL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(lot_id: int) -> ParsedListRow:
    from datetime import UTC, datetime
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return ParsedListRow(
        id=lot_id,
        cadastral_no=f"77:01:{lot_id:06d}:1",
        area_sqm=500,
        region="77",
        municipality="Тест",
        land_category="Земли населённых пунктов",
        permitted_use="ИЖС",
        ogv="ДГИ",
        status="PUBLISHED",
        date_create=now,
        date_update=now,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeHttpClient:
    """Returns pre-configured responses keyed by call index."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self._idx = 0

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        if self._idx >= len(self._responses):
            # Safety: return empty html once exhausted
            return HttpResponse(status=200, text="<empty/>", headers={}, final_url=url)
        resp = self._responses[self._idx]
        self._idx += 1
        if isinstance(resp, Exception):
            raise resp
        return HttpResponse(status=200, text=resp, headers={}, final_url=url)


class FakeListParser:
    """Returns pre-configured row lists or exceptions keyed by call index."""

    def __init__(self, page_rows: list[list[ParsedListRow] | Exception]) -> None:
        self._page_rows = page_rows
        self.calls: list[str] = []
        self._idx = 0

    def parse(self, html: str) -> ParsedListPage:
        self.calls.append(html)
        if self._idx >= len(self._page_rows):
            return ParsedListPage(rows=[], total_count=None)
        result = self._page_rows[self._idx]
        self._idx += 1
        if isinstance(result, Exception):
            raise result
        return ParsedListPage(rows=result, total_count=len(result))


def _make_fetcher(
    http: FakeHttpClient,
    parser: FakeListParser,
    url_builder: TorgiUrlBuilder = _URL_BUILDER,
) -> PaginatedListFetcher:
    return PaginatedListFetcher(http=http, list_parser=parser, url_builder=url_builder)


# ---------------------------------------------------------------------------
# Test 1: stops on empty page
# ---------------------------------------------------------------------------

class TestStopsOnEmptyPage:
    def test_two_pages_then_empty(self) -> None:
        """Iterator yields rows from page 1 and 2, then stops on empty page 3."""
        rows_p1 = [_make_row(1), _make_row(2)]
        rows_p2 = [_make_row(3)]
        rows_p3: list[ParsedListRow] = []

        parser = FakeListParser([rows_p1, rows_p2, rows_p3])
        http = FakeHttpClient(["<p1/>", "<p2/>", "<p3/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert [r.id for r in result] == [1, 2, 3]
        # 3 HTTP calls: page 1, 2, 3 (empty stops before page 4)
        assert len(http.calls) == 3

    def test_single_empty_page_yields_nothing(self) -> None:
        """Empty first page → no rows, single HTTP call."""
        parser = FakeListParser([[]])
        http = FakeHttpClient(["<empty/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert result == []
        assert len(http.calls) == 1


# ---------------------------------------------------------------------------
# Test 2: stop_event aborts iteration
# ---------------------------------------------------------------------------

class TestStopEventAborts:
    def test_stop_before_first_page(self) -> None:
        """Pre-set stop_event → no HTTP calls, no rows."""
        parser = FakeListParser([[_make_row(1)]])
        http = FakeHttpClient(["<p1/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()
        stop.set()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert result == []
        assert len(http.calls) == 0

    def test_stop_after_first_page(self) -> None:
        """stop_event set after consuming first page's rows — second page not fetched."""
        rows_p1 = [_make_row(10), _make_row(20)]
        parser = FakeListParser([rows_p1, [_make_row(30)]])
        http = FakeHttpClient(["<p1/>", "<p2/>"])

        stop = threading.Event()
        fetcher = _make_fetcher(http, parser)

        collected = []
        for row in fetcher.iterate(_REGION, stop, sleep_between_pages=0.0):
            collected.append(row.id)
            if row.id == 20:
                stop.set()

        # Only page 1 rows; stop fires during the inter-page sleep check.
        assert 10 in collected
        assert 20 in collected
        # Page 2 was not fetched because stop was set before the sleep returned.
        assert 30 not in collected


# ---------------------------------------------------------------------------
# Test 3: page limit (1000)
# ---------------------------------------------------------------------------

class TestPageLimit:
    def test_stops_at_page_limit(self) -> None:
        """When pages never return empty, iteration stops at 1000 pages."""
        # Simulate infinite pages by making the parser always return one row.
        class InfiniteParser:
            def __init__(self) -> None:
                self.call_count = 0

            def parse(self, html: str) -> ParsedListPage:
                self.call_count += 1
                rows = [_make_row(self.call_count)]
                return ParsedListPage(rows=rows, total_count=len(rows))

        class InfiniteHttp:
            def __init__(self) -> None:
                self.call_count = 0

            def get(
                self,
                url: str,
                *,
                params: Any = None,
                headers: Any = None,
                timeout: float | None = None,
            ) -> HttpResponse:
                self.call_count += 1
                return HttpResponse(status=200, text="<html/>", headers={}, final_url=url)

        http = InfiniteHttp()
        parser = InfiniteParser()
        stop = threading.Event()

        fetcher = PaginatedListFetcher(http=http, list_parser=parser, url_builder=_URL_BUILDER)
        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        # 1000 pages, one row each
        assert len(result) == 1000
        assert http.call_count == 1000


# ---------------------------------------------------------------------------
# Test 4: UpstreamError stops iteration
# ---------------------------------------------------------------------------

class TestUpstreamError:
    def test_upstream_error_stops_region(self) -> None:
        """UpstreamError on page 2 stops iteration; page 1 rows were already yielded."""
        rows_p1 = [_make_row(1)]
        http = FakeHttpClient(["<p1/>", UpstreamError("timeout", category="timeout")])
        parser = FakeListParser([rows_p1])  # only one parse call (page 1)

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert [r.id for r in result] == [1]
        assert len(http.calls) == 2  # page 1 ok, page 2 raises


# ---------------------------------------------------------------------------
# Test 5: ParseBugError stops iteration
# ---------------------------------------------------------------------------

class TestParseBugError:
    def test_parse_error_stops_region(self) -> None:
        """ParseBugError on page 1 stops iteration immediately — no rows."""
        http = FakeHttpClient(["<p1/>"])
        parser = FakeListParser([ParseBugError(selector="tr", context="test")])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert result == []
        assert len(http.calls) == 1


# ---------------------------------------------------------------------------
# Test 6: page URL contains page parameter for page >= 2
# ---------------------------------------------------------------------------

class TestPageUrls:
    def test_page1_url_has_no_page_param(self) -> None:
        """Page 1 URL does NOT include FreeLotSearch_page."""
        http = FakeHttpClient(["<p1/>"])
        parser = FakeListParser([[]])  # empty → stops after page 1

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()
        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert "FreeLotSearch_page" not in http.calls[0]

    def test_page2_url_has_page_param(self) -> None:
        """Page 2 URL includes FreeLotSearch_page=2."""
        http = FakeHttpClient(["<p1/>", "<p2/>"])
        parser = FakeListParser([[_make_row(1)], []])  # page 1 has rows, page 2 empty

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()
        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))

        assert len(http.calls) == 2
        assert "FreeLotSearch_page=2" in http.calls[1]


# ---------------------------------------------------------------------------
# Test 7: max_pages kwarg (ADR-036 head-poll)
# ---------------------------------------------------------------------------

class TestMaxPages:
    def test_iterate_respects_max_pages(self) -> None:
        """With max_pages=1, only page 1 is fetched even when more pages exist."""
        rows_p1 = [_make_row(1), _make_row(2)]
        rows_p2 = [_make_row(3)]
        rows_p3 = [_make_row(4)]

        parser = FakeListParser([rows_p1, rows_p2, rows_p3])
        http = FakeHttpClient(["<p1/>", "<p2/>", "<p3/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, max_pages=1))

        assert [r.id for r in result] == [1, 2]
        assert len(http.calls) == 1  # only page 1 fetched

    def test_iterate_unbounded_when_max_pages_none(self) -> None:
        """max_pages=None (default) walks all pages until empty — existing behaviour."""
        rows_p1 = [_make_row(1)]
        rows_p2 = [_make_row(2)]
        rows_p3: list[ParsedListRow] = []

        parser = FakeListParser([rows_p1, rows_p2, rows_p3])
        http = FakeHttpClient(["<p1/>", "<p2/>", "<p3/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, max_pages=None))

        assert [r.id for r in result] == [1, 2]
        assert len(http.calls) == 3  # walked to empty page


# ---------------------------------------------------------------------------
# Test 8: per_page forwarded to URL builder (ADR-036)
# ---------------------------------------------------------------------------

class TestPerPageForwarding:
    def test_iterate_forwards_per_page_to_url(self) -> None:
        """per_page=20 appears as 'per-page=20' in the constructed URL."""
        http = FakeHttpClient(["<p1/>"])
        parser = FakeListParser([[]])  # empty first page → stops

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()
        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, per_page=20))

        assert len(http.calls) == 1
        assert "per-page=20" in http.calls[0]

    def test_iterate_no_per_page_when_none(self) -> None:
        """per_page=None (default) omits the per-page query param entirely."""
        http = FakeHttpClient(["<p1/>"])
        parser = FakeListParser([[]])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()
        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, per_page=None))

        assert "per-page" not in http.calls[0]


# ---------------------------------------------------------------------------
# Test 9: page_callback — called with correct page_num and items_count
# ---------------------------------------------------------------------------

class TestPageCallback:
    def test_callback_called_per_page_with_correct_args(self) -> None:
        """page_callback receives (page_num, items_count) for each non-empty page."""
        rows_p1 = [_make_row(1), _make_row(2)]
        rows_p2 = [_make_row(3)]
        rows_p3: list[ParsedListRow] = []

        parser = FakeListParser([rows_p1, rows_p2, rows_p3])
        http = FakeHttpClient(["<p1/>", "<p2/>", "<p3/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        recorded: list[tuple[int, int]] = []

        def _cb(p: int, n: int) -> None:
            recorded.append((p, n))

        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, page_callback=_cb))

        # page 1: 2 rows, page 2: 1 row; empty page 3 never triggers callback
        assert recorded == [(1, 2), (2, 1)]

    def test_callback_not_called_on_empty_first_page(self) -> None:
        """page_callback is NOT invoked when first page is empty (no rows to yield)."""
        parser = FakeListParser([[]])
        http = FakeHttpClient(["<empty/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        recorded: list[tuple[int, int]] = []

        def _cb2(p: int, n: int) -> None:
            recorded.append((p, n))

        list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0, page_callback=_cb2))

        assert recorded == []

    def test_callback_none_by_default(self) -> None:
        """Omitting page_callback (default None) does not raise — existing callers unaffected."""
        rows_p1 = [_make_row(1)]
        parser = FakeListParser([rows_p1, []])
        http = FakeHttpClient(["<p1/>", "<p2/>"])

        fetcher = _make_fetcher(http, parser)
        stop = threading.Event()

        # No page_callback argument — must not raise
        result = list(fetcher.iterate(_REGION, stop, sleep_between_pages=0.0))
        assert [r.id for r in result] == [1]
