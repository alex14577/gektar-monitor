"""Unit tests for /filters routes.

Coverage:
  (a) POST /filters/view — valid form body → 204 + Set-Cookie with correct JSON.
  (b) POST /filters/view — invalid form body → 422.
  (c) POST /filters/clear → 204 + Set-Cookie with max_age=0.
  (d) GET /filters/subjects → 200 text/html containing a checkbox input.
  (e) Anti-mock: ViewFiltersService — all methods exercised.
  (f) Edge cases: subjects empty/multi, area_* empty/number/negative,
      checkboxes on/off, unknown field ignored.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import Settings
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import get_config_source, get_templates, get_view_filters_service
from fis_monitor.web.routes.filters import router
from fis_monitor.web.templates import TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfigSource:
    """Minimal fake config source for filter route tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: object) -> object:
        return object()

    def save(self, settings: Settings) -> None:
        self._settings = settings


def _build_app(
    svc: ViewFiltersService | None = None,
    config_source: _FakeConfigSource | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the filters router and real templates."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    used_svc = svc or ViewFiltersService()
    used_cs = config_source or _FakeConfigSource()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_view_filters_service] = lambda: used_svc
    app.dependency_overrides[get_config_source] = lambda: used_cs
    return app


_FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded"}


def _form(pairs: list[tuple[str, str]]) -> dict[str, object]:
    """Encode a list of (key, value) pairs as form body kwargs for TestClient."""
    body = urllib.parse.urlencode(pairs)
    return {"content": body, "headers": _FORM_HEADERS}


def _cookie_data(resp: object) -> dict:  # type: ignore[type-arg]
    """Decode percent-encoded JSON cookie from response."""
    cookie_raw = resp.cookies.get("view_filters")  # type: ignore[attr-defined]
    assert cookie_raw is not None, "view_filters cookie missing"
    return json.loads(unquote(cookie_raw))


# ---------------------------------------------------------------------------
# Anti-mock: ensure all ViewFiltersService methods are exercised
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
# (a) POST /filters/view — valid form body → 204 + Set-Cookie
# ---------------------------------------------------------------------------


class TestPostViewFiltersValid:
    def test_returns_204(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"subjects": "Краснодарский край", "only_new": "on"},
            )
        assert resp.status_code == 204, resp.text

    def test_set_cookie_present(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"subjects": "Краснодарский край", "only_new": "on"},
            )
        assert "view_filters" in resp.cookies

    def test_cookie_contains_correct_json(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={
                    "subjects": "ASCII_SUBJECT",
                    "area_min": "5",
                    "area_max": "50",
                    "only_stars": "on",
                },
            )
        data = _cookie_data(resp)
        assert data["subjects"] == ["ASCII_SUBJECT"]
        assert data["area_min"] == 5
        assert data["area_max"] == 50
        assert data["only_stars"] is True

    def test_empty_body_defaults_accepted(self) -> None:
        """Completely empty form body should use defaults — no 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# (b) POST /filters/view — invalid form body → 422
# ---------------------------------------------------------------------------


class TestPostViewFiltersInvalid:
    def test_negative_area_min_returns_422(self) -> None:
        """area_min with ge=0 constraint — negative value → 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": "-1"})
        assert resp.status_code == 422, resp.text

    def test_negative_area_max_returns_422(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_max": "-5"})
        assert resp.status_code == 422, resp.text

    def test_non_numeric_area_min_returns_422(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": "abc"})
        assert resp.status_code == 422, resp.text

    def test_subjects_over_limit_returns_422(self) -> None:
        """More than 50 subjects → 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                **_form([("subjects", f"subject_{i}") for i in range(51)]),
            )
        assert resp.status_code == 422, resp.text

    def test_subject_too_long_returns_422(self) -> None:
        """Subject longer than 128 chars → 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"subjects": "x" * 129})
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize(
        "area_min, area_max, expected_status",
        [
            ("500", "10", 422),   # min > max → error
            ("10", "500", 204),   # min < max → ok
            ("100", "100", 204),  # min == max → ok (boundary)
            (None, "500", 204),   # only area_max → ok
            ("500", None, 204),   # only area_min → ok
            (None, None, 204),    # neither → ok
        ],
    )
    def test_area_min_greater_than_area_max_returns_422(
        self,
        area_min: str | None,
        area_max: str | None,
        expected_status: int,
    ) -> None:
        """Cross-field validation: area_min must be <= area_max (gektar_monitor-gho)."""
        app = _build_app()
        data: dict[str, str] = {}
        if area_min is not None:
            data["area_min"] = area_min
        if area_max is not None:
            data["area_max"] = area_max
        with TestClient(app) as client:
            resp = client.post("/filters/view", data=data)
        assert resp.status_code == expected_status, resp.text
        if expected_status == 422:
            assert resp.json()["detail"] == "area_min must be <= area_max"


# ---------------------------------------------------------------------------
# (f) Edge cases per task spec
# ---------------------------------------------------------------------------


class TestPostViewFiltersEdgeCases:
    def test_subjects_empty_list(self) -> None:
        """No subjects key in form → subjects defaults to []."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.status_code == 204, resp.text
        data = _cookie_data(resp)
        assert data["subjects"] == []

    def test_subjects_multiple_values(self) -> None:
        """Repeated subjects keys → parsed as list."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                **_form([("subjects", "Foo"), ("subjects", "Bar")]),
            )
        assert resp.status_code == 204, resp.text
        data = _cookie_data(resp)
        assert set(data["subjects"]) == {"Foo", "Bar"}

    def test_area_min_empty_string_treated_as_none(self) -> None:
        """Empty string for area_min (e.g. from range input) → None in cookie."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": ""})
        assert resp.status_code == 204, resp.text
        data = _cookie_data(resp)
        assert data["area_min"] is None

    def test_area_min_valid_number(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": "10"})
        assert resp.status_code == 204, resp.text
        data = _cookie_data(resp)
        assert data["area_min"] == 10

    def test_checkbox_only_new_checked(self) -> None:
        """Checkbox with value 'on' → only_new True in cookie."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"only_new": "on"})
        data = _cookie_data(resp)
        assert data["only_new"] is True

    def test_checkbox_only_new_unchecked(self) -> None:
        """Absent checkbox key → only_new False in cookie."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        data = _cookie_data(resp)
        assert data["only_new"] is False

    def test_checkbox_only_stars_checked(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"only_stars": "on"})
        data = _cookie_data(resp)
        assert data["only_stars"] is True

    def test_checkbox_only_stars_unchecked(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        data = _cookie_data(resp)
        assert data["only_stars"] is False

    def test_unknown_form_field_ignored(self) -> None:
        """Unknown form fields must not cause 422 — they are silently ignored."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"unknown_field": "should_be_ignored"},
            )
        assert resp.status_code == 204, resp.text


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
        """Template renders a checkbox for each subject in subject_site_ids."""
        cs = _FakeConfigSource(Settings(subject_site_ids=[87]))
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert 'type="checkbox"' in resp.text, "Expected checkbox input in response"

    def test_subject_names_displayed(self) -> None:
        """Template renders names from SUBJECT_TITLE_BY_ID for subject_site_ids."""
        cs = _FakeConfigSource(Settings(subject_site_ids=[87]))
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert "Якутия" in resp.text, "Expected subject name in response"

    def test_only_monitored_subjects_rendered(self) -> None:
        """Only subjects in subject_site_ids appear — not the full macro-region scope."""
        cs = _FakeConfigSource(Settings(subject_site_ids=[27, 28]))
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID
        assert SUBJECT_TITLE_BY_ID[27] in resp.text
        assert SUBJECT_TITLE_BY_ID[28] in resp.text
        # site-id 87 (Якутия) is not in subject_site_ids — must not appear.
        assert "Якутия" not in resp.text

    def test_empty_subject_site_ids_renders_no_checkboxes(self) -> None:
        """Empty subject_site_ids → no checkboxes rendered (valid state)."""
        cs = _FakeConfigSource(Settings(subject_site_ids=[]))
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert resp.status_code == 200
        assert 'type="checkbox"' not in resp.text

    def test_selected_subjects_pre_checked(self) -> None:
        """Checkbox for a subject in the view_filters cookie must have checked attribute.

        ViewFilters.subjects = list[str]; the template uses ``sid | string in selected_subjects``
        so the cookie value "87" (string) must match site-id 87.
        """
        from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService

        cs = _FakeConfigSource(Settings(subject_site_ids=[87, 88]))
        svc = ViewFiltersService()
        app = _build_app(svc=svc, config_source=cs)

        # Serialise a ViewFilters with subjects=["87"] into the cookie.
        # serialize() returns a percent-encoded JSON string; pass it directly
        # so the route reads the percent-encoded value and can unquote it.
        cookie_value = svc.serialize(ViewFilters(subjects=["87"]))

        with TestClient(app) as client:
            client.cookies.set("view_filters", cookie_value)
            resp = client.get("/filters/subjects")

        body = resp.text
        # site-id 87 (Якутия) must be pre-checked.
        assert re.search(r'<input[^>]*value="87"[^>]*\bchecked\b', body), (
            "Expected value=87 input to be checked"
        )
        # site-id 88 must NOT be checked.
        assert not re.search(r'<input[^>]*value="88"[^>]*\bchecked\b', body), (
            "Expected value=88 input to NOT be checked"
        )
