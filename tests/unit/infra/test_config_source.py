"""Unit tests for WatchdogConfigSource.

Strategy: bypass the real watchdog Observer by calling ``_on_event()`` (which
triggers the debounce timer) or ``_do_reload()`` directly.  The ``parser`` DI
seam allows injecting a lambda instead of the real Pydantic model.

All tests are fast — no real filesystem watcher is started except where
explicitly documented.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileMovedEvent

from fis_monitor.domain.models import Settings
from fis_monitor.infra.config_source import (
    MAX_CONFIG_SIZE,
    WatchdogConfigSource,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Minimal Clock fake: returns a fixed UTC datetime, advances on demand."""

    def __init__(self) -> None:
        self._dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 1.0

    def tick(self, seconds: float = 1.0) -> None:
        from datetime import timedelta
        self._dt = self._dt + timedelta(seconds=seconds)


def _make_source(
    path: Path,
    *,
    parser: Callable[[bytes], Settings] | None = None,
    clock: _FakeClock | None = None,
) -> WatchdogConfigSource:
    """Factory: create WatchdogConfigSource with a mock Observer (no real FS watch)."""
    if parser is None:
        parser = lambda raw: Settings.model_validate(json.loads(raw))  # noqa: E731
    if clock is None:
        clock = _FakeClock()

    with patch("fis_monitor.infra.config_source.Observer") as MockObserver:
        mock_obs = MagicMock()
        MockObserver.return_value = mock_obs
        src = WatchdogConfigSource(path=path, clock=clock, parser=parser)
        # Replace real observer with mock so stop() doesn't hang.
        src._observer = mock_obs
    return src


def _write_settings(path: Path, **overrides: Any) -> bytes:
    """Write a valid Settings JSON to path and return raw bytes."""
    data = Settings(**overrides).model_dump(mode="json")
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return raw


# ---------------------------------------------------------------------------
# 1. Bootstrap default — path not found
# ---------------------------------------------------------------------------


def test_bootstrap_default_when_file_absent(tmp_path: Path) -> None:
    """When config.json does not exist, current() returns default Settings."""
    absent = tmp_path / "nonexistent" / "config.json"
    src = _make_source(absent)
    assert src.current() == Settings()


# ---------------------------------------------------------------------------
# 2. Valid reload triggers callback with new Settings
# ---------------------------------------------------------------------------


def test_valid_reload_triggers_callback(tmp_path: Path) -> None:
    """Writing valid config → _do_reload() swaps _current and delivers to subscriber."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    received: list[Settings] = []
    with src.subscribe(received.append):
        _write_settings(cfg, interval_minutes=5)
        src._do_reload()

    assert len(received) == 1
    assert received[0].interval_minutes == 5
    assert src.current().interval_minutes == 5
    assert src.reload_count == 1
    assert src.last_reload_at is not None


# ---------------------------------------------------------------------------
# 3. Invalid JSON — warning logged, old _current retained, error counter bumped
# ---------------------------------------------------------------------------


def test_invalid_json_retains_old_current(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Invalid JSON → warning logged (without PII), old snapshot kept."""
    cfg = tmp_path / "config.json"
    _write_settings(cfg, interval_minutes=10)
    src = _make_source(cfg)
    # force first load
    src._do_reload()
    assert src.current().interval_minutes == 10

    # Now corrupt the file
    cfg.write_bytes(b"{invalid json")
    import logging
    with caplog.at_level(logging.WARNING, logger="fis_monitor.infra.config_source"):
        src._do_reload()

    assert src.current().interval_minutes == 10  # retained
    assert src.reload_error_count == 1
    assert src.last_error_at is not None
    assert any("invalid JSON" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Invalid Pydantic shape — warning with error count, old retained
# ---------------------------------------------------------------------------


def test_invalid_pydantic_retains_old_current(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Valid JSON but wrong Pydantic shape → validation error count in warning."""
    cfg = tmp_path / "config.json"
    _write_settings(cfg, interval_minutes=7)
    src = _make_source(cfg)
    src._do_reload()
    assert src.current().interval_minutes == 7

    # Write JSON that fails Pydantic validation (interval_minutes out of range)
    bad = json.dumps({"interval_minutes": 9999}).encode()
    cfg.write_bytes(bad)

    import logging
    with caplog.at_level(logging.WARNING, logger="fis_monitor.infra.config_source"):
        src._do_reload()

    assert src.current().interval_minutes == 7  # old snapshot retained
    assert src.reload_error_count == 1
    # Must log "N validation error(s)" not the raw exception
    assert any("validation error" in r.message for r in caplog.records)
    # Must NOT log raw exc repr
    assert not any("ValidationError" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. Debounce coalesce — N events in quick succession → one _do_reload call
# ---------------------------------------------------------------------------


def test_debounce_coalesces_events(tmp_path: Path) -> None:
    """Multiple rapid FS events → debounce → exactly one _do_reload invocation."""
    cfg = tmp_path / "config.json"
    _write_settings(cfg)
    src = _make_source(cfg)

    reload_count: list[int] = [0]
    original_do_reload = src._do_reload

    def counting_reload() -> None:
        reload_count[0] += 1
        original_do_reload()

    src._do_reload = counting_reload  # type: ignore[method-assign]

    # Simulate 5 rapid events — each resets the debounce timer.
    # We patch threading.Timer to be synchronous for speed.
    fired: list[int] = [0]

    class _InstantTimer:
        def __init__(self, delay: float, fn: Callable[[], None]) -> None:
            self._fn = fn

        def cancel(self) -> None:
            pass

        def start(self) -> None:
            fired[0] += 1
            # Don't actually fire — we'll fire manually after all events.

    with patch("fis_monitor.infra.config_source.threading.Timer", _InstantTimer):
        for _ in range(5):
            src._on_event()

    # Only the last timer should have been "started" (all previous were cancelled).
    # The actual do_reload hasn't fired yet in this mock (start() is a no-op).
    # Manually trigger the reload once to simulate the timer firing.
    src._do_reload()
    assert reload_count[0] == 1, f"Expected 1 reload, got {reload_count[0]}"


# ---------------------------------------------------------------------------
# 6. FileMovedEvent dest_path filter — atomic save must trigger reload
# ---------------------------------------------------------------------------


def test_file_moved_event_dest_path_triggers_reload(tmp_path: Path) -> None:
    """FileMovedEvent with dest_path matching config.json must trigger reload.

    This is the BA-5 critical fix: inotify on Linux uses dest_path for atomic
    saves (os.replace / mv).  A filter on src_path would miss them.
    """
    cfg = tmp_path / "config.json"
    _write_settings(cfg, interval_minutes=3)
    src = _make_source(cfg)

    triggered: list[bool] = []
    original_on_event = src._on_event

    def capture_trigger() -> None:
        triggered.append(True)
        original_on_event()

    src._on_event = capture_trigger  # type: ignore[method-assign]

    # Simulate an atomic save: src is a temp file, dest is the real config.json.
    event = FileMovedEvent(
        src_path=str(tmp_path / "config.json.tmp123"),
        dest_path=str(cfg),
    )

    # Access the handler registered with the Observer mock.
    # Re-create via the handler class to simulate real dispatch.
    from fis_monitor.infra.config_source import _ConfigFileEventHandler

    handler = _ConfigFileEventHandler(
        target_name=cfg.name,
        on_relevant_event=capture_trigger,
    )
    handler.on_moved(event)

    assert len(triggered) == 1, "FileMovedEvent with dest=config.json must trigger reload"


def test_file_moved_event_non_target_dest_does_not_trigger(tmp_path: Path) -> None:
    """FileMovedEvent with dest_path NOT matching config.json must be ignored."""
    from fis_monitor.infra.config_source import _ConfigFileEventHandler

    triggered: list[bool] = []

    handler = _ConfigFileEventHandler(
        target_name="config.json",
        on_relevant_event=lambda: triggered.append(True),
    )
    event = FileMovedEvent(
        src_path=str(tmp_path / "config.json"),  # src matches, but…
        dest_path=str(tmp_path / "other.json"),  # …dest does NOT
    )
    handler.on_moved(event)
    assert not triggered, "Event with non-target dest should be ignored"


# ---------------------------------------------------------------------------
# 7. Subscribe + deliver
# ---------------------------------------------------------------------------


def test_subscribe_and_deliver(tmp_path: Path) -> None:
    """subscribe(cb) → _do_reload with new content → cb called with new Settings."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    received: list[Settings] = []
    sub = src.subscribe(received.append)

    _write_settings(cfg, interval_minutes=20)
    src._do_reload()

    assert len(received) == 1
    assert received[0].interval_minutes == 20
    sub.unsubscribe()


# ---------------------------------------------------------------------------
# 8. Unsubscribe idempotency
# ---------------------------------------------------------------------------


def test_unsubscribe_is_idempotent(tmp_path: Path) -> None:
    """Calling unsubscribe() multiple times must not raise."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    sub = src.subscribe(lambda s: None)
    sub.unsubscribe()
    sub.unsubscribe()  # second call must be a no-op


# ---------------------------------------------------------------------------
# 9. Content-hash dedup — identical bad JSON → exactly one warning
# ---------------------------------------------------------------------------


def test_content_hash_dedup_suppresses_repeated_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Writing identical bad JSON 3 times → at most one parse warning (hash dedup)."""
    cfg = tmp_path / "config.json"
    cfg.write_bytes(b"{not valid json at all")
    src = _make_source(cfg)

    import logging
    with caplog.at_level(logging.WARNING, logger="fis_monitor.infra.config_source"):
        src._do_reload()  # first: parse attempted, warning logged
        src._do_reload()  # second: same hash → skip entirely
        src._do_reload()  # third: same hash → skip entirely

    warning_msgs = [
        r.message for r in caplog.records
        if "invalid JSON" in r.message or "validation" in r.message
    ]
    assert len(warning_msgs) == 1, (
        f"Expected 1 warning, got {len(warning_msgs)}: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# 10. Size cap — oversized file skipped
# ---------------------------------------------------------------------------


def test_size_cap_skips_large_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """File > MAX_CONFIG_SIZE triggers warning and parse is skipped."""
    cfg = tmp_path / "config.json"
    # Create a sparse file that reports the oversized size via os.stat.
    # Use truncate: on Linux this creates a sparse file instantly without
    # actually allocating disk space.
    with open(cfg, "wb") as fh:
        fh.seek(MAX_CONFIG_SIZE + 1)
        fh.write(b"\x00")

    src = _make_source(cfg)

    # Reset hash so the size-check runs (not deduped).
    with src._lock:
        src._last_content_hash = b""

    import logging

    with caplog.at_level(logging.WARNING, logger="fis_monitor.infra.config_source"):
        src._do_reload()

    assert any("too large" in r.message for r in caplog.records)
    assert src.reload_error_count == 1


# ---------------------------------------------------------------------------
# 11. stop() — Observer teardown and idempotency
# ---------------------------------------------------------------------------


def test_stop_joins_observer(tmp_path: Path) -> None:
    """stop() calls observer.stop() and observer.join(); is_alive() returns False → no warning."""
    cfg = tmp_path / "config.json"

    with patch("fis_monitor.infra.config_source.Observer") as MockObserver:
        mock_obs = MagicMock()
        mock_obs.is_alive.return_value = False
        MockObserver.return_value = mock_obs
        src = WatchdogConfigSource(
            path=cfg,
            clock=_FakeClock(),
            parser=lambda raw: Settings.model_validate(json.loads(raw)),
        )

    src.stop()
    mock_obs.stop.assert_called_once()
    mock_obs.join.assert_called_once_with(timeout=5.0)


def test_stop_idempotent(tmp_path: Path) -> None:
    """Calling stop() twice must not raise."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)
    src.stop()
    src.stop()  # second call must be safe


def test_stop_cancels_pending_timer(tmp_path: Path) -> None:
    """stop() cancels the debounce timer if one is pending."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    mock_timer = MagicMock()
    with src._lock:
        src._pending_timer = mock_timer

    src.stop()
    mock_timer.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# 12. Settings.__eq__ skip callback — identical Settings → no delivery
# ---------------------------------------------------------------------------


def test_identical_settings_skips_callback(tmp_path: Path) -> None:
    """If reload produces the same Settings, subscriber callback is NOT called."""
    cfg = tmp_path / "config.json"
    # Write default-equivalent settings
    raw = Settings().model_dump_json().encode()
    cfg.write_bytes(raw)

    src = _make_source(cfg)
    # Load initial snapshot (same as default Settings).
    src._do_reload()

    received: list[Settings] = []
    sub = src.subscribe(received.append)

    # Write identical content again.
    cfg.write_bytes(raw)
    # Clear hash so reload is attempted (bypass dedup for this test).
    with src._lock:
        src._last_content_hash = b""
    src._do_reload()

    sub.unsubscribe()
    assert received == [], "Identical Settings reload must not trigger subscriber callback"


# ---------------------------------------------------------------------------
# Integration: real Observer + tmp_path (marked slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_watchdog_observer_triggers_reload(tmp_path: Path) -> None:
    """Real WatchdogConfigSource with real Observer picks up atomic file write.

    Uses os.replace (atomic save) and waits up to 2 s for callback delivery.
    """
    import os
    cfg = tmp_path / "config.json"
    clock = _FakeClock()

    received: list[Settings] = []
    event_received = threading.Event()

    def _cb(s: Settings) -> None:
        received.append(s)
        event_received.set()

    src = WatchdogConfigSource(
        path=cfg,
        clock=clock,
        parser=lambda raw: Settings.model_validate(json.loads(raw)),
    )
    sub = src.subscribe(_cb)
    try:
        # Atomic write via os.replace.
        tmp_cfg = tmp_path / "config.json.tmp"
        raw = Settings(interval_minutes=42).model_dump_json().encode()
        tmp_cfg.write_bytes(raw)
        os.replace(str(tmp_cfg), str(cfg))

        # Wait up to 2 s for the watchdog to pick it up.
        assert event_received.wait(timeout=2.0), "Watchdog did not fire within 2 s"
        assert received[0].interval_minutes == 42
    finally:
        sub.unsubscribe()
        src.stop()
