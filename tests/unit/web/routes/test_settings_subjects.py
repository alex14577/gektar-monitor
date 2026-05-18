"""Unit tests for POST /settings/subjects (ADR-035).

Coverage:
  1. POST /settings/subjects happy path → 200 + partial HTML, config saved.
  2. POST /settings/subjects empty list → 200, saved as [] (notify-all per ADR-035 I4).
  3. POST /settings/subjects unknown catalog id → 200 + scope_error in HTML (v7ar).
  4. POST /settings/subjects does not clobber other Settings fields.
  5. GET /settings HTML includes subject chip-picker with all catalog subjects.
  6. GET /settings HTML shows correct checked state from filters.rf_subjects.
  7. POST /settings/subjects persistence error → 200 + scope_error in HTML (v7ar).
  8. POST /settings/subjects success → scope_saved=subjects in HTML (triggers toast, v7ar).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import Settings
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID
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

    def __init__(
        self, settings: Settings | None = None, save_raises: Exception | None = None
    ) -> None:
        self._settings = settings or Settings()
        self.save_calls: list[Settings] = []
        self._save_raises = save_raises

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> object:
        return object()

    def save(self, settings: Settings) -> None:
        if self._save_raises is not None:
            raise self._save_raises
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
    # bd 47uh: header-status VM deps
    from datetime import UTC
    from datetime import datetime as _dt

    from fis_monitor.web.deps import get_clock, get_lot_repo
    from tests.fakes.lot_repository import FakeLotRepository

    class _C:
        def now(self):
            return _dt(2026, 5, 18, tzinfo=UTC)
        def monotonic(self):
            return 0.0

    app.dependency_overrides[get_lot_repo] = lambda: FakeLotRepository()
    app.dependency_overrides[get_clock] = lambda: _C()
    return app, fc


# ---------------------------------------------------------------------------
# POST /settings/subjects — happy path
# ---------------------------------------------------------------------------


class TestPostSubjectsHappyPath:
    def test_returns_200_html_on_valid_ids(self) -> None:
        """Valid catalog ids → 200 + partial HTML (ADR-035: full catalog, any region)."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        # 87 = Якутия, 27 = Карелия — both are in SUBJECT_TITLE_BY_ID
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=87&rf_subjects=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert "text/html" in resp.headers["content-type"]
        assert 'id="scope-and-subjects"' in resp.text

    def test_saves_rf_subjects(self) -> None:
        """POST /settings/subjects stores the new ids in filters.rf_subjects."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            client.post(
                "/settings/subjects",
                data="rf_subjects=87&rf_subjects=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert len(fc.save_calls) == 1
        assert set(fc.save_calls[0].filters.rf_subjects) == {87, 27}

    def test_empty_list_is_accepted(self) -> None:
        """Empty rf_subjects → 200, saved as [] (notify-all per ADR-035 I4)."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert len(fc.save_calls) == 1
        assert fc.save_calls[0].filters.rf_subjects == []

    def test_does_not_clobber_other_fields(self) -> None:
        """rf_subjects update must preserve other Settings fields."""
        initial = Settings(interval_minutes=42)
        fc = FakeConfigSource(initial)
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            client.post(
                "/settings/subjects",
                data="rf_subjects=87",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        saved = fc.save_calls[0]
        assert saved.interval_minutes == 42, "interval_minutes must be preserved"

    def test_any_catalog_id_valid_regardless_of_regions(self) -> None:
        """ADR-035: notify scope is independent of regions.

        27 (Карелия) is Арктика-only but must be accepted even when regions=[1] (ДФО).
        """
        fc = FakeConfigSource(Settings(regions=[1]))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=27",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert fc.save_calls[0].filters.rf_subjects == [27]


# ---------------------------------------------------------------------------
# POST /settings/subjects — validation errors
# ---------------------------------------------------------------------------


class TestPostSubjectsValidation:
    def test_unknown_catalog_id_returns_200_with_error(self) -> None:
        """site-id not in SUBJECT_TITLE_BY_ID → 200 + scope_error in partial (v7ar)."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=99999",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert 'id="scope-and-subjects"' in resp.text

    def test_unknown_id_error_mentions_id(self) -> None:
        """scope_error must mention the offending id."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=99999",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert "99999" in resp.text

    def test_unknown_id_does_not_save(self) -> None:
        """Validation error must not call config_source.save()."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            client.post(
                "/settings/subjects",
                data="rf_subjects=99999",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert fc.save_calls == []


class TestPostSubjectsPersistenceError:
    def test_save_exception_returns_200_with_error(self) -> None:
        """config_source.save() raises → 200 + scope_error in partial (v7ar)."""
        fc = FakeConfigSource(Settings(), save_raises=RuntimeError("disk full"))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=87",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert "disk full" in resp.text
        assert 'id="scope-and-subjects"' in resp.text


class TestPostSubjectsSuccessFeedback:
    def test_success_includes_scope_saved_subjects(self) -> None:
        """Successful POST /settings/subjects → HTML has scope_saved toast trigger (v7ar)."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.post(
                "/settings/subjects",
                data="rf_subjects=87",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert "subjects" in resp.text  # scope_saved='subjects' rendered in toast script


# ---------------------------------------------------------------------------
# GET /settings HTML — subjects chip-picker renders full catalog
# ---------------------------------------------------------------------------


class TestSettingsPageSubjectsUI:
    def test_all_catalog_subjects_in_html(self) -> None:
        """GET /settings HTML must contain all catalog subject names."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        assert resp.status_code == 200, resp.text
        # All 19 subjects must appear.
        for name in SUBJECT_TITLE_BY_ID.values():
            assert name in resp.text, f"Expected catalog subject '{name}' in settings page"

    def test_heading_is_subiekty_uvedomleniy(self) -> None:
        """Settings page must use new heading 'Субъекты уведомлений'."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        assert "Субъекты уведомлений" in resp.text

    def test_selected_rf_subjects_checked(self) -> None:
        """filters.rf_subjects=[87] → checkbox value=87 is checked."""
        from fis_monitor.domain.models import FiltersConfig

        fc = FakeConfigSource(Settings(filters=FiltersConfig(rf_subjects=[87])))
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        body = resp.text
        # value=87 must be checked.
        assert re.search(r'<input[^>]*value="87"[^>]*\bchecked\b', body), (
            "Expected value=87 input to be checked"
        )
        # value=88 must NOT be checked (it is not in rf_subjects).
        assert not re.search(r'<input[^>]*value="88"[^>]*\bchecked\b', body), (
            "Expected value=88 input to NOT be checked"
        )

    def test_form_input_name_is_rf_subjects(self) -> None:
        """Form checkboxes must use name='rf_subjects', not the legacy name."""
        fc = FakeConfigSource(Settings())
        app, _ = _make_app(fc)
        with TestClient(app) as client:
            resp = client.get("/settings", headers={"accept": "text/html"})
        assert 'name="rf_subjects"' in resp.text
        assert 'name="subject_site_ids"' not in resp.text
