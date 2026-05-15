"""Unit tests for GET /lots and GET /lots/{lot_id} routes.

Uses TestClient + app.dependency_overrides with a FakeLotQueryService that
exercises ALL methods of the fake (anti-mock pattern, orchestrator-playbook §6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import LotUserDTO
from fis_monitor.services.lot_query import LotFilters, Page
from fis_monitor.web.deps import get_lot_query
from fis_monitor.web.routes.lots import router

# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------

_DEFAULT_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_lot_user_dto(lot_id: int = 1) -> LotUserDTO:
    """Minimal LotUserDTO for use in fakes."""
    return LotUserDTO(
        id=lot_id,
        cadastral_no="27:23:0040000:0001",
        area_sqm=10_000,
        region="Хабаровский край",
        municipality="Хабаровск",
        land_category="Земли сельхозназначения",
        permitted_use="ЛПХ",
        ogv="Минимущество HK",
        status="Свободен",
        date_create=_DEFAULT_NOW,
        date_update=_DEFAULT_NOW,
        lat=48.48,
        lon=135.08,
        has_boundaries=True,
        raw_json={"k": "v"},
        parser_version=1,
        first_seen=_DEFAULT_NOW,
        last_seen=_DEFAULT_NOW,
        detail_fetched_at=_DEFAULT_NOW,
        enrichment_status="done",
        last_seen_at=_DEFAULT_NOW,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
        age_seconds=0,
        tier="match",
        freshness="hot",
    )


class FakeLotQueryService:
    """Fake for LotQueryService — implements ALL public methods."""

    def __init__(
        self,
        page: Page[LotUserDTO] | None = None,
        single: LotUserDTO | None = None,
        raise_on_invalid_cursor: bool = False,
    ) -> None:
        self._page: Page[LotUserDTO] = page or Page(
            items=(_make_lot_user_dto(),),
            next_cursor=None,
            has_more=False,
        )
        self._single: LotUserDTO | None = single
        self._raise_on_invalid_cursor = raise_on_invalid_cursor
        # Track calls to verify all methods are exercised.
        self.search_calls: list[dict[str, Any]] = []
        self.get_by_id_calls: list[int] = []

    # Implement ALL LotQueryService public methods:

    def search(
        self,
        filters: LotFilters,
        *,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> Page[LotUserDTO]:
        if self._raise_on_invalid_cursor and cursor and "!" in cursor:
            raise ValueError("malformed cursor")
        self.search_calls.append(
            {"filters": filters, "page_size": page_size, "cursor": cursor}
        )
        return self._page

    def get_by_id(self, lot_id: int) -> LotUserDTO | None:
        self.get_by_id_calls.append(lot_id)
        return self._single


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    fake: FakeLotQueryService | None = None,
) -> tuple[FastAPI, FakeLotQueryService]:
    """Return (app, fake) with dependency_overrides wired."""
    if fake is None:
        fake = FakeLotQueryService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_lot_query] = lambda: fake
    return app, fake


# ---------------------------------------------------------------------------
# Tests — GET /lots
# ---------------------------------------------------------------------------


def test_list_lots_returns_200_with_items() -> None:
    app, fake = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "has_more" in body
    assert len(body["items"]) == 1
    assert fake.search_calls != []


def test_list_lots_passes_filters() -> None:
    app, fake = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots?regions=1&regions=2&status=Свободен&page_size=10")
    assert resp.status_code == 200
    call = fake.search_calls[0]
    assert call["filters"].regions == (1, 2)
    assert call["filters"].status == "Свободен"
    assert call["page_size"] == 10


def test_list_lots_passes_cursor() -> None:
    app, fake = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots?cursor=abc123")
    assert resp.status_code == 200
    assert fake.search_calls[0]["cursor"] == "abc123"


def test_list_lots_unknown_status_returns_422() -> None:
    """LotFilters raises ValueError for unknown status — route maps to 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/lots?status=UNKNOWN_STATUS")
    assert resp.status_code == 422


def test_list_lots_malformed_cursor_returns_422() -> None:
    """Malformed cursor returns 422, not 500 (acceptance)."""
    fake = FakeLotQueryService(raise_on_invalid_cursor=True)
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/lots?cursor=!!!not-base64!!!")
    assert resp.status_code == 422


def test_list_lots_no_raw_json_leak() -> None:
    """raw_json must never appear in the response payload."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots")
    assert "raw_json" not in resp.text


# ---------------------------------------------------------------------------
# Tests — GET /lots/{lot_id}
# ---------------------------------------------------------------------------


def test_get_lot_returns_200_when_found() -> None:
    dto = _make_lot_user_dto(lot_id=42)
    app, fake = _make_app(FakeLotQueryService(single=dto))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 42
    assert fake.get_by_id_calls == [42]


def test_get_lot_returns_404_when_not_found() -> None:
    app, fake = _make_app(FakeLotQueryService(single=None))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/lots/99")
    assert resp.status_code == 404
    assert fake.get_by_id_calls == [99]


def test_get_lot_no_raw_json_leak() -> None:
    dto = _make_lot_user_dto()
    app, _ = _make_app(FakeLotQueryService(single=dto))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots/1")
    assert "raw_json" not in resp.text


# ---------------------------------------------------------------------------
# Tests — GET /lots/{lot_id}/redirect
# ---------------------------------------------------------------------------


def test_redirect_returns_302_with_location_for_known_lot() -> None:
    """Invariant: existing lot → 302 + Location pointing to torgi.gov.ru."""
    dto = _make_lot_user_dto(lot_id=9963)
    app, fake = _make_app(FakeLotQueryService(single=dto))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/lots/9963/redirect", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "9963" in location
    assert location.startswith("https://")
    assert fake.get_by_id_calls == [9963]


def test_redirect_returns_404_for_unknown_lot() -> None:
    """Invariant: unknown lot_id → 404, no redirect issued."""
    app, fake = _make_app(FakeLotQueryService(single=None))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/lots/99999/redirect", follow_redirects=False)
    assert resp.status_code == 404
    assert fake.get_by_id_calls == [99999]


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods in one test
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Verify every method of FakeLotQueryService is callable (anti-mock §6)."""
    dto = _make_lot_user_dto(lot_id=7)
    fake = FakeLotQueryService(single=dto)

    page = fake.search(LotFilters(), page_size=50, cursor=None)
    assert isinstance(page, Page)

    result = fake.get_by_id(7)
    assert result is not None
    assert result.id == 7

    assert len(fake.search_calls) == 1
    assert fake.get_by_id_calls == [7]
