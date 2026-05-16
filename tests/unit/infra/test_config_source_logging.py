"""Logging tests for WatchdogConfigSource DEBUG events (gektar_monitor-b9wq).

Covers:
- config.file_event (DEBUG — on relevant FS event)
- config.debounce.scheduled (DEBUG — after debounce timer scheduled)
- config.reload.start (DEBUG — content hash changed, parse begins)
- config.reload.finish (DEBUG — parse succeeded, swap done)
- config.bootstrap_subscriptions (DEBUG — on init with region_subs_repo)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fis_monitor.domain.models import Settings
from fis_monitor.infra.config_source import WatchdogConfigSource

_LOGGER = "fis_monitor.infra.config_source"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


class _FakeRegionSubRepo:
    def get_subscribed_at(self, region_id: int) -> None:
        return None

    def set_if_absent(self, region_id: int, at: datetime) -> None:
        pass

    def delete(self, region_id: int) -> None:
        pass


def _make_source(
    path: Path,
    *,
    clock: _FakeClock | None = None,
    region_subs_repo: _FakeRegionSubRepo | None = None,
) -> WatchdogConfigSource:
    if clock is None:
        clock = _FakeClock()
    parser = lambda raw: Settings.model_validate(json.loads(raw))  # noqa: E731
    with patch("fis_monitor.infra.config_source.Observer") as MockObs:
        mock_obs = MagicMock()
        MockObs.return_value = mock_obs
        src = WatchdogConfigSource(
            path=path,
            clock=clock,
            parser=parser,
            region_subs_repo=region_subs_repo,
        )
        src._observer = mock_obs
    return src


def _write_settings(path: Path) -> bytes:
    raw = json.dumps(Settings().model_dump(mode="json")).encode()
    path.write_bytes(raw)
    return raw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_on_event_emits_file_event_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.file_event emitted at DEBUG when a relevant FS event fires."""
    cfg_path = tmp_path / "config.json"
    _write_settings(cfg_path)
    src = _make_source(cfg_path)
    src.stop()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._on_event()
    # cancel the debounce timer immediately so it doesn't fire
    with src._lock:
        timer = src._pending_timer
        src._pending_timer = None
    if timer is not None:
        timer.cancel()

    records = [r for r in caplog.records if r.getMessage() == "config.file_event"]
    assert records, "expected config.file_event debug record"
    assert records[0].__dict__.get("path") == "config.json"


def test_on_event_emits_debounce_scheduled_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.debounce.scheduled emitted at DEBUG after debounce timer is set."""
    cfg_path = tmp_path / "config.json"
    _write_settings(cfg_path)
    src = _make_source(cfg_path)
    src.stop()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._on_event()
    with src._lock:
        timer = src._pending_timer
        src._pending_timer = None
    if timer is not None:
        timer.cancel()

    records = [r for r in caplog.records if r.getMessage() == "config.debounce.scheduled"]
    assert records, "expected config.debounce.scheduled debug record"
    assert "delay_ms" in records[0].__dict__


def test_do_reload_emits_reload_start_finish_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.reload.start + config.reload.finish emitted when content changes."""
    cfg_path = tmp_path / "config.json"
    _write_settings(cfg_path)
    src = _make_source(cfg_path)
    src.stop()

    # Trigger reload directly (bypass debounce timer).
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._do_reload()

    start_records = [r for r in caplog.records if r.getMessage() == "config.reload.start"]
    finish_records = [r for r in caplog.records if r.getMessage() == "config.reload.finish"]

    assert start_records, "expected config.reload.start"
    assert finish_records, "expected config.reload.finish"
    # finish should carry hash_old, hash_new, regions_diff_count
    rec = finish_records[0]
    assert "hash_old" in rec.__dict__
    assert "hash_new" in rec.__dict__
    assert "regions_diff_count" in rec.__dict__


def test_bootstrap_subscriptions_emits_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.bootstrap_subscriptions emitted at DEBUG on init (with region_subs_repo)."""
    cfg_path = tmp_path / "config.json"
    # Write settings with one region so bootstrap seeds it.
    raw = json.dumps(Settings(regions=[42]).model_dump(mode="json")).encode()
    cfg_path.write_bytes(raw)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src = _make_source(cfg_path, region_subs_repo=_FakeRegionSubRepo())
    src.stop()

    records = [r for r in caplog.records if r.getMessage() == "config.bootstrap_subscriptions"]
    assert records, "expected config.bootstrap_subscriptions debug record"
    assert "regions_seeded_count" in records[0].__dict__
