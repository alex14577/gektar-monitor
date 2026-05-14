"""Tests for ConnectionProvider (per-thread SQLite, connection tracking, PRAGMA).

TDD: RED -> GREEN -> REFACTOR
"""

from __future__ import annotations

import gc
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

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
            conn = provider.get()
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
    conn = provider.get()

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

    # temp_store: 2 = MEMORY
    assert _pragma(conn, "temp_store") == 2

    # cache_size: -20000 (~20 MiB page cache)
    assert _pragma(conn, "cache_size") == -20000

    # mmap_size: 256 MiB = 268435456
    assert _pragma(conn, "mmap_size") == 268435456

    # wal_autocheckpoint: 1000 (defence-in-depth duplicate, ADR-007 R4-minor)
    assert _pragma(conn, "wal_autocheckpoint") == 1000


# ---------------------------------------------------------------------------
# Test 3: close_all() closes provider permanently; subsequent get raises
# ---------------------------------------------------------------------------

def test_close_all_then_get_raises_runtime_error(tmp_path: Path) -> None:
    """close_all() is a terminal operation; get() must raise
    RuntimeError afterwards in the same thread."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")

    conn = provider.get()
    # confirm connection is alive
    conn.execute("SELECT 1").fetchone()

    provider.close_all()

    # Old connection should be closed -- operations must raise
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1").fetchone()

    # get() in any thread must now raise RuntimeError
    with pytest.raises(RuntimeError, match="closed"):
        provider.get()


# ---------------------------------------------------------------------------
# Test 3b: cross-thread close_all() invalidates provider for all threads
# ---------------------------------------------------------------------------

def test_close_all_from_other_thread_invalidates_provider(tmp_path: Path) -> None:
    """close_all from a different thread must close registered connections
    and make subsequent get() in any thread raise."""
    provider = ConnectionProvider(tmp_path / "test.db")

    # Thread A creates a connection
    conn_a = provider.get()
    conn_a.execute("CREATE TABLE t (id INTEGER)")

    # Thread B (shutdown thread) closes everything
    shutdown_thread = threading.Thread(target=provider.close_all)
    shutdown_thread.start()
    shutdown_thread.join(timeout=2.0)

    # Thread A's stale conn must not be usable; new get() must raise.
    with pytest.raises(RuntimeError, match="closed"):
        provider.get()


# ---------------------------------------------------------------------------
# Test 4: Registry does not retain dead references after clear
# ---------------------------------------------------------------------------

def test_registry_no_dead_refs(tmp_path: Path) -> None:
    """After _clear_thread_local() the connection is removed from the internal
    registry -- the provider does not hold a lingering strong reference."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")

    conn = provider.get()
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
    """Calling get() twice in the same thread returns the identical
    object."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")
    conn1 = provider.get()
    conn2 = provider.get()
    assert conn1 is conn2


# ---------------------------------------------------------------------------
# Test 6: registry-leak guard -- _configure failure must not leak connection
# ---------------------------------------------------------------------------

def test_configure_failure_does_not_leak_connection(tmp_path: Path) -> None:
    """If _configure raises, the raw connection must be closed immediately
    and NOT registered in _registry."""
    provider = ConnectionProvider(db_path=tmp_path / "state.db")

    closed_conns: list[sqlite3.Connection] = []

    def failing_configure(conn: sqlite3.Connection) -> None:
        # Track the connection object so we can assert it was closed
        closed_conns.append(conn)
        raise RuntimeError("simulated configure failure")

    with patch.object(provider, "_configure", failing_configure), pytest.raises(
        RuntimeError, match="simulated configure failure"
    ):
        provider.get()

    # Registry must be empty — no leaked entry
    with provider._lock:
        assert len(provider._registry) == 0, (
            "Registry must not contain a connection that failed to configure"
        )

    # The raw connection must have been closed (operating on it raises)
    assert len(closed_conns) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed_conns[0].execute("SELECT 1")
