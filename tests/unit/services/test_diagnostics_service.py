"""Unit tests for DiagnosticsService (bd issue a4t.7).

Acceptance criteria:
  #20 test_build_zip_happy_path      — schema matches, all allowed files included,
      audit_included=True.
  #21 test_schema_drift_fail_closed  — extra column → ok=False, schema_ok=False,
      generic ui_message.
  #22 test_audit_excluded_on_cloud_sync — detector returns True → audit.jsonl NOT in zip.
  #23 test_zip_excludes_dmp_and_core_files — crash.dmp / core.12345 never in zip.
  test_zip_only_allowlist_included   — secret.txt in data_dir → NOT in zip.

Design: uses tmp_path + real sqlite3 connections for schema tests (no mocking);
CloudSyncDetector is exercised via a simple stub (no mock library needed).
"""

from __future__ import annotations

import logging
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fis_monitor.services.diagnostics.exclude_policy import DiagnosticsExcludePolicy
from fis_monitor.services.diagnostics.service import (
    _GENERIC_UI_MESSAGE,
    CloudSyncDetector,
    DefaultCloudSyncDetector,
    DiagnosticsService,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class _FakeConnectionProvider:
    """Minimal ConnectionProvider stub backed by an in-memory SQLite DB."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is not None:
            self._conn = sqlite3.connect(str(db_path))
        else:
            self._conn = sqlite3.connect(":memory:")

    def get(self) -> sqlite3.Connection:
        return self._conn

    def close_all(self) -> None:
        self._conn.close()


class _FakeClock:
    """Minimal Clock stub."""

    def now(self) -> Any:
        from datetime import UTC, datetime

        return datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


class _AlwaysCloudSynced:
    """CloudSyncDetector stub that always returns True."""

    def is_cloud_synced(self, data_dir: Path) -> bool:
        return True


class _NeverCloudSynced:
    """CloudSyncDetector stub that always returns False."""

    def is_cloud_synced(self, data_dir: Path) -> bool:
        return False


def _create_minimal_schema(conn: sqlite3.Connection) -> None:
    """Create all tables declared in DIAGNOSTIC_SCHEMA_V1 with the exact columns."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lots (
            id INTEGER PRIMARY KEY,
            cadastral_no TEXT NOT NULL,
            area_sqm INTEGER,
            region TEXT NOT NULL,
            municipality TEXT,
            land_category TEXT,
            permitted_use TEXT,
            ogv TEXT,
            status TEXT NOT NULL,
            date_create TIMESTAMP NOT NULL,
            date_update TIMESTAMP,
            lat REAL,
            lon REAL,
            has_boundaries INTEGER,
            parser_version INTEGER NOT NULL DEFAULT 1,
            first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            detail_fetched_at TIMESTAMP,
            enrichment_status TEXT,
            enrichment_retries INTEGER NOT NULL DEFAULT 0,
            last_seen_at TIMESTAMP,
            last_status TEXT,
            last_status_at TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            inactive_reason TEXT,
            inactive_since TIMESTAMP,
            inactive_confirmed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region INTEGER NOT NULL,
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            status TEXT NOT NULL,
            lots_fetched INTEGER NOT NULL DEFAULT 0,
            new_lots INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            id_schema_check TEXT NOT NULL DEFAULT 'ok'
        );
        CREATE TABLE IF NOT EXISTS notifications (
            lot_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            recipient TEXT NOT NULL,
            sent_at TIMESTAMP,
            PRIMARY KEY (lot_id, channel, recipient)
        );
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def _build_service(
    tmp_path: Path,
    db_path: Path,
    *,
    cloud_sync_detector: CloudSyncDetector | None = None,
) -> DiagnosticsService:
    conn_provider = _FakeConnectionProvider(db_path=db_path)
    return DiagnosticsService(
        data_dir=tmp_path,
        conn_provider=conn_provider,
        clock=_FakeClock(),
        exclude_policy=DiagnosticsExcludePolicy(),
        cloud_sync_detector=cloud_sync_detector or _NeverCloudSynced(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Return a tmp data_dir populated with typical files."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    _create_minimal_schema(conn)
    conn.close()

    (tmp_path / "app.jsonl").write_text('{"event": "start"}\n')
    (tmp_path / "audit.jsonl").write_text('{"event": "audit"}\n')
    return tmp_path


# ---------------------------------------------------------------------------
# Test #20: happy path
# ---------------------------------------------------------------------------


def test_build_zip_happy_path(data_dir: Path, tmp_path: Path) -> None:
    """Schema matches → zip created with state.db, app.jsonl, audit.jsonl included."""
    db_path = data_dir / "state.db"
    service = _build_service(data_dir, db_path, cloud_sync_detector=_NeverCloudSynced())
    output = tmp_path / "diag.zip"

    result = service.build_zip(output)

    assert result.ok is True
    assert result.schema_ok is True
    assert result.audit_included is True
    assert output.exists()

    with zipfile.ZipFile(output, "r") as zf:
        names = set(zf.namelist())

    assert "state.db" in names
    assert "app.jsonl" in names
    assert "audit.jsonl" in names


# ---------------------------------------------------------------------------
# Test #21: schema drift — fail-closed
# ---------------------------------------------------------------------------


def test_schema_drift_fail_closed(data_dir: Path, tmp_path: Path) -> None:
    """Extra column in live DB → ok=False, schema_ok=False, generic ui_message, no zip."""
    db_path = data_dir / "state.db"
    # Add an extra column to cycles — drift from DIAGNOSTIC_SCHEMA_V1
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE cycles ADD COLUMN secret_field TEXT")
    conn.commit()
    conn.close()

    service = _build_service(data_dir, db_path)
    output = tmp_path / "diag.zip"

    result = service.build_zip(output)

    assert result.ok is False
    assert result.schema_ok is False
    assert result.ui_message == _GENERIC_UI_MESSAGE
    # Generic message must NOT leak internal details
    assert "secret_field" not in result.ui_message
    assert "cycles" not in result.ui_message
    assert str(data_dir) not in result.ui_message
    # Zip must NOT be created: fail-closed means no file I/O when schema drifts.
    # We assert on result flags (primary) and filesystem (secondary) to verify both
    # the contract and the atomic write guarantee.
    assert not output.exists(), (
        "zip must not be created when schema_ok=False (fail-closed, R3-M5)"
    )


# ---------------------------------------------------------------------------
# Test #22: cloud-sync — audit.jsonl excluded
# ---------------------------------------------------------------------------


def test_audit_excluded_on_cloud_sync(
    data_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cloud-sync detected → audit.jsonl NOT in zip, audit_included=False, warning logged."""
    db_path = data_dir / "state.db"
    service = _build_service(data_dir, db_path, cloud_sync_detector=_AlwaysCloudSynced())
    output = tmp_path / "diag.zip"

    with caplog.at_level(logging.WARNING, logger="fis_monitor.services.diagnostics.service"):
        result = service.build_zip(output)

    assert result.ok is True
    assert result.audit_included is False

    with zipfile.ZipFile(output, "r") as zf:
        names = set(zf.namelist())

    assert "audit.jsonl" not in names
    # Other files are still present
    assert "app.jsonl" in names

    # Warning must have been emitted
    assert any("cloud_sync_detected" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Test #23: *.dmp and core.* excluded unconditionally
# ---------------------------------------------------------------------------


def test_zip_excludes_dmp_and_core_files(data_dir: Path, tmp_path: Path) -> None:
    """crash.dmp and core.12345 must never appear in the zip."""
    db_path = data_dir / "state.db"
    (data_dir / "crash.dmp").write_bytes(b"\x00" * 16)
    (data_dir / "core.12345").write_bytes(b"\x00" * 16)

    service = _build_service(data_dir, db_path)
    output = tmp_path / "diag.zip"

    result = service.build_zip(output)

    assert result.ok is True
    with zipfile.ZipFile(output, "r") as zf:
        names = set(zf.namelist())

    assert "crash.dmp" not in names
    assert "core.12345" not in names


# ---------------------------------------------------------------------------
# Test: allow-list — unknown files excluded
# ---------------------------------------------------------------------------


def test_zip_only_allowlist_included(data_dir: Path, tmp_path: Path) -> None:
    """secret.txt in data_dir must NOT appear in the zip (not in allow-list)."""
    db_path = data_dir / "state.db"
    (data_dir / "secret.txt").write_text("top secret")

    service = _build_service(data_dir, db_path)
    output = tmp_path / "diag.zip"

    result = service.build_zip(output)

    assert result.ok is True
    with zipfile.ZipFile(output, "r") as zf:
        names = set(zf.namelist())

    assert "secret.txt" not in names


# ---------------------------------------------------------------------------
# Bonus: DefaultCloudSyncDetector path heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path_str", "expected"),
    [
        ("/home/user/Dropbox/data", True),
        ("/Users/alice/Library/Mobile Documents/com~apple~CloudDocs/iCloud/data", True),
        ("/home/user/OneDrive/data", True),
        ("C:\\Users\\alice\\Google Drive\\data", True),
        ("C:\\Users\\alice\\GoogleDrive\\data", True),
        ("/home/user/.local/share/fis_monitor/data", False),
        ("/tmp/test_data", False),
    ],
)
def test_default_cloud_sync_detector(path_str: str, expected: bool) -> None:
    detector = DefaultCloudSyncDetector()
    assert detector.is_cloud_synced(Path(path_str)) is expected
