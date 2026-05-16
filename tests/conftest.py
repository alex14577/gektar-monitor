"""Project-wide pytest fixtures.

Provides:
- `schema_sql`: canonical schema.sql contents (session-scoped, read once).
- `tmp_db_path`: per-test path inside pytest's auto-cleaned tmp_path.
- `tmp_db`:     per-test ConnectionProvider with the full schema applied.
- `reset_fis_monitor_logger`: autouse fixture that resets the fis_monitor
  logger handlers after each test so logging tests do not bleed into each
  other (plg.1).

WAL mode is applied per-connection by `ConnectionProvider._configure` (ADR-007).
The schema (including `PRAGMA user_version = 2`) is applied by `init_db()`.

Function-scope by design — every test gets an isolated DB file. This avoids
flaky test ordering caused by leaked writer state.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import LATEST_SCHEMA_VERSION, init_db
from fis_monitor.utils.log import _AUDIT_DISABLED_ATTR

# Canonical schema location — repo-rooted, robust against test-tree moves.
SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "docs" / "db" / "schema.sql"

# ---------------------------------------------------------------------------
# Logger isolation — keeps logging tests hermetic
# ---------------------------------------------------------------------------

_FIS_MONITOR_LOGGER = "fis_monitor"
# Child loggers that also get handlers installed by setup_logging (plg.3).
_CHILD_LOGGERS = ("fis_monitor.audit", "fis_monitor.requests")


@pytest.fixture(autouse=True)
def reset_fis_monitor_logger() -> Iterator[None]:
    """Reset the ``fis_monitor`` logger family to a clean state after every test.

    Removes all handlers and re-enables propagation so that log output from
    one test cannot affect another. This is especially important for tests in
    ``tests/unit/utils/test_log.py`` that install handlers via ``setup_logging``,
    and for plg.3 file-channel tests (audit / requests child loggers).
    """
    import contextlib

    yield
    for logger_name in (_FIS_MONITOR_LOGGER, *_CHILD_LOGGERS):
        lg = logging.getLogger(logger_name)
        for handler in list(lg.handlers):
            lg.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        lg.propagate = True
        # Clear fail-closed sentinel if present (plg.3 audit channel).
        # Uses the imported constant so a rename doesn't silently break this.
        if hasattr(lg, _AUDIT_DISABLED_ATTR):
            setattr(lg, _AUDIT_DISABLED_ATTR, False)


@pytest.fixture(scope="session")
def schema_sql() -> str:
    """Cached schema.sql contents (read once per test session)."""
    if not SCHEMA_SQL_PATH.exists():
        raise FileNotFoundError(
            f"Canonical schema.sql not found at {SCHEMA_SQL_PATH}. "
            "Run pytest from the repo root, or update SCHEMA_SQL_PATH."
        )
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Per-test DB file path under pytest's auto-cleaned tmp_path."""
    return tmp_path / "state.db"


@pytest.fixture
def tmp_db(tmp_db_path: Path, schema_sql: str) -> Iterator[ConnectionProvider]:
    """Per-test SQLite DB with the full schema applied.

    Yields a `ConnectionProvider` — tests grab connections via
    `provider.get()`. The provider is closed in teardown so
    no sqlite3 handles leak (important on Windows where unclosed handles
    block tmp_path cleanup).
    """
    provider = ConnectionProvider(db_path=tmp_db_path)
    try:
        init_db(provider, schema_sql=schema_sql, latest_version=LATEST_SCHEMA_VERSION)
        yield provider
    finally:
        provider.close_all()
