"""TorgiUrlBuilder — endpoint URL composer для надальнийвосток.рф (Punycode).

Endpoint paths и query templates — доменные константы (NOT user-configurable
per ADR-024). Если сайт меняет path → меняется парсер; единица изменения одна.
"""
from __future__ import annotations

from dataclasses import dataclass

# Module-level constants — DOMAIN, not config.
_LIST_PATH = "/cabinet/free-lot"
# Square brackets percent-encoded for safe inclusion as-is in a GET URL.
_LIST_QUERY = "?FreeLotSearch%5BrfSubjectId%5D%5B%5D={region}&use_filter_pocket=1&sort={sort}"
# Yii2 pagination parameter.  Page 1 is the default (omit for first page to
# keep URLs canonical); page 2+ appends "&FreeLotSearch_page={page}".
_LIST_PAGE_PARAM = "&FreeLotSearch_page={page}"
_DETAIL_PATH = "/cabinet/free-lot-view?id={lot_id}"

# Default sort: DESC by date_create — newest lots first (research v3 confirmed,
# docs/ops/server-performance-v3.md §H3). Minus prefix is Yii2 convention for
# DESC. "-" and "_" are RFC 3986 unreserved chars — no percent-encoding needed.
DEFAULT_LIST_SORT = "-DATE_CREATE"

# PJAX headers per docs/ops/server-performance-v3.md §H1: server returns table
# fragment (~88 KB vs 269 KB, 3x bandwidth save) when these are present. Render
# time is the same — bottleneck is upstream SQL, not HTML generation. Parser
# remains compatible: PJAX fragment contains same tr[data-key] structure.
# Shared by MonitorCycleService + FullScanService (both fetch list pages).
PJAX_HEADERS: dict[str, str] = {
    "X-PJAX": "true",
    "X-PJAX-Container": "#free-lots-pjax-container",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass(frozen=True)
class TorgiUrlBuilder:
    """Composes endpoint URLs against a configurable ``base_url``.

    Frozen value-object — recreated by composition root on Settings reload
    (currently only on restart; hot-reload is future work, see ADR-024).
    """

    base_url: str

    def lot_list_url(
        self,
        *,
        region: int,
        sort: str = DEFAULT_LIST_SORT,
        page: int = 1,
    ) -> str:
        """Return the list-page URL for *region* with optional *sort* and *page*.

        *sort* defaults to ``DEFAULT_LIST_SORT`` ("-DATE_CREATE") so the server
        returns newest lots first — a P1 correctness fix (without DESC sort,
        page 1 contains lots from 2021, not today).

        *sort* is inserted raw — Yii2 sort syntax uses only ASCII unreserved
        chars ("-", "_", letters, digits) which are RFC 3986 safe in a query.
        Callers passing values with ``&``/``=``/space must encode themselves.

        *page* selects the Yii2 pagination page (1-based).  Page 1 is
        the default; for page >= 2 the ``FreeLotSearch_page`` param is
        appended.  ``page`` MUST be a positive integer — no validation is
        performed here; callers enforce the 1..1000 guard.
        """
        base = f"{self.base_url}{_LIST_PATH}{_LIST_QUERY.format(region=region, sort=sort)}"
        if page > 1:
            base += _LIST_PAGE_PARAM.format(page=page)
        return base

    def lot_detail_url(self, *, lot_id: int) -> str:
        return f"{self.base_url}{_DETAIL_PATH.format(lot_id=lot_id)}"
