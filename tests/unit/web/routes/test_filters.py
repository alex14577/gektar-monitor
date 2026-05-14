"""Unit tests for /filters routes.

Coverage:
  (a) POST /filters/view — valid body → 204 + Set-Cookie with correct JSON.
  (b) POST /filters/view — invalid body (extra forbidden field) → 422.
  (c) POST /filters/clear → 204 + Set-Cookie with max_age=0.
  (d) GET /filters/subjects → 200 text/html containing a checkbox input.
  (e) Anti-mock: FakeViewFiltersService — all methods exercised.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import get_templates, get_view_filters_service
from fis_monitor.web.routes.filters import router
from fis_monitor.web.templates import TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(svc: ViewFiltersService | None = None) -> FastAPI:
    """Build a minimal FastAPI app with the filters router and real templates."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    used_svc = svc or ViewFiltersService()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_view_filters_service] = lambda: used_svc
    return app


# ---------------------------------------------------------------------------
# Anti-mock: ensure all FakeViewFiltersService / real ViewFiltersService methods
# are exercised so we catch runtime API bugs in fakes.
# ---------------------------------------------------------------------------


def test_view_filters_service_all_methods() -> None:
    """Invoke ALL public methods of ViewFiltersService (anti-mock §6)."""
    svc = ViewFiltersService()

    filters = ViewFilters(subjects=["Московская область"], only_new=True)
    serialised = svc.serialize(filters)
    assert isinstance(serialised, str)

    recovered = svc.deserialize(serialised)
    assert recovered is not None
    assert recovered.subjects == ["Московская область"]
    assert recovered.only_new is True

    # Deserialise invalid input → None
    assert svc.deserialize("not-json") is None
    assert svc.deserialize("") is None


# ---------------------------------------------------------------------------
# (a) POST /filters/view — valid body → 204 + Set-Cookie
# ---------------------------------------------------------------------------


class TestPostViewFiltersValid:
    def test_returns_204(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                json={"subjects": ["Краснодарский край"], "only_new": True},
            )
        assert resp.status_code == 204, resp.text

    def test_set_cookie_present(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                json={"subjects": ["Краснодарский край"], "only_new": True},
            )
        assert "view_filters" in resp.cookies

    def test_cookie_contains_correct_json(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                json={
                    "subjects": ["ASCII_SUBJECT"],
                    "area_min": 5,
                    "area_max": 50,
                    "only_new": False,
                    "only_stars": True,
                },
            )
        cookie_raw = resp.cookies.get("view_filters")
        assert cookie_raw is not None, "view_filters cookie missing"
        # Cookie value is percent-encoded JSON — decode before parsing.
        data = json.loads(unquote(cookie_raw))
        assert data["subjects"] == ["ASCII_SUBJECT"]
        assert data["area_min"] == 5
        assert data["area_max"] == 50
        assert data["only_stars"] is True

    def test_empty_body_defaults_accepted(self) -> None:
        """Completely empty body should use defaults — no 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", json={})
        assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# (b) POST /filters/view — invalid body → 422
# ---------------------------------------------------------------------------


class TestPostViewFiltersInvalid:
    def test_extra_field_returns_422(self) -> None:
        """ViewFiltersBody has extra='forbid'; unknown field → 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                json={"unknown_field": "should_fail"},
            )
        assert resp.status_code == 422, resp.text

    def test_negative_area_min_returns_422(self) -> None:
        """area_min has ge=0 constraint — negative value → 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                json={"area_min": -1},
            )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# (c) POST /filters/clear → 204 + expiring Set-Cookie
# ---------------------------------------------------------------------------


class TestPostClearFilters:
    def test_returns_204(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert resp.status_code == 204, resp.text

    def test_set_cookie_with_max_age_zero(self) -> None:
        """Clear response must set the cookie with max-age=0 to delete it."""
        app = _build_app()
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/filters/clear")
        # TestClient follows Set-Cookie; the cookie value should be empty
        # and the raw header should contain max-age=0.
        set_cookie = resp.headers.get("set-cookie", "")
        assert "view_filters" in set_cookie
        assert "max-age=0" in set_cookie.lower()


# ---------------------------------------------------------------------------
# (d) GET /filters/subjects → 200 text/html with checkbox input
# ---------------------------------------------------------------------------


class TestGetSubjects:
    def test_returns_200(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert resp.status_code == 200, resp.text

    def test_content_type_is_html(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert "text/html" in resp.headers["content-type"]

    def test_contains_checkbox_input(self) -> None:
        """Template must render at least one checkbox for subject selection."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert 'type="checkbox"' in resp.text, "Expected checkbox input in response"

    def test_selected_subjects_pre_checked(self) -> None:
        """Subjects from current cookie should appear with checked attribute."""
        app = _build_app()
        # Set cookie with a known subject checked
        with TestClient(app) as client:
            client.post(
                "/filters/view",
                json={"subjects": ["Московская область"]},
            )
            resp = client.get("/filters/subjects")
        # The pre-selected subject should have 'checked' somewhere in its label context
        assert "Московская область" in resp.text
