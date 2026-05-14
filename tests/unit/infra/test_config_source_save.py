"""Unit tests for WatchdogConfigSource.save().

Strategy: bypass the real watchdog Observer (mock it out), call ``save()``
directly and verify:
  1. File is written atomically (no partial state visible on the filesystem).
  2. ``_current`` is updated optimistically after save.
  3. The content-hash is updated so the self-triggered watchdog event is
     a no-op (dedup silences it).
  4. A subscriber callback is NOT invoked synchronously by ``save()`` itself
     (notification travels via the watchdog reload-path, not an in-process
     shortcut — ADR-023).  We verify this by checking that calling
     ``_do_reload()`` after ``save()`` with an identical-hash file does NOT
     deliver another callback (dedup skips it).
  5. On I/O error: temp file is cleaned up and the original exception is re-raised.
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

from fis_monitor.domain.models import Settings
from fis_monitor.infra.config_source import WatchdogConfigSource

# ---------------------------------------------------------------------------
# Helpers (mirrors test_config_source.py helpers to keep tests independent)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self._dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 1.0


def _make_source(
    path: Path,
    *,
    parser: Callable[[bytes], Settings] | None = None,
    clock: _FakeClock | None = None,
) -> WatchdogConfigSource:
    """Create WatchdogConfigSource with a mocked Observer (no real FS watch)."""
    if parser is None:
        parser = lambda raw: Settings.model_validate(json.loads(raw))  # noqa: E731
    if clock is None:
        clock = _FakeClock()

    with patch("fis_monitor.infra.config_source.Observer") as MockObserver:
        mock_obs = MagicMock()
        MockObserver.return_value = mock_obs
        src = WatchdogConfigSource(path=path, clock=clock, parser=parser)
        src._observer = mock_obs
    return src


# ---------------------------------------------------------------------------
# 1. save() writes file with correct content
# ---------------------------------------------------------------------------


def test_save_writes_file_content(tmp_path: Path) -> None:
    """save() persists the new Settings JSON to the config file."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    new_settings = Settings(interval_minutes=7, regions=[3, 4])
    src.save(new_settings)

    assert cfg.exists(), "config file must exist after save()"
    saved_raw = cfg.read_bytes()
    parsed = Settings.model_validate_json(saved_raw)
    assert parsed.interval_minutes == 7
    assert parsed.regions == [3, 4]


# ---------------------------------------------------------------------------
# 2. save() is atomic — no temp file left behind
# ---------------------------------------------------------------------------


def test_save_no_temp_file_leftover(tmp_path: Path) -> None:
    """After a successful save(), no .tmp.* files remain in the directory."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    src.save(Settings(interval_minutes=3))

    leftover = list(tmp_path.glob("*.tmp.*"))
    assert leftover == [], f"Unexpected temp files after save: {leftover}"


# ---------------------------------------------------------------------------
# 3. save() updates _current optimistically
# ---------------------------------------------------------------------------


def test_save_updates_current_optimistically(tmp_path: Path) -> None:
    """After save(), current() immediately returns the new Settings."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    original = src.current()
    assert original.interval_minutes != 9  # sanity

    new_settings = Settings(interval_minutes=9)
    src.save(new_settings)

    assert src.current().interval_minutes == 9


# ---------------------------------------------------------------------------
# 4. save() updates content-hash so _do_reload() is a no-op (dedup)
# ---------------------------------------------------------------------------


def test_save_dedup_prevents_double_subscriber_notification(tmp_path: Path) -> None:
    """The watchdog self-event after save() does NOT re-deliver to subscribers.

    After save() the content-hash is already recorded.  Calling _do_reload()
    (simulating the inotify event) should detect the identical hash and return
    without invoking subscriber callbacks a second time.
    """
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    new_settings = Settings(interval_minutes=11)
    src.save(new_settings)

    received: list[Settings] = []
    with src.subscribe(received.append):
        # Simulate the self-triggered watchdog event arriving after save().
        src._do_reload()

    # Dedup must suppress this reload entirely — no callback delivered.
    assert received == [], (
        "Subscriber was notified by the self-reload after save() — "
        "content-hash dedup should have silenced it."
    )


# ---------------------------------------------------------------------------
# 5. Subscriber IS notified when an external change arrives after save()
# ---------------------------------------------------------------------------


def test_external_change_after_save_notifies_subscriber(tmp_path: Path) -> None:
    """After save(), an external write with different content still notifies subscribers."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    # First save
    src.save(Settings(interval_minutes=5))

    # Now simulate an external edit with different content
    external_settings = Settings(interval_minutes=20)
    cfg.write_bytes(external_settings.model_dump_json(indent=2).encode())

    received: list[Settings] = []
    with src.subscribe(received.append):
        src._do_reload()

    assert len(received) == 1
    assert received[0].interval_minutes == 20


# ---------------------------------------------------------------------------
# 6. save() cleans up temp file on write error and re-raises
# ---------------------------------------------------------------------------


def test_save_cleans_up_temp_on_error(tmp_path: Path) -> None:
    """If os.replace fails, save() removes the temp file and re-raises."""
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    def failing_replace(src_path: Any, dst_path: Any) -> None:
        raise OSError("simulated replace failure")

    with patch("fis_monitor.infra.config_source.os.replace", side_effect=failing_replace), \
            pytest.raises(OSError, match="simulated replace failure"):
        src.save(Settings(interval_minutes=3))

    # No temp files should remain
    leftover = list(tmp_path.glob("*.tmp.*"))
    assert leftover == [], f"Temp file not cleaned up after error: {leftover}"


# ---------------------------------------------------------------------------
# 7. save() is thread-safe under concurrent reads
# ---------------------------------------------------------------------------


def test_save_thread_safe_concurrent_reads(tmp_path: Path) -> None:
    """Concurrent current() reads during save() never see a partially-written state.

    We fire save() from one thread and read current() from several others.
    The result must always be either the old or the new Settings — never None
    or a partially constructed object.
    """
    cfg = tmp_path / "config.json"
    src = _make_source(cfg)

    old_settings = Settings(interval_minutes=1)
    new_settings = Settings(interval_minutes=2)
    src.save(old_settings)  # establish baseline

    errors: list[Exception] = []
    valid_interval_minutes = {1, 2}

    def reader() -> None:
        for _ in range(50):
            try:
                s = src.current()
                assert s.interval_minutes in valid_interval_minutes, (
                    f"Unexpected interval_minutes={s.interval_minutes!r} during concurrent read"
                )
            except Exception as exc:
                errors.append(exc)

    def writer() -> None:
        src.save(new_settings)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    writer_thread = threading.Thread(target=writer)

    for t in threads:
        t.start()
    writer_thread.start()

    for t in threads:
        t.join(timeout=5.0)
    writer_thread.join(timeout=5.0)

    assert errors == [], f"Reader errors during concurrent save: {errors}"
