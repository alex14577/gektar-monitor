"""Shared fixtures and fakes for web route unit tests.

Extracted from test_main.py and test_filters.py to eliminate duplication
(orchestrator rule: dedup fakes, playbook #6 — all Protocol methods covered).
"""

from __future__ import annotations

from typing import Any

import pytest

from fis_monitor.domain.models import LotUserDTO, Settings
from fis_monitor.services.lot_query import LotFilters, Page

# ---------------------------------------------------------------------------
# Shared fake implementations
# ---------------------------------------------------------------------------


class FakeLotQueryService:
    """Duck-typed fake for ``LotQueryService``.

    Records ``LotFilters`` passed to ``search()`` (accessible via
    ``search_calls``) and returns a preset page of ``LotUserDTO`` items so
    callers get template-renderable data without a real database.

    ``get_by_id`` raises ``NotImplementedError`` — regression guard: if a
    caller unexpectedly starts using it in tests the failure is immediate.
    """

    def __init__(self, items: tuple[LotUserDTO, ...] = ()) -> None:
        self._items = items
        self.search_calls: list[LotFilters] = []

    def search(
        self,
        filters: LotFilters,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> Page[LotUserDTO]:
        self.search_calls.append(filters)
        return Page(items=self._items, next_cursor=None, has_more=False)

    def count(self, filters: LotFilters) -> int:
        """Return total matching items (same as len(search result) for this fake)."""
        return len(self._items)

    def get_by_id(self, lot_id: int) -> Any:
        raise NotImplementedError


class FakeLotRepo:
    """Duck-typed fake for ``LotRepository``.

    Only ``count_active()`` is exercised by the feed and filter routes.
    Other Protocol methods raise ``NotImplementedError`` so a regression that
    starts touching them fails loud.

    ``count_active`` accepts an optional ``region_id`` param matching the
    Protocol signature; the fake ignores it and returns the configured count.
    """

    def __init__(self, active_count: int = 0) -> None:
        self._active_count = active_count
        self.count_active_calls: int = 0

    def count_active(self, region_ids: tuple[int, ...] = ()) -> int:
        self.count_active_calls += 1
        return self._active_count

    def latest_new_first_seen(self) -> Any:
        """bd 47uh: header-status VM probes this. None = empty DB → "—"."""
        return None

    def get(self, lot_id: int) -> Any:
        raise NotImplementedError

    def upsert(self, lot: Any, *, tracked: Any) -> Any:
        raise NotImplementedError

    def mark_inactive(self, lot_id: int, reason: str, at: Any) -> None:
        raise NotImplementedError


class FakeConfigSource:
    """Minimal fake config source for route tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: object) -> object:
        return object()

    def save(self, settings: Settings) -> None:
        self._settings = settings


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_lot_query() -> FakeLotQueryService:
    """Return a fresh ``FakeLotQueryService`` with no items."""
    return FakeLotQueryService()


@pytest.fixture()
def fake_lot_repo() -> FakeLotRepo:
    """Return a fresh ``FakeLotRepo`` with zero active lots."""
    return FakeLotRepo()
