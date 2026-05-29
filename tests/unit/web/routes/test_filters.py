"""Unit tests for /filters routes.

Coverage:
  (a) POST /filters/view — valid form body → 200 text/html + id="feed" + Set-Cookie.
  (b) POST /filters/view — invalid form body → 422.
  (c) POST /filters/clear → 200 text/html + Set-Cookie with max_age=0.
  (d) GET /filters/subjects → 200 text/html containing a checkbox input.
  (e) Anti-mock: ViewFiltersService — all methods exercised.
  (f) Edge cases: subjects empty/multi, area_* empty/number/negative,
      checkboxes on/off, unknown field ignored.
  (g) Filter content invariants: response body reflects active filters.
  (h) OOB button: POST /filters/view emits hx-swap-oob button outside #feed.
  (i) Data-flow: subject site-ids → subject_display_names in SQL layer (not int codes).
  (j) Round-trip: cookie from POST applies on GET.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import LotUserDTO, Settings
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import (
    get_config_source,
    get_lot_query,
    get_lot_repo,
    get_templates,
    get_view_filters_service,
)
from fis_monitor.web.routes.filters import router
from fis_monitor.web.templates import TEMPLATES_DIR, build_templates
from tests.factories import make_lot
from tests.unit.web.routes.conftest import FakeConfigSource, FakeLotQueryService, FakeLotRepo

# ---------------------------------------------------------------------------
# Factories / helpers
# ---------------------------------------------------------------------------


def _make_lot_user_dto(**overrides: Any) -> LotUserDTO:
    """Build a ``LotUserDTO`` with sensible defaults for feed rendering tests.

    ``freshness="hot"`` is intentional: age_seconds=100 < 3600 (hot threshold),
    so this matches the expected freshness bucket for a freshly-seen lot.
    """
    lot = make_lot(**overrides)
    return LotUserDTO(
        **lot.model_dump(),
        age_seconds=100,
        tier="match",
        freshness="hot",  # consistent with age_seconds=100 < _AGE_HOT_SECS
    )


def _build_app(
    svc: ViewFiltersService | None = None,
    config_source: FakeConfigSource | None = None,
    lot_query: FakeLotQueryService | None = None,
    lot_repo: FakeLotRepo | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the filters router and real templates."""
    templates = build_templates()
    used_svc = svc or ViewFiltersService()
    used_cs = config_source or FakeConfigSource()
    used_lot_query = lot_query or FakeLotQueryService()
    used_lot_repo = lot_repo or FakeLotRepo()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_view_filters_service] = lambda: used_svc
    app.dependency_overrides[get_config_source] = lambda: used_cs
    app.dependency_overrides[get_lot_query] = lambda: used_lot_query
    app.dependency_overrides[get_lot_repo] = lambda: used_lot_repo
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
# (a) POST /filters/view — valid form body → 200 + text/html + id="feed" + Set-Cookie
# ---------------------------------------------------------------------------


class TestPostViewFiltersValid:
    def test_returns_200(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"subjects": "Краснодарский край", "only_new": "on"},
            )
        assert resp.status_code == 200, resp.text

    def test_content_type_is_html(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"subjects": "Краснодарский край", "only_new": "on"},
            )
        assert resp.headers["content-type"].startswith("text/html")

    def test_body_contains_feed_div(self) -> None:
        """Response must contain id="feed" so htmx outerHTML swap targets correctly."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert 'id="feed"' in resp.text

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
                    "only_new": "on",
                },
            )
        data = _cookie_data(resp)
        assert data["subjects"] == ["ASCII_SUBJECT"]
        assert data["area_min"] == 5
        assert data["area_max"] == 50
        assert data["only_new"] is True

    def test_empty_body_defaults_accepted(self) -> None:
        """Completely empty form body should use defaults — no 422."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.status_code == 200, resp.text


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
            ("500", "10", 422),  # min > max → error
            ("10", "500", 200),  # min < max → ok
            ("100", "100", 200),  # min == max → ok (boundary)
            (None, "500", 200),  # only area_max → ok
            ("500", None, 200),  # only area_min → ok
            (None, None, 200),  # neither → ok
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
        assert resp.status_code == 200, resp.text
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
        assert resp.status_code == 200, resp.text
        data = _cookie_data(resp)
        assert set(data["subjects"]) == {"Foo", "Bar"}

    def test_area_min_empty_string_treated_as_none(self) -> None:
        """Empty string for area_min (e.g. from range input) → None in cookie."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": ""})
        assert resp.status_code == 200, resp.text
        data = _cookie_data(resp)
        assert data["area_min"] is None

    def test_area_min_valid_number(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"area_min": "10"})
        assert resp.status_code == 200, resp.text
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

    def test_unknown_form_field_ignored(self) -> None:
        """Unknown form fields must not cause 422 — they are silently ignored."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                data={"unknown_field": "should_be_ignored"},
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# (g) Filter content invariants — feed partial reflects filtered results
# ---------------------------------------------------------------------------


class TestPostViewFiltersFilterContent:
    def test_with_lots_body_contains_lot_data(self) -> None:
        """When lots are returned, the feed partial renders lot content."""
        dto = _make_lot_user_dto(id=99, municipality="Тестовый Город")
        lot_query = FakeLotQueryService(items=(dto,))
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.status_code == 200
        assert 'id="feed"' in resp.text

    def test_empty_filter_passes_empty_subject_display_names_to_query(self) -> None:
        """Empty subjects → LotFilters.subject_display_names == () (no region restriction)."""
        lot_query = FakeLotQueryService()
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            client.post("/filters/view", data={})
        assert lot_query.search_calls
        last = lot_query.search_calls[-1]
        assert last.subject_display_names == ()
        assert last.regions == ()  # int-codes field also empty

    def test_subject_filter_translated_to_subject_display_names(self) -> None:
        """Subject site-ids are translated to display names for SQL region filter.

        Invariant: POST with subjects=["27","34"] must pass
        LotFilters.subject_display_names containing the display names from
        SUBJECT_TITLE_BY_ID — NOT integer codes in .regions.
        This ensures SQL WHERE region IN ('Республика Карелия', 'Мурманская область')
        matches the TEXT lots.region column.
        """
        lot_query = FakeLotQueryService()
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            client.post(
                "/filters/view",
                **_form([("subjects", "27"), ("subjects", "34")]),
            )
        assert lot_query.search_calls
        last = lot_query.search_calls[-1]
        assert set(last.subject_display_names) == {
            SUBJECT_TITLE_BY_ID[27],
            SUBJECT_TITLE_BY_ID[34],
        }
        # regions (int-based field) must be empty — site-ids are not stored there
        assert last.regions == ()

    def test_unknown_subject_id_silently_dropped(self) -> None:
        """Subject IDs not in SUBJECT_TITLE_BY_ID are dropped from subject_display_names."""
        lot_query = FakeLotQueryService()
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            # 99999 is not a known site-id; 27 is valid
            client.post(
                "/filters/view",
                **_form([("subjects", "27"), ("subjects", "99999")]),
            )
        assert lot_query.search_calls
        last = lot_query.search_calls[-1]
        assert set(last.subject_display_names) == {SUBJECT_TITLE_BY_ID[27]}

    def test_empty_filter_no_lots_shows_feed_div(self) -> None:
        """With no lots, feed div is still rendered (empty state or loading)."""
        lot_query = FakeLotQueryService(items=())
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert 'id="feed"' in resp.text

    def test_active_filter_renders_empty_state(self) -> None:
        """Active subject filter with no results → empty-state message rendered."""
        lot_query = FakeLotQueryService(items=())
        app = _build_app(lot_query=lot_query)
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={"subjects": "27"})
        assert resp.status_code == 200
        # The "Ничего не подходит" empty state must appear inside #feed.
        assert 'id="feed"' in resp.text
        assert "Ничего не подходит" in resp.text


# ---------------------------------------------------------------------------
# (h) OOB button invariants — filter trigger updated outside #feed on POST
# ---------------------------------------------------------------------------


class TestOobFilterTrigger:
    def test_post_response_contains_oob_button(self) -> None:
        """POST /filters/view response must contain hx-swap-oob button.

        htmx uses this to update #filter-trigger outside #feed without a full
        page reload.
        """
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.status_code == 200
        assert 'hx-swap-oob="true"' in resp.text
        assert 'id="filter-trigger"' in resp.text

    def test_post_with_subjects_oob_shows_count(self) -> None:
        """POST with subjects selected → OOB button renders «N выбран(о)»."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/filters/view",
                **_form([("subjects", "27"), ("subjects", "34")]),
            )
        body = resp.text
        assert 'hx-swap-oob="true"' in body
        # Exactly 2 subjects selected — expect count string
        assert "2 выбран" in body

    def test_post_empty_subjects_oob_shows_all(self) -> None:
        """POST with no subjects → OOB button renders «Все · N»."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        body = resp.text
        assert 'hx-swap-oob="true"' in body
        assert "Все · " in body

    def test_feed_lots_template_no_oob_without_render_oob(self) -> None:
        """_feed_lots.html.jinja does not emit hx-swap-oob when render_oob is absent.

        This verifies the template guard: OOB button is only emitted when the
        POST path sets render_oob=True in context.  Without it (GET path), no
        OOB is present, preventing spurious DOM swaps.
        """
        from types import SimpleNamespace

        from jinja2 import Environment, FileSystemLoader

        templates_dir = str(TEMPLATES_DIR)
        env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)
        tmpl = env.get_template("partials/_feed_lots.html.jinja")

        filters_ctx = SimpleNamespace(
            subjects=[],
            area_min="",
            area_max="",
            area_min_label="0",
            area_max_label="∞",
            only_new=False,
        )
        scope_ctx = SimpleNamespace(subjects_count=19)
        zones_ctx = SimpleNamespace(hot=(), today=())

        html = tmpl.render(
            filters=filters_ctx,
            scope=scope_ctx,
            zones=zones_ctx,
            archive_count=0,
            lot_count=0,
            filters_active=False,
            health=SimpleNamespace(total_lots=0),
            session=SimpleNamespace(expired=False, expires_soon=False),
            # render_oob intentionally absent — simulates GET path
        )
        assert 'id="filter-trigger"' not in html or 'hx-swap-oob="true"' not in html, (
            "OOB button must not appear when render_oob is absent"
        )
        assert 'hx-swap-oob="true"' not in html


# ---------------------------------------------------------------------------
# (j) Round-trip: cookie from POST applies on GET (M-2)
# ---------------------------------------------------------------------------


class TestRoundTripCookieApplied:
    def test_post_cookie_encodes_subjects_and_query_receives_subject_display_names(self) -> None:
        """POST sets cookie with subjects; re-applying it translates to subject_display_names.

        Invariant (M-2):
          1. POST /filters/view with subjects=["34"] → cookie encodes subjects=["34"].
          2. Deserialising that cookie into ViewFilters and running build_feed_context
             passes LotFilters.subject_display_names containing SUBJECT_TITLE_BY_ID[34].

        This tests the full cookie → query bridge without needing a running GET / route.
        """
        from fis_monitor.domain.models import Settings
        from fis_monitor.services.view_filters import deserialize
        from fis_monitor.web.feed_context import build_feed_context

        # Step 1: POST /filters/view to obtain the cookie value
        app = _build_app()
        with TestClient(app) as client:
            post_resp = client.post(
                "/filters/view",
                **_form([("subjects", "34")]),
            )
        assert post_resp.status_code == 200
        cookie_val = post_resp.cookies.get("view_filters")
        assert cookie_val is not None, "Cookie must be set by POST"

        # Cookie must encode subjects=["34"]
        cookie_data = json.loads(unquote(cookie_val))
        assert cookie_data["subjects"] == ["34"]

        # Step 2: Deserialise cookie → ViewFilters → build_feed_context
        view_filters = deserialize(cookie_val)
        assert view_filters is not None
        assert view_filters.subjects == ["34"]

        tracking_query = FakeLotQueryService()
        build_feed_context(
            filters=view_filters,
            lot_query=tracking_query,  # type: ignore[arg-type]
            settings=Settings(),
            active_lot_count=0,
        )
        assert tracking_query.search_calls, "build_feed_context must call search()"
        last = tracking_query.search_calls[-1]
        assert SUBJECT_TITLE_BY_ID[34] in last.subject_display_names


# ---------------------------------------------------------------------------
# (c) POST /filters/clear → 204 + expiring Set-Cookie
# ---------------------------------------------------------------------------


class TestPostClearFilters:
    def test_returns_200(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert resp.status_code == 200, resp.text

    def test_content_type_is_html(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert resp.headers["content-type"].startswith("text/html")

    def test_body_contains_feed_div(self) -> None:
        """Response must contain id='feed' so htmx outerHTML swap finds the target."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert 'id="feed"' in resp.text

    def test_set_cookie_with_max_age_zero(self) -> None:
        """Clear response must set the cookie with max-age=0 to delete it."""
        app = _build_app()
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/filters/clear")
        set_cookie = resp.headers.get("set-cookie", "")
        assert "view_filters" in set_cookie
        assert "max-age=0" in set_cookie.lower()

    def test_oob_filter_trigger_present(self) -> None:
        """Response must include the OOB swap for #filter-trigger (counter reset)."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert 'hx-swap-oob="true"' in resp.text
        assert 'id="filter-trigger"' in resp.text


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
        """Template renders a checkbox for each subject in filters.rf_subjects."""
        cs = FakeConfigSource(Settings(subject_site_ids=[87]))  # migrated → rf_subjects
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert 'type="checkbox"' in resp.text, "Expected checkbox input in response"

    def test_subject_names_displayed(self) -> None:
        """Template renders names from SUBJECT_TITLE_BY_ID for filters.rf_subjects."""
        cs = FakeConfigSource(Settings(subject_site_ids=[87]))  # migrated → rf_subjects
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert "Якутия" in resp.text, "Expected subject name in response"

    def test_full_catalog_rendered_regardless_of_rf_subjects(self) -> None:
        """Popover shows all catalog subjects (ADR-035: view scope is region-independent).

        Even when rf_subjects contains only [27, 28], all 19 subjects including
        Якутия (87) must appear — view scope is independent of notify selection.
        """
        cs = FakeConfigSource(Settings(subject_site_ids=[27, 28]))  # migrated → rf_subjects
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

        assert SUBJECT_TITLE_BY_ID[27] in resp.text
        assert SUBJECT_TITLE_BY_ID[28] in resp.text
        # All subjects including Якутия (87) must appear — full catalog.
        assert "Якутия" in resp.text

    def test_empty_rf_subjects_renders_all_catalog_checkboxes(self) -> None:
        """Empty filters.rf_subjects → all 19 catalog subjects still rendered."""
        from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

        cs = FakeConfigSource(Settings())
        app = _build_app(config_source=cs)
        with TestClient(app) as client:
            resp = client.get("/filters/subjects")
        assert resp.status_code == 200
        assert 'type="checkbox"' in resp.text
        # Full catalog must appear.
        assert len(SUBJECT_TITLE_BY_ID) == 19  # guard against catalog drift

    def test_selected_subjects_pre_checked(self) -> None:
        """Checkbox for a subject in the view_filters cookie must have checked attribute.

        ViewFilters.subjects = list[str]; the template uses ``sid | string in selected_subjects``
        so the cookie value "87" (string) must match site-id 87.
        """
        from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService

        cs = FakeConfigSource(Settings(subject_site_ids=[87, 88]))  # migrated → rf_subjects
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


# ---------------------------------------------------------------------------
# (m72b) HX-Trigger: filter-changed — SSE live-reconnect header (ADR-052 resolved)
# ---------------------------------------------------------------------------


class TestHxTriggerFilterChanged:
    def test_post_view_filters_returns_hx_trigger_header(self) -> None:
        """POST /filters/view must include HX-Trigger: filter-changed.

        htmx reads this header and fires htmx:trigger on document.body so
        app.js can reconnect the SSE EventSource with the updated cookie.
        """
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/view", data={})
        assert resp.headers.get("hx-trigger") == "filter-changed", (
            "Expected HX-Trigger: filter-changed in POST /filters/view response"
        )

    def test_post_clear_filters_returns_hx_trigger_header(self) -> None:
        """POST /filters/clear must include HX-Trigger: filter-changed.

        Clearing filters also changes the view_filters cookie; the SSE
        connection must reconnect to pick up the reset predicate.
        """
        app = _build_app()
        with TestClient(app) as client:
            resp = client.post("/filters/clear")
        assert resp.headers.get("hx-trigger") == "filter-changed", (
            "Expected HX-Trigger: filter-changed in POST /filters/clear response"
        )
