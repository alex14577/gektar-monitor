"""Tests for SqliteMigrationRunner — Protocol impl + TOCTOU re-check + chain build."""

import sqlite3
from pathlib import Path

import pytest

from fis_monitor.domain.errors import (
    ConcurrentMigrationError,
    DomainError,
    MigrationChainBroken,
)
from fis_monitor.domain.interfaces import MigrationRunner
from fis_monitor.infra.sqlite.migrations import (
    Migration,
    SqliteMigrationRunner,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(path: Path, user_version: int = 0) -> sqlite3.Connection:
    """Open a fresh sqlite3.Connection at the given user_version."""
    conn = sqlite3.connect(path)
    if user_version != 0:
        conn.execute(f"PRAGMA user_version = {user_version}")
    return conn


def _uv(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------------------------------
# Empty-runner behaviour
# ---------------------------------------------------------------------------


def test_empty_runner_list_is_empty() -> None:
    runner = SqliteMigrationRunner()
    assert list(runner.list_migrations()) == []


def test_empty_runner_noop_when_versions_match(tmp_path: Path) -> None:
    """from_version == to_version on empty runner → commits + returns silently."""
    conn = _open(tmp_path / "db.sqlite", user_version=2)
    runner = SqliteMigrationRunner()
    runner.run_pending(conn, from_version=2, to_version=2)
    assert _uv(conn) == 2


def test_empty_runner_raises_chain_broken_when_versions_differ(
    tmp_path: Path,
) -> None:
    conn = _open(tmp_path / "db.sqlite", user_version=1)
    runner = SqliteMigrationRunner()
    with pytest.raises(MigrationChainBroken) as ei:
        runner.run_pending(conn, from_version=1, to_version=2)
    assert ei.value.from_version == 1
    assert ei.value.to_version == 2
    # Rollback executed — user_version unchanged.
    assert _uv(conn) == 1


# ---------------------------------------------------------------------------
# TOCTOU re-check (bd 1zk)
# ---------------------------------------------------------------------------


def test_toctou_mismatch_raises_concurrent_migration_error(tmp_path: Path) -> None:
    """run_pending re-reads user_version after BEGIN IMMEDIATE; mismatch → error."""
    conn = _open(tmp_path / "db.sqlite", user_version=3)
    runner = SqliteMigrationRunner(
        migrations=[Migration(1, 2, apply=lambda c: None)]
    )
    with pytest.raises(ConcurrentMigrationError) as ei:
        runner.run_pending(conn, from_version=1, to_version=2)
    assert ei.value.expected_version == 1
    assert ei.value.actual_version == 3
    # State unchanged.
    assert _uv(conn) == 3


def test_concurrent_migration_error_is_domain_error() -> None:
    err = ConcurrentMigrationError(expected_version=1, actual_version=2)
    assert isinstance(err, DomainError)
    # PII contract: only integers in message.
    msg = str(err)
    assert "1" in msg and "2" in msg
    assert "/" not in msg  # no paths
    assert ".db" not in msg


def test_migration_chain_broken_is_domain_error() -> None:
    err = MigrationChainBroken(from_version=1, to_version=3)
    assert isinstance(err, DomainError)
    assert "1" in str(err) and "3" in str(err)


# ---------------------------------------------------------------------------
# Successful single-step migration
# ---------------------------------------------------------------------------


def test_single_step_migration_applies_and_updates_user_version(
    tmp_path: Path,
) -> None:
    conn = _open(tmp_path / "db.sqlite", user_version=1)

    def _create_foo(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE foo (x INTEGER)")

    runner = SqliteMigrationRunner(
        migrations=[Migration(1, 2, apply=_create_foo)]
    )
    runner.run_pending(conn, from_version=1, to_version=2)

    assert _uv(conn) == 2
    # Table created — visible after commit.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "foo" in tables


def test_multi_step_chain_applies_all_migrations(tmp_path: Path) -> None:
    conn = _open(tmp_path / "db.sqlite", user_version=1)

    runner = SqliteMigrationRunner(
        migrations=[
            Migration(1, 2, apply=lambda c: c.execute("CREATE TABLE a (x INT)")),
            Migration(2, 3, apply=lambda c: c.execute("CREATE TABLE b (x INT)")),
        ]
    )
    runner.run_pending(conn, from_version=1, to_version=3)

    assert _uv(conn) == 3
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"a", "b"} <= tables


# ---------------------------------------------------------------------------
# Failure rollback
# ---------------------------------------------------------------------------


def test_failure_mid_chain_rolls_back_all_changes(tmp_path: Path) -> None:
    """Second migration raises → first migration's side-effects also rolled back."""
    conn = _open(tmp_path / "db.sqlite", user_version=1)

    def _boom(_c: sqlite3.Connection) -> None:
        raise RuntimeError("boom")

    runner = SqliteMigrationRunner(
        migrations=[
            Migration(1, 2, apply=lambda c: c.execute("CREATE TABLE keep (x INT)")),
            Migration(2, 3, apply=_boom),
        ]
    )
    with pytest.raises(RuntimeError, match="boom"):
        runner.run_pending(conn, from_version=1, to_version=3)

    assert _uv(conn) == 1
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "keep" not in tables


# ---------------------------------------------------------------------------
# Chain-builder edge cases
# ---------------------------------------------------------------------------


def test_chain_broken_when_target_unreachable(tmp_path: Path) -> None:
    """Runner with 1→2 only; request 1→3 → MigrationChainBroken."""
    conn = _open(tmp_path / "db.sqlite", user_version=1)
    runner = SqliteMigrationRunner(
        migrations=[Migration(1, 2, apply=lambda c: None)]
    )
    with pytest.raises(MigrationChainBroken):
        runner.run_pending(conn, from_version=1, to_version=3)
    assert _uv(conn) == 1


def test_chain_broken_when_starting_version_not_registered(tmp_path: Path) -> None:
    """Runner with 2→3 only; request 1→3 → MigrationChainBroken."""
    conn = _open(tmp_path / "db.sqlite", user_version=1)
    runner = SqliteMigrationRunner(
        migrations=[Migration(2, 3, apply=lambda c: None)]
    )
    with pytest.raises(MigrationChainBroken):
        runner.run_pending(conn, from_version=1, to_version=3)
    assert _uv(conn) == 1


def test_duplicate_from_version_rejected_in_constructor() -> None:
    with pytest.raises(ValueError, match="Duplicate migration"):
        SqliteMigrationRunner(
            migrations=[
                Migration(1, 2, apply=lambda c: None),
                Migration(1, 3, apply=lambda c: None),
            ]
        )


def test_list_migrations_sorted_by_from_version() -> None:
    m1 = Migration(2, 3, apply=lambda c: None)
    m2 = Migration(1, 2, apply=lambda c: None)
    runner = SqliteMigrationRunner(migrations=[m1, m2])
    listed = list(runner.list_migrations())
    assert [m.from_version for m in listed] == [1, 2]


# ---------------------------------------------------------------------------
# Callable signature compatibility (init_db expects Callable[[conn, int, int], None])
# ---------------------------------------------------------------------------


def test_runner_callable_signature_matches_init_db(tmp_path: Path) -> None:
    """SqliteMigrationRunner is Callable[[Connection, int, int], None]."""
    conn = _open(tmp_path / "db.sqlite", user_version=1)
    runner = SqliteMigrationRunner(
        migrations=[Migration(1, 2, apply=lambda c: c.execute("CREATE TABLE t (x)"))]
    )
    # Same effect as runner.run_pending(...).
    runner(conn, 1, 2)
    assert _uv(conn) == 2


# ---------------------------------------------------------------------------
# Protocol structural conformance
# ---------------------------------------------------------------------------


def test_sqlite_migration_runner_conforms_to_protocol() -> None:
    """SqliteMigrationRunner has the structural shape of MigrationRunner.

    MigrationRunner is not @runtime_checkable (policy from 531.2) — we
    check the structural attributes manually. This is a smoke test that
    will fail if the Protocol contract drifts.
    """
    runner = SqliteMigrationRunner()
    assert callable(getattr(runner, "list_migrations", None))
    assert callable(getattr(runner, "run_pending", None))
    assert callable(runner)  # __call__ is part of the Protocol
    # Static check — MigrationRunner is the Protocol type imported above.
    # If mypy passes, the structural conformance is verified at type-check
    # time; this assignment guards against accidental attribute removal.
    proto: MigrationRunner = runner  # noqa: F841


# ---------------------------------------------------------------------------
# Integration: init_db wires SqliteMigrationRunner end-to-end
# ---------------------------------------------------------------------------


def test_init_db_invokes_runner_and_verifies_user_version(tmp_path: Path) -> None:
    """init_db passes the runner as Callable, then re-reads user_version.

    Build a v1 DB by hand, run init_db with a runner that upgrades 1→2,
    confirm user_version landed on 2 and the runner's side effects are
    visible.
    """
    from fis_monitor.infra.sqlite.connection import ConnectionProvider
    from fis_monitor.infra.sqlite.init_db import init_db

    db_path = tmp_path / "state.db"
    # Bootstrap a "v1" database: one table + user_version=1.
    boot = sqlite3.connect(db_path)
    boot.execute("CREATE TABLE legacy (x INTEGER)")
    boot.execute("PRAGMA user_version = 1")
    boot.commit()
    boot.close()

    runner = SqliteMigrationRunner(
        migrations=[
            Migration(1, 2, apply=lambda c: c.execute("CREATE TABLE upgraded (x)"))
        ]
    )
    provider = ConnectionProvider(db_path=db_path)
    try:
        init_db(
            provider,
            schema_sql="-- unused; runner handles upgrade",
            latest_version=2,
            migration_runner=runner,
        )
        conn = provider.get()
        assert _uv(conn) == 2
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"legacy", "upgraded"} <= tables
    finally:
        provider.close_all()


def test_init_db_raises_when_runner_leaves_wrong_user_version(
    tmp_path: Path,
) -> None:
    """init_db verifies user_version *after* the runner returns.

    A buggy runner that forgets to bump user_version is caught by init_db's
    post-runner check (init_db.py lines 115-121).
    """
    from fis_monitor.infra.sqlite.connection import ConnectionProvider
    from fis_monitor.infra.sqlite.init_db import init_db

    db_path = tmp_path / "state.db"
    boot = sqlite3.connect(db_path)
    boot.execute("CREATE TABLE legacy (x INTEGER)")
    boot.execute("PRAGMA user_version = 1")
    boot.commit()
    boot.close()

    # Buggy callable: applies a change but never bumps user_version.
    def _buggy_runner(conn: sqlite3.Connection, _from: int, _to: int) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE bug (x)")
        conn.commit()

    provider = ConnectionProvider(db_path=db_path)
    try:
        with pytest.raises(RuntimeError, match="user_version"):
            init_db(
                provider,
                schema_sql="-- unused",
                latest_version=2,
                migration_runner=_buggy_runner,
            )
    finally:
        provider.close_all()
