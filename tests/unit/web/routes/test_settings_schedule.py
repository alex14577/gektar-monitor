"""Unit tests for POST /settings/schedule (ADR-033, gektar_monitor-xjv).

Coverage:
  1. POST /settings/schedule happy path → 200 + partial HTML with saved values.
  2. POST /settings/schedule does not clobber unrelated Settings fields.
  3. Validation: interval_minutes out of range (< 0, > 60) → 422.
  4. Validation: full_scan_time invalid format (not HH:MM) → 422.
  5. Validation: full_scan_l2_priority_days < 1 → 422.
  6. Boundary values: interval=0 (continuous) accepted; time=23:59 accepted;
     l2_days=365 accepted.
  7. Hot-reload smoke: config_source.current() reflects new schedule after save.
"""

from __future__ import annotations

from typing import Any

import pytest
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


_VALID_PAYLOAD = {
    "interval_minutes": "15",
    "full_scan_time": "04:00",
    "full_scan_l2_priority_days": "7",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPostScheduleHappyPath:
    def test_returns_200_with_html(self) -> None:
        """Successful POST returns 200 with partial HTML (htmx outerHTML swap)."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post("/settings/schedule", data=_VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text
        assert "text/html" in resp.headers["content-type"]

    def test_response_contains_saved_interval(self) -> None:
        """Returned partial HTML must show the saved interval_minutes value."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "interval_minutes": "5"},
            )
        assert 'value="5"' in resp.text, "Expected saved interval_minutes in partial HTML"

    def test_saves_all_three_fields(self) -> None:
        """All three fields are persisted together (atomic update per ADR-033)."""
        app, fc = _make_app()
        payload = {
            "interval_minutes": "5",
            "full_scan_time": "02:30",
            "full_scan_l2_priority_days": "14",
        }
        with TestClient(app) as client:
            client.post("/settings/schedule", data=payload)
        saved = fc.save_calls[0]
        assert saved.interval_minutes == 5
        assert saved.monitoring.full_scan_time == "02:30"
        assert saved.monitoring.full_scan_l2_priority_days == 14

    def test_does_not_clobber_other_fields(self) -> None:
        """Schedule update must preserve unrelated Settings fields."""
        initial = Settings(regions=[2], subject_site_ids=[27])
        app, fc = _make_app(FakeConfigSource(initial))
        with TestClient(app) as client:
            client.post("/settings/schedule", data=_VALID_PAYLOAD)
        saved = fc.save_calls[0]
        assert saved.regions == [2], "regions must be preserved"
        assert saved.subject_site_ids == [27], "subject_site_ids must be preserved"

    def test_hot_reload_smoke(self) -> None:
        """After save, config_source.current() reflects the new schedule.

        MonitorCycleService calls current() on every iteration — so the
        updated value is available immediately on the next cycle (ADR-033).
        """
        app, fc = _make_app()
        with TestClient(app) as client:
            client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "interval_minutes": "1"},
            )
        # Simulate MonitorCycleService reading config on next iteration
        live_settings = fc.current()
        assert live_settings.interval_minutes == 1


# ---------------------------------------------------------------------------
# Boundary values
# ---------------------------------------------------------------------------


class TestPostScheduleBoundaries:
    def test_interval_zero_accepted(self) -> None:
        """interval_minutes=0 (continuous mode) must be valid."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "interval_minutes": "0"},
            )
        assert resp.status_code == 200, resp.text

    def test_interval_60_accepted(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "interval_minutes": "60"},
            )
        assert resp.status_code == 200, resp.text

    def test_full_scan_time_23_59_accepted(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_time": "23:59"},
            )
        assert resp.status_code == 200, resp.text

    def test_full_scan_time_00_00_accepted(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_time": "00:00"},
            )
        assert resp.status_code == 200, resp.text

    def test_l2_days_1_accepted(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_l2_priority_days": "1"},
            )
        assert resp.status_code == 200, resp.text

    def test_l2_days_365_accepted(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_l2_priority_days": "365"},
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestPostScheduleValidation:
    @pytest.mark.parametrize("bad_interval", ["-1", "61", "100"])
    def test_interval_out_of_range_returns_422(self, bad_interval: str) -> None:
        """interval_minutes outside 0–60 → 422."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "interval_minutes": bad_interval},
            )
        assert resp.status_code == 422, f"Expected 422 for interval={bad_interval}"

    @pytest.mark.parametrize(
        "bad_time",
        ["24:00", "99:99", "4:00", "04:60", "noon", "4", "", "25:30", "00:60"],
    )
    def test_full_scan_time_bad_format_returns_422(self, bad_time: str) -> None:
        """full_scan_time not matching HH:MM → 422."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_time": bad_time},
            )
        assert resp.status_code == 422, f"Expected 422 for full_scan_time={bad_time!r}"

    @pytest.mark.parametrize("bad_days", ["0", "-1", "366", "1000"])
    def test_l2_days_out_of_range_returns_422(self, bad_days: str) -> None:
        """full_scan_l2_priority_days outside 1–365 → 422."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={**_VALID_PAYLOAD, "full_scan_l2_priority_days": bad_days},
            )
        assert resp.status_code == 422, f"Expected 422 for l2_days={bad_days}"

    def test_missing_field_returns_422(self) -> None:
        """Omitting any required field → 422."""
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/settings/schedule",
                data={"interval_minutes": "15"},  # missing full_scan_time and l2_days
            )
        assert resp.status_code == 422, resp.text
