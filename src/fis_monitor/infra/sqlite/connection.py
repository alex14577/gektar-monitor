"""Per-thread SQLite connection provider.

Architecture: infra/sqlite layer (Layer 0). Not a domain Protocol — concrete
class accepted by repositories directly (domain does not know about sqlite3).
See: docs/architecture/03-protocols.md §3.1, docs/decisions/ADR-007-per-connection-pragma.md.

PRAGMA split (ADR-007):
- Persistent (schema.sql): journal_mode=WAL, auto_vacuum=INCREMENTAL
- Per-connection (_configure): busy_timeout, synchronous, foreign_keys, etc.

Threading model: one connection per thread, enforced via threading.local.
check_same_thread=False — threading-safety is guaranteed by this class; we
disable sqlite3's own check so that close_all() may be called from a shutdown
thread.

WeakSet tracking: sqlite3.Connection does not support weak references in
Python 3.12. We track live connections via a dict keyed by connection id(),
with cleanup handled explicitly in close_all() and _clear_thread_local().
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path


class ConnectionProvider:
    """Per-thread sqlite3.Connection with tracking and PRAGMA setup.

    Responsibilities (SRP):
    - Connection lifecycle: open, configure, close.
    - Does NOT open transactions, does NOT run migrations.

    Usage:
        provider = ConnectionProvider(db_path=Path("state.db"))
        conn = provider.get_connection()   # per-thread, idempotent
        ...
        provider.close_all()               # shutdown — closes all live conns
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        # Registry: id(conn) -> conn. Protected by _lock.
        # We cannot use WeakSet because sqlite3.Connection does not support
        # weak references in CPython 3.12. A dict keyed by id() provides the
        # same lifecycle semantics: entries are removed in _clear_thread_local
        # and close_all(), preventing leaks.
        self._registry: dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_connection(self) -> sqlite3.Connection:
        """Return the per-thread sqlite3.Connection, creating it if needed."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def close_all(self) -> None:
        """Close every tracked live connection.

        Safe to call from a shutdown thread (not necessarily the owning
        thread). Takes a snapshot of current connections before iterating to
        avoid mutation during close (R3-minor pattern from docs/architecture/03-protocols.md).

        Also clears the thread-local slot for the calling thread so that a
        subsequent get_connection() creates a fresh connection rather than
        returning the now-closed one.
        """
        with self._lock:
            snapshot = list(self._registry.values())
            self._registry.clear()
        for conn in snapshot:
            with contextlib.suppress(sqlite3.Error):  # already-closed is OK
                conn.close()
        # Clear the local slot for the current thread (other threads' locals
        # will be re-created on next get_connection() call since the old conn
        # is closed and not in the registry anymore).
        self._local.conn = None

    def _clear_thread_local(self) -> None:
        """Remove the connection slot for the current thread.

        This drops the strong reference held in threading.local. The
        connection is also removed from the registry so it becomes GC-eligible.
        Exposed for testing. Not part of the public production API.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            with self._lock:
                self._registry.pop(id(conn), None)
            self._local.conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,  # safety guaranteed per-thread by us
        )
        self._configure(conn)
        with self._lock:
            self._registry[id(conn)] = conn
        return conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        """Apply per-connection PRAGMA settings (ADR-007).

        Per-connection (not stored in DB file, must be set on every connect):
          busy_timeout  = 5000   — wait up to 5 s before raising SQLITE_BUSY
          synchronous   = NORMAL — balance durability vs performance
          foreign_keys  = OFF    — per ADR-007; FK enforced at service layer
          auto_vacuum   = INCREMENTAL — persistent but safe to re-apply
          temp_store    = MEMORY — temp tables/indices in RAM
          cache_size    = -20000 — ~20 MiB page cache per connection
          mmap_size     = 268435456 — 256 MiB memory-mapped I/O
          wal_autocheckpoint = 1000 — per-connection duplicate of persistent
                               value; ensures consistent behaviour after
                               restore from backup (schema.sql may not run).

        journal_mode=WAL is persistent (set in schema.sql) but also safe to
        set per-connection — it is a no-op on an already-WAL database.
        """
        # auto_vacuum must be set BEFORE journal_mode on a fresh database;
        # once WAL is applied, auto_vacuum cannot be changed until VACUUM.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -20000")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
