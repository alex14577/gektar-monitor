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
from pathlib import Path

import pytest

from fis_monitor.domain.models import Settings

from .conftest import FakeRegionSubRepo, make_config_source, write_settings

_LOGGER = "fis_monitor.infra.config_source"


def _cancel_pending_timer(src) -> None:  # type: ignore[type-arg]
    with src._lock:
        timer = src._pending_timer
        src._pending_timer = None
    if timer is not None:
        timer.cancel()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_on_event_emits_file_event_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.file_event emitted at DEBUG when a relevant FS event fires."""
    cfg_path = tmp_path / "config.json"
    write_settings(cfg_path)
    src = make_config_source(cfg_path)
    src.stop()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._on_event()
    _cancel_pending_timer(src)

    records = [r for r in caplog.records if r.getMessage() == "config.file_event"]
    assert records, "expected config.file_event debug record"
    assert records[0].__dict__.get("path") == "config.json"


def test_on_event_emits_debounce_scheduled_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.debounce.scheduled emitted at DEBUG after debounce timer is set."""
    cfg_path = tmp_path / "config.json"
    write_settings(cfg_path)
    src = make_config_source(cfg_path)
    src.stop()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._on_event()
    _cancel_pending_timer(src)

    records = [r for r in caplog.records if r.getMessage() == "config.debounce.scheduled"]
    assert records, "expected config.debounce.scheduled debug record"
    assert "delay_ms" in records[0].__dict__


def test_do_reload_emits_reload_start_finish_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.reload.start + config.reload.finish emitted when content changes."""
    cfg_path = tmp_path / "config.json"
    write_settings(cfg_path)
    src = make_config_source(cfg_path)
    src.stop()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src._do_reload()

    start_records = [r for r in caplog.records if r.getMessage() == "config.reload.start"]
    finish_records = [r for r in caplog.records if r.getMessage() == "config.reload.finish"]

    assert start_records, "expected config.reload.start"
    assert finish_records, "expected config.reload.finish"
    rec = finish_records[0]
    assert "hash_old" in rec.__dict__
    assert "hash_new" in rec.__dict__
    assert "regions_diff_count" in rec.__dict__


def test_bootstrap_subscriptions_emits_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """config.bootstrap_subscriptions emitted at DEBUG on init (with region_subs_repo)."""
    cfg_path = tmp_path / "config.json"
    raw = json.dumps(Settings(regions=[42]).model_dump(mode="json")).encode()
    cfg_path.write_bytes(raw)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        src = make_config_source(cfg_path, region_subs_repo=FakeRegionSubRepo())
    src.stop()

    records = [r for r in caplog.records if r.getMessage() == "config.bootstrap_subscriptions"]
    assert records, "expected config.bootstrap_subscriptions debug record"
    assert "regions_seeded_count" in records[0].__dict__
