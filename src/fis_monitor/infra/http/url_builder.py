"""TorgiUrlBuilder — endpoint URL composer для надальнийвосток.рф (Punycode).

Endpoint paths и query templates — доменные константы (NOT user-configurable
per ADR-024). Если сайт меняет path → меняется парсер; единица изменения одна.
"""
from __future__ import annotations

from dataclasses import dataclass

# Module-level constants — DOMAIN, not config.
_LIST_PATH = "/cabinet/free-lot"
# Square brackets percent-encoded for safe inclusion as-is in a GET URL.
_LIST_QUERY = "?FreeLotSearch%5BrfSubjectId%5D%5B%5D={region}&use_filter_pocket=1"
_DETAIL_PATH = "/cabinet/free-lot-view?id={lot_id}"


@dataclass(frozen=True)
class TorgiUrlBuilder:
    """Composes endpoint URLs against a configurable ``base_url``.

    Frozen value-object — recreated by composition root on Settings reload
    (currently only on restart; hot-reload is future work, see ADR-024).
    """

    base_url: str

    def lot_list_url(self, *, region: int) -> str:
        return f"{self.base_url}{_LIST_PATH}{_LIST_QUERY.format(region=region)}"

    def lot_detail_url(self, *, lot_id: int) -> str:
        return f"{self.base_url}{_DETAIL_PATH.format(lot_id=lot_id)}"
