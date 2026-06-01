"""Layer 4 integration: lot_count in build_feed_context + _feed_lots template (ddpf+hke7).

Invariants covered:
  (1) build_feed_context returns lot_count = lot_query.count(lot_filters).
  (1) count ≠ rendered page size when total > page_size (counter is true total).
  Template: #feed-lot-count displays lot_count, not len(zones.today).

docs/architecture/09-test-strategy.md Layer 4:
  Integration: TestClient + fake infra (no real SQLite for build_feed_context).
  Jinja rendered via real build_templates() or direct env.get_template.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fis_monitor.domain.models import Settings
from fis_monitor.services.lot_query import LotFilters, Page
from fis_monitor.services.view_filters import ViewFilters
from fis_monitor.web.feed_context import build_feed_context
from fis_monitor.web.templates import build_templates

# ---------------------------------------------------------------------------
# Fake LotQueryService with controlled count() return
# ---------------------------------------------------------------------------


class _FakeLotQueryService:
    """Fake with separate controls for search items (page-capped) and count (true total)."""

    def __init__(
        self,
        items: tuple,
        *,
        true_total: int,
    ) -> None:
        self._items = items
        self._true_total = true_total

    def search(
        self,
        filters: LotFilters,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> Page:
        return Page(items=self._items[:page_size], next_cursor=None, has_more=False)

    def count(self, filters: LotFilters) -> int:
        return self._true_total

    def get_by_id(self, lot_id: int) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_feed_context_returns_lot_count() -> None:
    """Invariant (1): build_feed_context['lot_count'] == lot_query.count(...)."""
    fake = _FakeLotQueryService(items=(), true_total=42)
    ctx = build_feed_context(
        filters=ViewFilters(),
        lot_query=fake,  # type: ignore[arg-type]
        settings=Settings(),
        active_lot_count=0,
    )
    assert ctx["lot_count"] == 42


def test_build_feed_context_lot_count_exceeds_page_size() -> None:
    """Invariant (1): lot_count can exceed _FEED_PAGE_SIZE (200) — it's the true total."""
    from fis_monitor.web.feed_context import _FEED_PAGE_SIZE

    # Simulate 350 total lots but only page_size items returned by search.
    true_total = _FEED_PAGE_SIZE + 150  # 350
    fake = _FakeLotQueryService(items=(), true_total=true_total)
    ctx = build_feed_context(
        filters=ViewFilters(),
        lot_query=fake,  # type: ignore[arg-type]
        settings=Settings(),
        active_lot_count=true_total,
    )
    assert ctx["lot_count"] == true_total, (
        f"lot_count must equal true_total={true_total}, got {ctx['lot_count']}"
    )


def test_feed_lots_template_renders_lot_count_not_page_size() -> None:
    """Template renders lot_count in #feed-lot-count, not len(zones.today).

    This ensures the filter-bar counter shows the true total even when
    zones.today is capped at _FEED_PAGE_SIZE.
    """
    templates = build_templates()
    env = templates.env

    from datetime import UTC, datetime, timedelta

    from fis_monitor.domain.models import LotUserDTO
    from fis_monitor.web.sse_encoder import LotUserViewModel
    from tests.factories import make_lot

    now = datetime(2026, 1, 1, tzinfo=UTC)
    # 3 lots in zones.today (page-capped), but true total is 350
    lot_dtos = [
        LotUserViewModel(
            LotUserDTO(
                **make_lot(id=i, date_create=now + timedelta(seconds=i)).model_dump(),
                age_seconds=60,
                tier="match",
                freshness="hot",
            )
        )
        for i in range(1, 4)
    ]
    true_total = 350

    filters_ctx = SimpleNamespace(
        subjects=[],
        area_min="",
        area_max="",
        area_min_label="0",
        area_max_label="∞",
        only_new=False,
    )
    tmpl = env.get_template("partials/_feed_lots.html.jinja")
    html = tmpl.render(
        filters=filters_ctx,
        scope=SimpleNamespace(subjects_count=19),
        zones=SimpleNamespace(hot=(), today=tuple(lot_dtos)),
        archive_count=0,
        next_cursor=None,
        lot_count=true_total,
        filters_active=False,
        health=SimpleNamespace(total_lots=true_total),
        session=SimpleNamespace(expired=False, expires_soon=False),
    )

    # Counter must show true_total, not len(zones.today)=3
    assert "350 лотов" in html or "350 лота" in html or "350 лот" in html, (
        f"Expected '350 лот...' in counter, not found. "
        f"Check #feed-lot-count. snippet: {html[:500]}"
    )
    assert "3 лота" not in html or "350" in html, (
        "Template must render lot_count (350), not len(zones.today) (3)"
    )
    assert 'id="feed-lot-count"' in html
    assert 'class="filter-bar__count js-lot-count"' in html


def test_feed_lot_count_filter_bar_renders_at_zero() -> None:
    """Fix #2: filter-bar #feed-lot-count is always in DOM even when lot_count=0.

    Before fix the element was gated by {%% if lot_count %%} and absent at
    lot_count=0, so the first SSE lot.new could not find .js-lot-count and the
    counter silently stayed invisible until page reload.
    """
    templates = build_templates()
    env = templates.env
    tmpl = env.get_template("partials/_feed_lots.html.jinja")

    from types import SimpleNamespace

    html = tmpl.render(
        filters=SimpleNamespace(
            subjects=[],
            area_min="",
            area_max="",
            area_min_label="0",
            area_max_label="∞",
            only_new=False,
        ),
        scope=SimpleNamespace(subjects_count=19),
        zones=SimpleNamespace(hot=(), today=()),
        archive_count=0,
        next_cursor=None,
        lot_count=0,
        filters_active=False,
        health=SimpleNamespace(total_lots=0),
        session=SimpleNamespace(expired=False, expires_soon=False),
    )

    assert 'id="feed-lot-count"' in html, (
        "#feed-lot-count must be in DOM even when lot_count=0 so JS can increment it"
    )
    assert 'data-count="0"' in html, (
        "data-count must be 0 so CSS hides the counter and JS knows the base value"
    )
