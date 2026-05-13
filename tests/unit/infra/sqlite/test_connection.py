"""Tests for ConnectionProvider (per-thread SQLite, connection tracking, PRAGMA).

TDD: RED -> GREEN -> REFACTOR
"""

from __future__ import annotations

import gc
import sqlite3
import threading
from pathlib import Path

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider

# ---------------------------------------------------------------------------
# Test 1: per-thread isolation -- two threads get different Connection objects
# ---------------------------------------------------------------------------

def test_per_thread_isolation(tmp_path: Path) -> None:
    """Two threads must receive distinct sqlite3.Connection instances."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")
    connections: list[sqlite3.Connection] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            conn = provider.get_connection()
            connections.append(conn)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Thread errors: {errors}"
    assert len(connections) == 2
    assert connections[0] is not connections[1], (
        "Each thread must have its own Connection"
    )


# ---------------------------------------------------------------------------
# Test 2: PRAGMA settings applied per-connection
# ---------------------------------------------------------------------------

def _pragma(conn: sqlite3.Connection, name: str) -> object:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return row[0]


def test_pragma_settings(tmp_path: Path) -> None:
    """PRAGMA values must match the spec (ADR-007)."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")
    conn = provider.get_connection()

    # journal_mode: WAL (persistent, but safe to check)
    assert _pragma(conn, "journal_mode") == "wal"

    # synchronous: 1 = NORMAL
    assert _pragma(conn, "synchronous") == 1

    # foreign_keys: 0 = OFF (per ADR-007 per-connection list)
    assert _pragma(conn, "foreign_keys") == 0

    # busy_timeout: 5000 ms
    assert _pragma(conn, "busy_timeout") == 5000

    # auto_vacuum: 2 = INCREMENTAL (persistent, set at schema init time)
    # We set it per-connection too (safe to call each time).
    assert _pragma(conn, "auto_vacuum") == 2


# ---------------------------------------------------------------------------
# Test 3: close_all() closes all live connections; new get_connection() works
# ---------------------------------------------------------------------------

def test_close_all_then_reconnect(tmp_path: Path) -> None:
    """close_all() closes every tracked connection; subsequent get_connection()
    creates a fresh one."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")

    conn = provider.get_connection()
    # confirm connection is alive
    conn.execute("SELECT 1").fetchone()

    provider.close_all()

    # Old connection should be closed -- operations must raise
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1").fetchone()

    # A new get_connection() must return a working connection
    new_conn = provider.get_connection()
    result = new_conn.execute("SELECT 1").fetchone()
    assert result == (1,)


# ---------------------------------------------------------------------------
# Test 4: Registry does not retain dead references after clear
# ---------------------------------------------------------------------------

def test_registry_no_dead_refs(tmp_path: Path) -> None:
    """After _clear_thread_local() the connection is removed from the internal
    registry -- the provider does not hold a lingering strong reference."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")

    conn = provider.get_connection()
    conn_id = id(conn)

    # Verify connection is tracked
    with provider._lock:
        assert conn_id in provider._registry

    # Clear the thread-local slot -- drops the strong reference from provider
    provider._clear_thread_local()
    del conn
    gc.collect()

    # Registry must no longer contain the entry
    with provider._lock:
        assert conn_id not in provider._registry, (
            "Registry should release the connection after _clear_thread_local"
        )


# ---------------------------------------------------------------------------
# Test 5: same thread always gets the same connection (idempotency)
# ---------------------------------------------------------------------------

def test_same_thread_same_connection(tmp_path: Path) -> None:
    """Calling get_connection() twice in the same thread returns the identical
    object."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")
    conn1 = provider.get_connection()
    conn2 = provider.get_connection()
    assert conn1 is conn2
