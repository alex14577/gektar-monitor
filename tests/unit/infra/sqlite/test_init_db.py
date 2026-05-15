"""Tests for init_db() pre-flight PRAGMA user_version check.

TDD: RED -> GREEN -> REFACTOR
Covers: fresh DB, up-to-date no-op, newer DB error, older DB with/without runner,
        legacy zero-version with tables, error hierarchy, PII safety.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from fis_monitor.domain.errors import DomainError, MigrationRequired
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = Path(__file__).parent.parent.parent.parent.parent / "docs" / "db" / "schema.sql"


def _load_schema() -> str:
    return _SCHEMA_SQL.read_text(encoding="utf-8")


def _make_provider(tmp_path: Path, name: str = "state.db") -> ConnectionProvider:
    return ConnectionProvider(db_path=tmp_path / name)


def _pragma(conn: sqlite3.Connection, name: str) -> Any:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return row[0]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Test 1: fresh DB gets schema applied
# ---------------------------------------------------------------------------


def test_init_db_fresh_db_applies_schema(tmp_path: Path) -> None:
    """Empty DB file → init_db() applies schema_sql, user_version == 3,
    expected tables exist."""
    schema_sql = _load_schema()
    provider = _make_provider(tmp_path)

    init_db(provider, schema_sql=schema_sql, latest_version=3)

    conn = provider.get()
    assert _pragma(conn, "user_version") == 3
    tables = _table_names(conn)
    assert "notifications" in tables, "notifications table must be created"
    assert "smtp_credentials" in tables, "smtp_credentials table must be created"
    assert "lots" in tables, "lots table must be created"
    provider.close_all()


# ---------------------------------------------------------------------------
# Test 2: already up-to-date → no-op
# ---------------------------------------------------------------------------


def test_init_db_already_current_is_noop(tmp_path: Path) -> None:
    """DB already at user_version=3 → init_db() is a no-op (no schema applied,
    no error raised)."""
    schema_sql = _load_schema()
    provider = _make_provider(tmp_path)

    # Bootstrap: apply schema once
    init_db(provider, schema_sql=schema_sql, latest_version=3)

    # Second call must be silent (no error, no state change)
    init_db(provider, schema_sql=schema_sql, latest_version=3)

    conn = provider.get()
    assert _pragma(conn, "user_version") == 3
    provider.close_all()


# ---------------------------------------------------------------------------
# Test 3: DB with user_version > latest → RuntimeError, no paths in message
# ---------------------------------------------------------------------------


def test_init_db_newer_raises_runtime_error(tmp_path: Path) -> None:
    """DB with user_version=99 > latest_version=2 → RuntimeError with 'newer than app'."""
    db_path = tmp_path / "state.db"
    # Manually stamp user_version=99
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    provider = _make_provider(tmp_path)

    with pytest.raises(RuntimeError, match="newer than app"):
        init_db(provider, schema_sql="", latest_version=2)

    provider.close_all()


def test_init_db_pii_safety_no_paths_in_runtime_error(tmp_path: Path) -> None:
    """RuntimeError message for 'newer than app' must NOT contain absolute paths —
    only version numbers."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    provider = _make_provider(tmp_path)

    try:
        init_db(provider, schema_sql="", latest_version=2)
        pytest.fail("Expected RuntimeError was not raised")
    except RuntimeError as exc:
        msg = str(exc)
        assert "/" not in msg, f"Message must not contain '/' (path separator): {msg!r}"
        assert "\\" not in msg, f"Message must not contain backslash (path separator): {msg!r}"
        assert str(db_path) not in msg, f"Path must not appear in message: {msg!r}"
        # Version numbers must be present
        assert "99" in msg
        assert "2" in msg
    finally:
        provider.close_all()


# ---------------------------------------------------------------------------
# Test 4: older version, no runner → MigrationRequired
# ---------------------------------------------------------------------------


def test_init_db_older_no_runner_raises_migration_required(tmp_path: Path) -> None:
    """DB with user_version=1, no migration_runner → MigrationRequired."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    provider = _make_provider(tmp_path)

    with pytest.raises(MigrationRequired) as exc_info:
        init_db(provider, schema_sql="", latest_version=2, migration_runner=None)

    exc = exc_info.value
    assert exc.from_version == 1
    assert exc.to_version == 2
    provider.close_all()


# ---------------------------------------------------------------------------
# Test 5: older version with runner → runner invoked exactly once
# ---------------------------------------------------------------------------


def test_init_db_older_with_runner_invokes_runner(tmp_path: Path) -> None:
    """DB with user_version=1 + migration_runner → runner called with (conn, 1, 2)."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    calls: list[tuple[sqlite3.Connection, int, int]] = []

    def fake_runner(conn: sqlite3.Connection, from_v: int, to_v: int) -> None:
        calls.append((conn, from_v, to_v))
        # A real runner must stamp user_version on completion.
        conn.execute(f"PRAGMA user_version = {to_v}")

    provider = _make_provider(tmp_path)
    init_db(provider, schema_sql="", latest_version=2, migration_runner=fake_runner)

    assert len(calls) == 1, f"runner must be called exactly once, got {len(calls)}"
    assert calls[0][1] == 1, f"from_version must be 1, got {calls[0][1]}"
    assert calls[0][2] == 2, f"to_version must be 2, got {calls[0][2]}"
    assert isinstance(calls[0][0], sqlite3.Connection), "first arg must be Connection"
    provider.close_all()


# ---------------------------------------------------------------------------
# Test 6: user_version=0 but tables exist → legacy DB → MigrationRequired
# ---------------------------------------------------------------------------


def test_init_db_legacy_zero_with_tables_raises_migration_required(tmp_path: Path) -> None:
    """DB with user_version=0 but existing tables (legacy) → MigrationRequired,
    not schema apply."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    # user_version stays 0, but a table exists (legacy DB)
    conn.execute("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    provider = _make_provider(tmp_path)

    with pytest.raises(MigrationRequired) as exc_info:
        init_db(provider, schema_sql="SELECT 1", latest_version=2, migration_runner=None)

    exc = exc_info.value
    assert exc.from_version == 0
    assert exc.to_version == 2
    provider.close_all()


# ---------------------------------------------------------------------------
# Test 6b: runner that does NOT stamp user_version → RuntimeError
# ---------------------------------------------------------------------------


def test_init_db_runner_did_not_stamp_user_version_raises(tmp_path: Path) -> None:
    """DB with user_version=1, runner is a no-op (does not stamp user_version) →
    init_db() must raise RuntimeError containing 'user_version is 1, expected 2'.
    Message must NOT contain any file path (PII-safe)."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    # No-op runner: does nothing, does not advance user_version.
    noop_runner = lambda conn, from_v, to_v: None  # noqa: E731

    provider = _make_provider(tmp_path)

    try:
        with pytest.raises(RuntimeError) as exc_info:
            init_db(provider, schema_sql="", latest_version=2, migration_runner=noop_runner)

        msg = str(exc_info.value)
        # Must mention the actual version left behind and the expected version.
        assert "1" in msg, f"Message must mention actual user_version=1: {msg!r}"
        assert "2" in msg, f"Message must mention expected version=2: {msg!r}"
        # PII-safe: no file paths in the message.
        assert "/" not in msg, f"Message must not contain '/' (path separator): {msg!r}"
        assert "\\" not in msg, f"Message must not contain backslash: {msg!r}"
        assert str(db_path) not in msg, f"Path must not appear in message: {msg!r}"
    finally:
        provider.close_all()


# ---------------------------------------------------------------------------
# Test 7: MigrationRequired is in DomainError hierarchy
# ---------------------------------------------------------------------------


def test_migration_required_exception_in_errors_hierarchy() -> None:
    """MigrationRequired must be a DomainError with from_version/to_version attrs."""
    exc = MigrationRequired(from_version=0, to_version=1)

    assert isinstance(exc, DomainError), "MigrationRequired must be a DomainError"
    assert exc.from_version == 0
    assert exc.to_version == 1

    # Second form with positional args
    exc2 = MigrationRequired(3, 7)
    assert exc2.from_version == 3
    assert exc2.to_version == 7
