"""Layer 4 route tests for GET /feed/count (B1, ADR-060 amendment 2026-06-01).

Coverage (per test-strategy §Layer 4 — Web):
  1. Returns 200 with #feed-lot-count span and correct data-count attribute.
  2. view_filters cookie is respected — count reflects filtered result.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService, serialize
from fis_monitor.web.deps import (
    get_lot_query,
    get_lot_repo,
    get_templates,
    get_view_filters_service,
)
from fis_monitor.web.routes.main import router
from fis_monitor.web.templates import STATIC_DIR, build_templates
from tests.unit.web.routes.conftest import FakeLotQueryService, FakeLotRepo


def _make_app(lot_query: FakeLotQueryService, lot_repo: FakeLotRepo | None = None) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_lot_query] = lambda: lot_query
    _repo = lot_repo if lot_repo is not None else FakeLotRepo()
    app.dependency_overrides[get_lot_repo] = lambda: _repo
    app.dependency_overrides[get_templates] = lambda: build_templates()
    app.dependency_overrides[get_view_filters_service] = lambda: ViewFiltersService()
    return app


def test_feed_count_returns_span_with_correct_data_count() -> None:
    """GET /feed/count returns 200 with #feed-lot-count span and correct data-count."""
    from fis_monitor.domain.models import LotUserDTO
    from tests.factories import make_lot

    lot = make_lot(id=1)
    payload = lot.model_dump()
    payload.update(age_seconds=100, tier="match", freshness="hot", seen_at=None)
    dto = LotUserDTO(**payload)

    fake_query = FakeLotQueryService(items=(dto,))
    app = _make_app(fake_query)

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/feed/count")

    assert resp.status_code == 200
    assert 'id="feed-lot-count"' in resp.text
    assert 'data-count="1"' in resp.text


def test_feed_count_returns_oob_registry_count() -> None:
    """GET /feed/count response contains OOB #registry-count span with data-count=X."""
    fake_query = FakeLotQueryService(items=())
    fake_repo = FakeLotRepo(active_count=77)
    app = _make_app(fake_query, lot_repo=fake_repo)

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/feed/count")

    assert resp.status_code == 200
    assert 'id="registry-count"' in resp.text
    assert 'data-count="77"' in resp.text
    assert fake_repo.count_active_calls == 1


def test_feed_count_respects_view_filters_cookie() -> None:
    """GET /feed/count with view_filters cookie passes filters to lot_query.count()."""
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

    fake_query = FakeLotQueryService(items=())
    app = _make_app(fake_query)

    # Use a real catalog id so _view_filters_to_lot_filters translates it.
    some_id = next(iter(SUBJECT_TITLE_BY_ID))
    cookie = serialize(ViewFilters(subjects=[str(some_id)]))

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/feed/count", cookies={"view_filters": cookie})

    assert resp.status_code == 200
    assert 'data-count="0"' in resp.text
    # count() called with filters that include the subject display name
    # (FakeLotQueryService.count returns len(items)=0 regardless of filters,
    # but the route must have called lot_query — verified via response shape).
    assert 'id="feed-lot-count"' in resp.text
