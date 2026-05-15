"""Unit tests for POST /settings/regions and POST /settings/recipients.

Tests use TestClient + app.dependency_overrides with a FakeConfigSource that
implements save() (ADR-023: ConfigSource now includes save()).

Anti-mock pattern: FakeConfigSource implements ALL public methods including save(),
and a dedicated all-methods test exercises every method.

Coverage (POST /settings/regions — htmx form, ≥1 macro-region required):
  1. Happy path with form-encoded region_ids → 200 + partial HTML.
  2. Saved Settings.regions matches submission (deduplicated).
  3. Other Settings fields preserved.
  4. Empty selection → 422.
  5. Unknown macro id (e.g. 99) → 422.
  6. Duplicates collapsed.

Coverage (POST /settings/recipients): unchanged JSON contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.models import Settings
from fis_monitor.web.deps import get_config_source, get_templates
from fis_monitor.web.routes.settings import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR

# ---------------------------------------------------------------------------
# Fake
# ---------------------------------------------------------------------------


class FakeConfigSource:
    """Fake ConfigSource — implements ALL public methods including save() (ADR-023)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.current_calls: int = 0
        self.subscribe_calls: int = 0
        self.save_calls: list[Settings] = []

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: Any) -> object:
        self.subscribe_calls += 1
        return object()

    def save(self, settings: Settings) -> None:
        self._settings = settings
        self.save_calls.append(settings)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    fake_config: FakeConfigSource | None = None,
) -> tuple[FastAPI, FakeConfigSource]:
    fc = fake_config or FakeConfigSource()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fc
    app.dependency_overrides[get_templates] = lambda: templates
    return app, fc


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL methods on FakeConfigSource
# ---------------------------------------------------------------------------


def test_all_fake_config_source_methods_exercised() -> None:
    """Every method on FakeConfigSource must be callable without error (§6)."""
    fc = FakeConfigSource()
    s = fc.current()
    assert isinstance(s, Settings)
    assert fc.current_calls == 1

    fc.subscribe(lambda x: None)
    assert fc.subscribe_calls == 1

    new_settings = Settings(interval_minutes=5)
    fc.save(new_settings)
    assert len(fc.save_calls) == 1
    assert fc.save_calls[0].interval_minutes == 5
    assert fc.current() == new_settings


# ---------------------------------------------------------------------------
# POST /settings/regions — happy path
# ---------------------------------------------------------------------------


def test_post_regions_happy_path_200() -> None:
    """POST /settings/regions with valid macro ids → 200 + HTML partial."""
    app, _fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/settings/regions",
            data={"region_ids": ["1", "2"]},
        )
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert 'id="scope-and-subjects"' in resp.text


def test_post_regions_updates_settings() -> None:
    """POST /settings/regions stores the new regions list via config_source.save()."""
    app, fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post(
            "/settings/regions",
            data={"region_ids": ["1", "2"]},
        )
    assert len(fc.save_calls) == 1
    saved = fc.save_calls[0]
    assert saved.regions == [1, 2]


def test_post_regions_dedups_submission() -> None:
    """Duplicate region_ids in submission collapse to a single entry."""
    app, fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post(
            "/settings/regions",
            data={"region_ids": ["1", "1", "2"]},
        )
    saved = fc.save_calls[0]
    assert saved.regions == [1, 2]



def test_post_regions_preserves_other_settings_fields() -> None:
    """POST /settings/regions must not clobber unrelated Settings fields."""
    initial = Settings(interval_minutes=42)
    app, fc = _make_app(FakeConfigSource(settings=initial))
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post("/settings/regions", data={"region_ids": ["1"]})

    saved = fc.save_calls[0]
    assert saved.interval_minutes == 42


# ---------------------------------------------------------------------------
# POST /settings/regions — validation errors
# ---------------------------------------------------------------------------


def test_post_regions_empty_selection_422() -> None:
    """POST /settings/regions with zero region_ids → 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/settings/regions", data={})
    assert resp.status_code == 422
    assert "At least one" in resp.text


def test_post_regions_unknown_macro_422() -> None:
    """POST /settings/regions with unknown macro id (e.g. 99) → 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/settings/regions", data={"region_ids": ["99"]})
    assert resp.status_code == 422
    assert "Unknown macro-region" in resp.text


def test_post_regions_zero_macro_422() -> None:
    """POST /settings/regions with region_ids=0 → 422 (0 is not a valid macro)."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/settings/regions", data={"region_ids": ["0"]})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /settings/recipients — happy path
# ---------------------------------------------------------------------------


def test_post_recipients_happy_path_200() -> None:
    """POST /settings/recipients with valid emails → 200 {"ok": true}."""
    app, _fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/settings/recipients",
            json={"recipients": ["alice@example.com", "bob@example.com"]},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_post_recipients_updates_settings() -> None:
    """POST /settings/recipients stores the new recipients list via save()."""
    app, fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post(
            "/settings/recipients",
            json={"recipients": ["user@example.org"]},
        )

    assert len(fc.save_calls) == 1
    saved = fc.save_calls[0]
    assert saved.notifications.email.recipients == ["user@example.org"]


def test_post_recipients_preserves_other_settings_fields() -> None:
    """POST /settings/recipients must not clobber other Settings fields."""
    initial = Settings(interval_minutes=30, regions=[10, 11])
    app, fc = _make_app(FakeConfigSource(settings=initial))
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post("/settings/recipients", json={"recipients": ["x@y.com"]})

    saved = fc.save_calls[0]
    assert saved.interval_minutes == 30
    assert saved.regions == [10, 11]


def test_post_recipients_empty_list_accepted() -> None:
    """POST /settings/recipients with empty recipients → 200 (disabling email is valid)."""
    app, fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/settings/recipients", json={"recipients": []})
    assert resp.status_code == 200
    saved = fc.save_calls[0]
    assert saved.notifications.email.recipients == []


def test_post_recipients_default_empty_when_field_absent() -> None:
    """POST /settings/recipients with body {} (no field) → empty list accepted."""
    app, fc = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/settings/recipients", json={})
    assert resp.status_code == 200
    saved = fc.save_calls[0]
    assert saved.notifications.email.recipients == []


# ---------------------------------------------------------------------------
# POST /settings/recipients — validation errors
# ---------------------------------------------------------------------------


def test_post_recipients_invalid_email_422() -> None:
    """POST /settings/recipients with invalid email address → 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/settings/recipients",
            json={"recipients": ["not-an-email"]},
        )
    assert resp.status_code == 422
