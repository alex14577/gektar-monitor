from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from fis_monitor.domain.interfaces import Lot
from fis_monitor.domain.models import LotUpsertResult, TrackedField


class FakeLotRepository:
    """Canonical in-memory fake for LotRepository Protocol.

    See ADR-041 §Fake signature canon — single fake per Protocol.
    """

    def __init__(self, count_active_value: int = 0) -> None:
        self._count_active_value = count_active_value
        self.count_active_calls: list[int | None] = []
        self._lots: dict[int, Lot] = {}
        self._last_known_ids: dict[int, int] = {}

    def upsert(self, lot: Lot, *, tracked: Sequence[TrackedField]) -> LotUpsertResult:
        self._lots[lot.id] = lot
        return LotUpsertResult(was_new=False, changes=[])

    def get(self, lot_id: int) -> Lot | None:
        return self._lots.get(lot_id)

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        return []

    def get_last_known_id(self, region: int) -> int | None:
        return self._last_known_ids.get(region)

    def set_last_known_id(self, region: int, value: int) -> None:
        self._last_known_ids[region] = value

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        pass

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        pass

    def needing_enrichment(self, limit: int) -> list[int]:
        return []

    def count_active(self, region_id: int | None = None) -> int:
        self.count_active_calls.append(region_id)
        return self._count_active_value
