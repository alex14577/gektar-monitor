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
from collections.abc import Callable, Iterator
from typing import Protocol

from fis_monitor.domain.errors import ParseBugError, SessionExpiredError, UpstreamError
from fis_monitor.domain.interfaces import HttpClient
from fis_monitor.domain.models import ParsedListPage, ParsedListRow
from fis_monitor.infra.http.url_builder import PJAX_HEADERS as _PJAX_HEADERS
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 1000


class _ListParser(Protocol):
    def parse(self, html: str) -> ParsedListPage: ...


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
        http: HttpClient,
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
        per_page: int | None = None,
        max_pages: int | None = None,
        page_start: int = 1,
        page_callback: Callable[[int, int], None] | None = None,
        total_callback: Callable[[int], None] | None = None,
        raise_on_network_error: bool = False,
        raise_on_parse_error: bool = False,
    ) -> Iterator[ParsedListRow]:
        """Yield every ``ParsedListRow`` from pages 1..N for ``region``.

        *region* is a macro_id (1=ДФО, 2=Арктика).  Fetch scope is the full
        macro-region — no subject-level narrowing at the HTTP layer (ADR-035).

        Stops when:
          - the parser returns an empty list (end of catalogue), or
          - ``stop_event`` is set, or
          - ``max_pages`` pages have been fetched (ADR-036: head-poll uses 1), or
          - a page limit of 1000 is reached (warning logged).

        *per_page* is forwarded to ``lot_list_url`` as the Yii2 ``per-page``
        query parameter.  ``None`` uses the site default (≈50 rows).
        ADR-036: FullScan and Backfill call this with ``per_page=50``,
        ``max_pages=None`` (unbounded full walk). MonitorCycle does NOT use
        ``iterate`` — it issues a single ``lot_list_url(per_page=20)`` request
        directly. ``max_pages`` therefore caps generic callers; it is not the
        mechanism that enforces head-poll.

        A ``sleep_between_pages`` pause is inserted **between** pages
        (not before the first) to respect the upstream rate limit.

        ``UpstreamError`` or ``ParseBugError`` from any page ends iteration
        for that region via ``return`` (partial rows already yielded stay with
        the caller).  ``SessionExpiredError`` is re-raised instead so callers
        (BackfillService, FullScanService) can publish ``SseSessionExpired``.
        Yielding partial results would corrupt BackfillService coverage tracking.

        *page_callback*, if supplied, is called **before** yielding the first
        row of each page as ``page_callback(page_num, items_count)``.  This
        lets callers (e.g. ``BackfillService``) track the current page without
        maintaining a parallel counter.  The callback runs synchronously in the
        iterator's thread; it must not block.
        """
        page = page_start if page_start >= 1 else 1
        pages_walked = 0

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

            url = self._url_builder.lot_list_url(region=region, page=page, per_page=per_page)
            try:
                response = self._http.get(url, headers=_PJAX_HEADERS)
            except UpstreamError:
                logger.warning(
                    "paginated_list_fetcher: UpstreamError fetching region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                if raise_on_network_error:
                    raise
                return
            except Exception:
                logger.warning(
                    "paginated_list_fetcher: unexpected error fetching "
                    "region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                if raise_on_network_error:
                    raise
                return

            try:
                parsed_page = self._list_parser.parse(response.text)
                rows = parsed_page.rows
                if total_callback is not None and parsed_page.total_count is not None:
                    total_callback(parsed_page.total_count)
            except SessionExpiredError:
                logger.warning(
                    "paginated_list_fetcher: SessionExpiredError on region=%s page=%d"
                    " — propagating",
                    region,
                    page,
                )
                raise
            except ParseBugError:
                logger.warning(
                    "paginated_list_fetcher: ParseBugError on region=%s page=%d — stopping",
                    region,
                    page,
                    exc_info=True,
                )
                logger.warning(
                    "paginated_list_fetcher: ParseBugError response diagnostics:"
                    " status=%s final_url=%s text_len=%d text_head=%r",
                    response.status,
                    response.final_url,
                    len(response.text),
                    response.text[:2000],
                )
                if raise_on_parse_error:
                    raise
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
            if page_callback is not None:
                page_callback(page, len(rows))
            yield from rows

            pages_walked += 1
            # ADR-036: max_pages caps the walk for bounded scan; None = unbounded.
            if max_pages is not None and pages_walked >= max_pages:
                return

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
