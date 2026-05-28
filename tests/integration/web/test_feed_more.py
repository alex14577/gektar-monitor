"""Integration tests for GET /feed/more (Layer 4 — TestClient + fake infra).

Invariants covered (per test-strategy Layer 4 + task spec):
  T1 Walk all pages: successive /feed/more cursors on a >page_size dataset
     → every active lot appears exactly once (no dup, no gap).
  T2 Filters preserved: region filter in cookie → every page returns only matching lots.
  T3 Exhaustion: dataset ≤ one page → no #load-more-trigger rendered on initial feed.
  T4 Invalid cursor → 422.
  T5 only_new preserved across pages: unseen lots appear, seen lots are excluded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fis_monitor.web.routes.main as _main_module
from fis_monitor.domain.models import LotUserDTO, Settings
from fis_monitor.services.lot_query import LotFilters, Page
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import (
    get_lot_query,
    get_templates,
    get_view_filters_service,
)
from fis_monitor.web.routes.main import router
from fis_monitor.web.templates import build_templates
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COOKIE_NAME = "view_filters"
# Small page size used in pagination tests so we can cross real page boundaries
# with a handful of fixtures.  Tests monkeypatch the route module constant to
# this value so the route itself uses the small size.
_TEST_PAGE_SIZE = 3


# ---------------------------------------------------------------------------
# Helpers: build LotUserDTO test fixtures
# ---------------------------------------------------------------------------


def _make_dto(
    lot_id: int,
    *,
    region: str = "Хабаровский край",
    date_create: datetime | None = None,
    seen_at: datetime | None = None,
    was_new: bool = False,
) -> LotUserDTO:
    """Build a LotUserDTO with controlled fields for pagination tests."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    dc = date_create or (base + timedelta(seconds=lot_id))
    lot = make_lot(
        id=lot_id,
        region=region,
        date_create=dc,
        date_update=dc,
        first_seen=dc,
        last_seen=dc,
        last_seen_at=dc,
    )
    return LotUserDTO(
        **lot.model_dump(),
        age_seconds=100,
        tier="match",
        freshness="hot",
        seen_at=seen_at,
        was_new=was_new,
    )


# ---------------------------------------------------------------------------
# Fake LotQueryService with real cursor support
# ---------------------------------------------------------------------------


class _PagedFakeLotQueryService:
    """Fake LotQueryService that paginates an in-memory list.

    Supports cursor-based pagination by decoding the cursor and slicing the
    pre-sorted item list.  Uses the same cursor encode/decode logic as the
    real service so tests exercise the actual cursor contract.

    Supports cursor-based pagination; sort order is hardcoded DESC (newest first).
    """

    def __init__(
        self,
        items: list[LotUserDTO],
    ) -> None:
        # Store items; sort order is determined per search() call.
        self._items = items
        self.search_calls: list[tuple[LotFilters, int, str | None]] = []

    def search(
        self,
        filters: LotFilters,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> Page[LotUserDTO]:
        from fis_monitor.services.lot_query import _decode_cursor, _encode_cursor

        self.search_calls.append((filters, page_size, cursor))

        # Sort items by (date_create, id) DESC (hardcoded newest first).
        sorted_items = sorted(
            self._items,
            key=lambda d: (d.date_create, d.id),
            reverse=True,
        )

        # Apply region filter (subject_display_names).
        if filters.subject_display_names:
            sorted_items = [d for d in sorted_items if d.region in filters.subject_display_names]

        # Apply cursor: skip items already returned (DESC order hardcoded).
        if cursor is not None:
            date_iso, last_id = _decode_cursor(cursor)
            last_dt = datetime.fromisoformat(date_iso)
            sorted_items = [
                dto
                for dto in sorted_items
                if dto.date_create < last_dt or (dto.date_create == last_dt and dto.id < last_id)
            ]

        has_more = len(sorted_items) > page_size
        page_items = sorted_items[:page_size]

        next_cursor = None
        if has_more and page_items:
            last = page_items[-1]
            next_cursor = _encode_cursor(last.date_create, last.id)

        return Page(
            items=tuple(page_items),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_by_id(self, lot_id: int) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    lot_query: _PagedFakeLotQueryService,
    *,
    svc: ViewFiltersService | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with only the main router and real templates."""
    templates = build_templates()
    used_svc = svc or ViewFiltersService()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_lot_query] = lambda: lot_query
    app.dependency_overrides[get_view_filters_service] = lambda: used_svc
    return app


def _view_filters_cookie(filters: ViewFilters) -> str:
    """Serialise ViewFilters into cookie value (percent-encoded JSON)."""
    return ViewFiltersService().serialize(filters)


# ---------------------------------------------------------------------------
# T1: Walk all pages — no dup, no gap
# ---------------------------------------------------------------------------


class TestWalkAllPages:
    """T1: successive /feed/more cursors walk the full dataset exactly once.

    The route module constant ``_FEED_PAGE_SIZE`` is monkeypatched to
    ``_TEST_PAGE_SIZE`` (3) so crossing real page boundaries is possible with a
    small fixture set.  10 lots at page_size=3 → 3 full /feed/more pages + 1
    lot on the last page = 4 pages total (≥3 pages required).
    """

    def test_all_lots_appear_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """10 lots, route page_size=3 → walk all /feed/more pages, each lot id once."""
        # Force the route to use a small page size so page boundaries are real.
        monkeypatch.setattr(_main_module, "_FEED_PAGE_SIZE", _TEST_PAGE_SIZE)

        # 10 lots with unique date_create offsets so keyset cursor is unambiguous.
        lots = [_make_dto(i) for i in range(1, 11)]
        lq = _PagedFakeLotQueryService(lots)
        app = _build_app(lq)

        collected_ids: list[int] = []
        pages_walked = 0

        with TestClient(app, raise_server_exceptions=True) as client:
            # Simulate what the browser does after GET /: obtain the first cursor
            # by calling search directly with the same filters the route will use.
            initial_page = lq.search(LotFilters(), page_size=_TEST_PAGE_SIZE)
            for dto in initial_page.items:
                collected_ids.append(dto.id)

            cursor = initial_page.next_cursor
            shown = len(initial_page.items)
            assert cursor, "Expected a cursor on the initial page (10 lots, page_size=3)"

            while cursor:
                resp = client.get(
                    f"/feed/more?cursor={cursor}&shown={shown}",
                    cookies={_COOKIE_NAME: _view_filters_cookie(ViewFilters())},
                )
                assert resp.status_code == 200, resp.text
                body = resp.text
                pages_walked += 1

                found = re.findall(r'id="lot-(\d+)"', body)
                collected_ids.extend(int(lid) for lid in found)
                shown += len(found)

                # Extract next cursor — URL-encoded, so decode %3D→= etc.
                m = re.search(r'hx-get="/feed/more\?cursor=([^&"]+)', body)
                cursor = m.group(1) if m else None
                if cursor:
                    from urllib.parse import unquote_plus

                    cursor = unquote_plus(cursor)

        assert pages_walked >= 3, f"Expected ≥3 /feed/more pages, walked only {pages_walked}"
        assert sorted(collected_ids) == list(range(1, 11)), (
            f"Expected all ids 1-10, got {sorted(collected_ids)}"
        )
        assert len(collected_ids) == len(set(collected_ids)), (
            f"Duplicate ids: {[x for x in collected_ids if collected_ids.count(x) > 1]}"
        )

    def test_next_cursor_absent_on_last_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Last page response must NOT contain #load-more-trigger."""
        monkeypatch.setattr(_main_module, "_FEED_PAGE_SIZE", _TEST_PAGE_SIZE)

        lots = [_make_dto(i) for i in range(1, 5)]  # 4 lots, page_size=3
        lq = _PagedFakeLotQueryService(lots)
        app = _build_app(lq)

        # Get first-page cursor directly.
        first_page = lq.search(LotFilters(), page_size=_TEST_PAGE_SIZE)
        cursor = first_page.next_cursor
        assert cursor, "Expected a next_cursor on first page of 4 lots with page_size=3"

        with TestClient(app) as client:
            resp = client.get(f"/feed/more?cursor={cursor}")
        assert resp.status_code == 200
        assert "load-more-trigger" not in resp.text, "Last page must not render #load-more-trigger"


# ---------------------------------------------------------------------------
# T2: Filters preserved across pages
# ---------------------------------------------------------------------------


class TestFiltersPreserved:
    """T2: region filter in cookie → every load-more page returns only matching lots."""

    def test_region_filter_applied_on_load_more(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cookie with subject filter → /feed/more returns only matching region lots."""
        from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

        # Pick a real subject ID (27 → Республика Карелия).
        subject_id = 27
        subject_name = SUBJECT_TITLE_BY_ID[subject_id]

        # 5 matching lots at page_size=3 guarantees next_cursor (5 > 3).
        matching = [_make_dto(i, region=subject_name) for i in range(1, 6)]
        non_matching = [_make_dto(100 + i, region="Другой регион") for i in range(3)]
        all_lots = matching + non_matching

        lq = _PagedFakeLotQueryService(all_lots)
        filters = ViewFilters(subjects=[str(subject_id)])
        cookie_val = _view_filters_cookie(filters)

        app = _build_app(lq)

        # Build initial cursor using the SAME filter context the /feed/more call will
        # use: subject_display_names translated from the cookie, page_size=_TEST_PAGE_SIZE
        # (matching the monkeypatched route constant).
        monkeypatch.setattr(_main_module, "_FEED_PAGE_SIZE", _TEST_PAGE_SIZE)
        first_page = lq.search(
            LotFilters(subject_display_names=(subject_name,)),
            page_size=_TEST_PAGE_SIZE,
        )
        cursor = first_page.next_cursor
        assert cursor, "Expected a cursor (5 matching lots, page_size=3)"

        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, cookie_val)
            resp = client.get(f"/feed/more?cursor={cursor}")

        assert resp.status_code == 200
        body = resp.text
        # Only the matching lot should appear.
        found_ids = [int(x) for x in re.findall(r'id="lot-(\d+)"', body)]
        assert all(lid in {d.id for d in matching} for lid in found_ids), (
            f"Non-matching lot ids in response: {found_ids}"
        )
        assert not any(lid in {d.id for d in non_matching} for lid in found_ids)

    def test_filters_passed_to_search(self) -> None:
        """subject_display_names from cookie reach LotFilters.subject_display_names."""
        from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

        subject_id = 34
        subject_name = SUBJECT_TITLE_BY_ID[subject_id]

        lots = [_make_dto(i, region=subject_name) for i in range(1, 3)]
        lq = _PagedFakeLotQueryService(lots)

        first_page = lq.search(LotFilters(), page_size=1)
        cursor = first_page.next_cursor
        assert cursor

        filters = ViewFilters(subjects=[str(subject_id)])
        cookie_val = _view_filters_cookie(filters)

        app = _build_app(lq)
        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, cookie_val)
            client.get(f"/feed/more?cursor={cursor}")

        # Last search call must carry subject_display_names, not region int codes.
        last_call = lq.search_calls[-1]
        filters_used = last_call[0]
        assert subject_name in filters_used.subject_display_names
        assert filters_used.regions == ()


# ---------------------------------------------------------------------------
# T3: Exhaustion — single page → no load-more trigger
# ---------------------------------------------------------------------------


class TestExhaustion:
    """T3: dataset ≤ page_size → no #load-more-trigger on initial feed response."""

    def test_single_page_no_load_more_trigger(self) -> None:
        """With ≤ page_size lots, build_feed_context must return next_cursor=None."""
        from fis_monitor.web.feed_context import build_feed_context

        lots = [_make_dto(i) for i in range(1, 5)]  # 4 lots < 200 page_size
        lq = _PagedFakeLotQueryService(lots)

        ctx = build_feed_context(
            filters=ViewFilters(),
            lot_query=lq,  # type: ignore[arg-type]
            settings=Settings(),
            active_lot_count=len(lots),
        )
        assert ctx["next_cursor"] is None, "next_cursor must be None when all lots fit in one page"

    def test_feed_lots_no_trigger_when_next_cursor_none(self) -> None:
        """_feed_lots.html.jinja must not render #load-more-trigger when next_cursor is None."""
        from types import SimpleNamespace

        from jinja2 import Environment, FileSystemLoader

        from fis_monitor.web.templates import TEMPLATES_DIR

        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
        tmpl = env.get_template("partials/_feed_lots.html.jinja")

        filters_ctx = SimpleNamespace(
            subjects=[],
            area_min="",
            area_max="",
            area_min_label="0",
            area_max_label="∞",
            only_new=False,
        )
        html = tmpl.render(
            filters=filters_ctx,
            scope=SimpleNamespace(subjects_count=19),
            zones=SimpleNamespace(hot=(), today=()),
            archive_count=0,
            next_cursor=None,  # ← no more pages
            filters_active=False,
            health=SimpleNamespace(total_lots=0),
            session=SimpleNamespace(expired=False, expires_soon=False),
        )
        assert "load-more-trigger" not in html, (
            "#load-more-trigger must not appear when next_cursor is None"
        )

    def test_feed_lots_trigger_rendered_when_next_cursor_set(self) -> None:
        """_feed_lots.html.jinja must render #load-more-trigger when next_cursor is set.

        The trigger lives inside <section class="zone"> (Fix #1), so zones.today must
        be non-empty for the section (and trigger) to render.  The cursor is
        URL-encoded by the |urlencode filter, so '=' padding becomes '%3D'.
        Uses build_templates() so project filters (dateformat etc.) are registered.
        """
        from types import SimpleNamespace

        # Use the project's Jinja2Templates instance so all custom filters are
        # registered (e.g. |dateformat used by _lot_poster.html.jinja).
        templates = build_templates()
        env = templates.env

        fake_cursor = "dGVzdC1jdXJzb3I="  # base64 of "test-cursor"; '=' → '%3D' in URL
        fake_cursor_encoded = "dGVzdC1jdXJzb3I%3D"

        # Provide one lot in zones.today so the <section class="zone"> block renders.
        lot = _make_dto(42)
        from fis_monitor.web.sse_encoder import LotUserViewModel

        fake_lot = LotUserViewModel(lot)

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
            zones=SimpleNamespace(hot=(), today=(fake_lot,)),
            archive_count=0,
            next_cursor=fake_cursor,
            filters_active=False,
            health=SimpleNamespace(total_lots=1),
            session=SimpleNamespace(expired=False, expires_soon=False),
        )
        assert "load-more-trigger" in html, "#load-more-trigger must be present"
        assert fake_cursor_encoded in html, (
            f"URL-encoded cursor {fake_cursor_encoded!r} must appear in hx-get attr"
        )
        assert 'id="load-more-btn"' in html


# ---------------------------------------------------------------------------
# T4: Invalid cursor → 422
# ---------------------------------------------------------------------------


class TestInvalidCursor:
    """T4: malformed cursor → 422."""

    @pytest.mark.parametrize(
        "bad_cursor",
        [
            "not-base64!!!",
            "YWJj",  # valid base64 but no colon separator → "abc"
            "",
        ],
    )
    def test_malformed_cursor_returns_422(self, bad_cursor: str) -> None:
        lots = [_make_dto(1)]
        lq = _PagedFakeLotQueryService(lots)
        app = _build_app(lq)

        with TestClient(app) as client:
            resp = client.get(f"/feed/more?cursor={bad_cursor}")
        assert resp.status_code == 422, (
            f"Expected 422 for cursor={bad_cursor!r}, got {resp.status_code}"
        )

    def test_missing_cursor_param_returns_422(self) -> None:
        """cursor param is required; absent → FastAPI returns 422."""
        lots = [_make_dto(1)]
        lq = _PagedFakeLotQueryService(lots)
        app = _build_app(lq)

        with TestClient(app) as client:
            resp = client.get("/feed/more")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# T5: only_new preserved across pages
# ---------------------------------------------------------------------------


class TestOnlyNewPreserved:
    """T5: only_new filter applied on load-more pages (post-filter, not SQL)."""

    def test_seen_lots_excluded_on_load_more(self) -> None:
        """With only_new=True in cookie, seen lots must not appear in load-more response."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        unseen = _make_dto(1, date_create=now + timedelta(seconds=2), seen_at=None)
        seen = _make_dto(2, date_create=now + timedelta(seconds=1), seen_at=now)

        # page_size=1 in search() call so first page has item 1 and cursor points past it.
        lq = _PagedFakeLotQueryService([unseen, seen])

        # Get cursor pointing past lot 1 (DESC order: lot 2 is on page 2).
        first_page = lq.search(LotFilters(), page_size=1)
        cursor = first_page.next_cursor
        assert cursor, "Need a cursor for second page"

        filters = ViewFilters(only_new=True)
        cookie_val = _view_filters_cookie(filters)

        app = _build_app(lq)
        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, cookie_val)
            resp = client.get(f"/feed/more?cursor={cursor}")

        assert resp.status_code == 200
        body = resp.text
        # Lot 2 (seen) must not appear.
        assert 'id="lot-2"' not in body, "Seen lot must be excluded when only_new=True"

    def test_unseen_lots_included_on_load_more(self) -> None:
        """With only_new=True, unseen lots must still appear in load-more response."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        lots = [
            _make_dto(1, date_create=now + timedelta(seconds=3), seen_at=None),
            _make_dto(2, date_create=now + timedelta(seconds=2), seen_at=None),
            _make_dto(3, date_create=now + timedelta(seconds=1), seen_at=None),
        ]
        lq = _PagedFakeLotQueryService(lots)

        first_page = lq.search(LotFilters(), page_size=2)
        cursor = first_page.next_cursor
        assert cursor

        filters = ViewFilters(only_new=True)
        cookie_val = _view_filters_cookie(filters)

        app = _build_app(lq)
        with TestClient(app) as client:
            client.cookies.set(_COOKIE_NAME, cookie_val)
            resp = client.get(f"/feed/more?cursor={cursor}")

        assert resp.status_code == 200
        assert 'id="lot-3"' in resp.text
