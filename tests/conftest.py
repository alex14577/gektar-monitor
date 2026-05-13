"""Project-wide pytest fixtures.

Provides:
- `schema_sql`: canonical schema.sql contents (session-scoped, read once).
- `tmp_db_path`: per-test path inside pytest's auto-cleaned tmp_path.
- `tmp_db`:     per-test ConnectionProvider with the full schema applied.

WAL mode is applied per-connection by `ConnectionProvider._configure` (ADR-007).
The schema (including `PRAGMA user_version = 2`) is applied by `init_db()`.

Function-scope by design — every test gets an isolated DB file. This avoids
flaky test ordering caused by leaked writer state.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.init_db import init_db

# Canonical schema location — repo-rooted, robust against test-tree moves.
SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "db" / "schema.sql"
)


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
def tmp_db(
    tmp_db_path: Path, schema_sql: str
) -> Iterator[ConnectionProvider]:
    """Per-test SQLite DB with the full schema applied.

    Yields a `ConnectionProvider` — tests grab connections via
    `provider.get_connection()`. The provider is closed in teardown so
    no sqlite3 handles leak (important on Windows where unclosed handles
    block tmp_path cleanup).
    """
    provider = ConnectionProvider(db_path=tmp_db_path)
    try:
        init_db(provider, schema_sql=schema_sql)
        yield provider
    finally:
        provider.close_all()
