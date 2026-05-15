"""PaginatedListFetcher — iterate all pages of a lot-list region.

Fetches pages 1..N for a region until the parser returns an empty list
or ``stop_event`` is set.  Each call to ``iterate`` yields individual
``ParsedListRow`` objects so callers (BackfillService, FullScanService)
can process rows lazily without accumulating a huge list.

Design decisions:
  - Page limit of 1000 prevents infinite loops on broken pagination.
  - ``sleep_between_pages`` defaults to 2 s (rate-limit requirement) but is
    injectable so tests run at 0.0 s.
  - ``HttpClient``, ``ListParser``, and ``TorgiUrlBuilder`` are constructor-
    injected — no module-level singletons.

Errors:
  - ``UpstreamError`` and ``ParseBugError`` from a single page are logged at
    WARNING level and stop iteration for that region (conservative: partial
    data is worse than no data for deduplication purposes in BackfillService).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Protocol

from fis_monitor.domain.errors import ParseBugError, UpstreamError
from fis_monitor.domain.models import ParsedListRow
from fis_monitor.infra.http.url_builder import PJAX_HEADERS as _PJAX_HEADERS
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 1000


class _HttpClient(Protocol):
    def get(self, url: str, *, params: object = None, headers: object = None, timeout: float | None = None) -> object: ...  # noqa: E501


class _ListParser(Protocol):
    def parse(self, html: str) -> list[ParsedListRow]: ...


class PaginatedListFetcher:
    """Paginated iterator over a region's lot-list pages.

    Protocol-typed dependencies allow full substitution in unit tests:
    ``FakeHttpClient`` + ``FakeListParser`` satisfy the structural protocols
    without inheriting from them.

    Usage::

        fetcher = PaginatedListFetcher(http=..., list_parser=..., url_builder=...)
        for row in fetcher.iterate(region=77, stop_event=stop_ev):
            process(row)
    """

    def __init__(
        self,
        *,
        http: _HttpClient,
        list_parser: _ListParser,
        url_builder: TorgiUrlBuilder,
    ) -> None:
        self._http = http
        self._list_parser = list_parser
        self._url_builder = url_builder

    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float = 2.0,
    ) -> Iterator[ParsedListRow]:
        """Yield every ``ParsedListRow`` from pages 1..N for ``region``.

        *region* is a macro_id (1=ДФО, 2=Арктика).  Fetch scope is the full
        macro-region — no subject-level narrowing at the HTTP layer (ADR-035).

        Stops when:
          - the parser returns an empty list (end of catalogue), or
          - ``stop_event`` is set, or
          - a page limit of 1000 is reached (warning logged).

        A ``sleep_between_pages`` pause is inserted **between** pages
        (not before the first) to respect the upstream rate limit.

        ``UpstreamError`` or ``ParseBugError`` from any page ends iteration
        for that region — yielding partial results would corrupt BackfillService
        coverage tracking.
        """
        page = 1

        while True:
            if stop_event.is_set():
                logger.info(
                    "paginated_list_fetcher: stop_event set; aborting region=%s at page=%d",
                    region,
                    page,
                )
                return

            if page > _PAGE_LIMIT:
                logger.warning(
                    "paginated_list_fetcher: reached page limit %d for region=%s; "
                    "aborting to prevent infinite pagination",
                    _PAGE_LIMIT,
                    region,
                )
                return

            url = self._url_builder.lot_list_url(region=region, page=page)
            try:
                response = self._http.get(url, headers=_PJAX_HEADERS)
            except UpstreamError:
                logger.warning(
                    "paginated_list_fetcher: UpstreamError fetching region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                return
            except Exception:
                logger.warning(
                    "paginated_list_fetcher: unexpected error fetching "
                    "region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                return

            try:
                rows = self._list_parser.parse(response.text)  # type: ignore[union-attr]
            except ParseBugError:
                logger.warning(
                    "paginated_list_fetcher: ParseBugError on region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                return
            except Exception:
                logger.warning(
                    "paginated_list_fetcher: unexpected parse error on "
                    "region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                return

            if not rows:
                logger.debug(
                    "paginated_list_fetcher: empty page for region=%s page=%d — end of catalogue",
                    region,
                    page,
                )
                return

            logger.debug(
                "paginated_list_fetcher: region=%s page=%d yielding %d rows",
                region,
                page,
                len(rows),
            )
            yield from rows

            page += 1

            # Rate-limit: pause between pages (not after the last page).
            # Use stop_event.wait so shutdown is responsive.
            if stop_event.wait(sleep_between_pages):
                logger.info(
                    "paginated_list_fetcher: stop_event set during inter-page sleep; "
                    "aborting region=%s",
                    region,
                )
                return
