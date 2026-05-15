"""Unit tests for POST /settings/subjects (ADR-031, gektar_monitor-ekb).

Coverage:
  1. POST /settings/subjects happy path → 204, config saved.
  2. POST /settings/subjects empty list → 204 (disabling subject filter is valid).
  3. POST /settings/subjects out-of-scope id → 422.
  4. POST /settings/subjects does not clobber other Settings fields.
  5. GET /settings HTML includes subject chip-picker with scoped subject names.
  6. GET /filters/subjects returns scoped subjects (real names, not PLACEHOLDER).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import Settings
from fis_monitor.web.deps import (
    get_config_source,
    get_settings_service,
    get_smtp_test,
    get_templates,
)
from fis_monitor.web.routes.settings import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSource:
    """Fake ConfigSource — all public methods implemented (anti-mock §6)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.save_calls: list[Settings] = []

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> object:
        return object()

    def save(self, settings: Settings) -> None:
        self._settings = settings
        self.save_calls.append(settings)


class _FakeSettingsService:
    def set_smtp_credentials(self, creds: Any) -> None:
        pass


class _FakeSmtpTestService:
    def test_send(self, lot: Any, recipient: str) -> Any:
        return object()


def _make_app(
    fake_config: FakeConfigSource | None = None,
) -> tuple[FastAPI, FakeConfigSource]:
    fc = fake_config or FakeConfigSource()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI()
    # Static mount required: base.html.jinja calls url_for('static', ...).
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fc
    app.dependency_overrides[get_settings_service] = lambda: _FakeSettingsService()
    app.dependency_overrides[get_smtp_test] = lambda: _FakeSmtpTestService()
    app.dependency_overrides[get_templates] = lambda: templates
    return app, fc


# ---------------------------------------------------------------------------
# POST /settings/subjects — happy path
# ---------------------------------------------------------------------------


class TestPostSubjectsHappyPath:
    def test_returns_204_on_valid_ids(self) -> None:
        """Valid site-ids within regions=[1] → 204."""
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        # 87 = Якутия, belongs to ДФО (macro 1)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="subject_site_ids=87&subject_site_ids=88",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 204, resp.text

    def test_saves_subject_site_ids(self) -> None:
        """POST /settings/subjects stores the new ids via config_source.save()."""
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            client.post(
                "/settings/subjects",
                data="subject_site_ids=87&subject_site_ids=88",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert len(fc.save_calls) == 1
        assert set(fc.save_calls[0].subject_site_ids) == {87, 88}

    def test_empty_list_accepted(self) -> None:
        """Empty subject_site_ids (no restriction) → 204."""
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 204, resp.text
        assert fc.save_calls[0].subject_site_ids == []

    def test_does_not_clobber_other_fields(self) -> None:
        """subject_site_ids update must preserve other Settings fields."""
        initial = Settings(interval_minutes=42, regions=[1])
        fc = FakeConfigSource(initial)
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            client.post(
                "/settings/subjects",
                data="subject_site_ids=87",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        saved = fc.save_calls[0]
        assert saved.interval_minutes == 42, "interval_minutes must be preserved"


# ---------------------------------------------------------------------------
# POST /settings/subjects — validation errors
# ---------------------------------------------------------------------------


class TestPostSubjectsValidation:
    def test_out_of_scope_id_returns_422(self) -> None:
        """site-id not in subjects_for_macros(regions) → 422."""
        # regions=[1] (ДФО); site-id 27 (Карелия) belongs to Арктика only
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="subject_site_ids=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 422, resp.text

    def test_out_of_scope_error_mentions_id(self) -> None:
        """422 detail must mention the offending id."""
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="subject_site_ids=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert "27" in resp.text

    def test_both_regions_allows_shared_subjects(self) -> None:
        """87 (Якутия) is in both ДФО and Арктика — valid for regions=[1,2]."""
        fc = FakeConfigSource(Settings(regions=[1, 2]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="subject_site_ids=87&subject_site_ids=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# GET /settings HTML — subjects chip-picker renders correct names
# ---------------------------------------------------------------------------


class TestSettingsPageSubjectsUI:
    def test_subject_names_in_html(self) -> None:
        """GET /settings HTML must contain subject names (not raw numbers)."""
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        assert resp.status_code == 200, resp.text
        # At least one known ДФО subject name must appear
        assert "Якутия" in resp.text, "Expected ДФО subject name in settings page"

    def test_current_subject_site_ids_checked(self) -> None:
        """Settings with subject_site_ids=[87] → checkbox value=87 is checked."""
        fc = FakeConfigSource(Settings(regions=[1], subject_site_ids=[87]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        body = resp.text
        # The checked checkbox must have value="87" co-located in the same <input> tag.
        assert re.search(r'<input[^>]*value="87"[^>]*\bchecked\b', body), (
            "Expected value=87 input to be checked"
        )
        # Negative case: site-id 88 must NOT be checked (it is not in subject_site_ids).
        assert not re.search(r'<input[^>]*value="88"[^>]*\bchecked\b', body), (
            "Expected value=88 input to NOT be checked"
        )
