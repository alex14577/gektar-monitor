"""Integration tests for SqliteSmtpCredentialsRepository.

Uses the ``tmp_db`` fixture (from conftest.py) which provides a fresh
ConnectionProvider with the full v2 schema applied.

A local ``FakeClock`` is defined here to keep tests hermetic.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from fis_monitor.domain.models import SmtpCredentials
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from fis_monitor.infra.sqlite.repositories.smtp_credentials import (
    SqliteSmtpCredentialsRepository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock stub — returns a fixed UTC datetime that can be advanced."""

    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self._dt = self._dt + timedelta(seconds=seconds)


def make_repo(
    tmp_db: ConnectionProvider, clock: FakeClock | None = None
) -> SqliteSmtpCredentialsRepository:
    if clock is None:
        clock = FakeClock()
    return SqliteSmtpCredentialsRepository(conn_provider=tmp_db, clock=clock)


def _make_creds(
    *,
    smtp_user: str = "user@example.com",
    smtp_password: str = "s3cr3t",
    smtp_host: str = "smtp.example.com",
    smtp_port: int = 587,
    use_default: bool = True,
    from_name: str | None = None,
) -> SmtpCredentials:
    return SmtpCredentials(
        smtp_user=smtp_user,
        smtp_password=SecretStr(smtp_password),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        use_default=use_default,
        from_name=from_name,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_on_empty_db_returns_none(tmp_db: ConnectionProvider) -> None:
    """load() returns None when no credentials row exists."""
    repo = make_repo(tmp_db)
    assert repo.load() is None


def test_save_and_load_round_trip_all_fields(tmp_db: ConnectionProvider) -> None:
    """save() + load() round-trips all fields; smtp_password matches via get_secret_value()."""
    repo = make_repo(tmp_db)
    original = _make_creds(
        smtp_user="alice@smtp.test",
        smtp_password="hunter2",
        smtp_host="smtp.test",
        smtp_port=465,
        use_default=False,
    )
    repo.save(original)
    loaded = repo.load()

    assert loaded is not None
    assert loaded.smtp_user == "alice@smtp.test"
    assert loaded.smtp_password.get_secret_value() == "hunter2"  # ADR-017
    assert loaded.smtp_host == "smtp.test"
    assert loaded.smtp_port == 465
    assert loaded.use_default is False


def test_save_twice_is_singleton(tmp_db: ConnectionProvider) -> None:
    """save() twice keeps exactly one row (INSERT OR REPLACE semantics)."""
    repo = make_repo(tmp_db)
    repo.save(_make_creds(smtp_host="first.example.com"))
    repo.save(_make_creds(smtp_host="second.example.com"))

    conn: sqlite3.Connection = tmp_db.get()
    count = conn.execute(
        "SELECT COUNT(*) FROM smtp_credentials WHERE id = 1"
    ).fetchone()[0]
    assert count == 1


def test_check_constraint_id_must_be_1(tmp_db: ConnectionProvider) -> None:
    """Direct INSERT with id=2 raises IntegrityError (CHECK id=1)."""
    conn: sqlite3.Connection = tmp_db.get()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO smtp_credentials
                (id, smtp_user, smtp_password, smtp_host, smtp_port, use_default, updated_at)
            VALUES (2, 'x', 'y', 'host', 587, 1, '2024-01-01')
            """
        )


def test_check_constraint_smtp_port_zero_raises_validation_error() -> None:
    """Pydantic rejects smtp_port=0 — we never construct an invalid instance."""
    with pytest.raises(ValidationError):
        SmtpCredentials(
            smtp_user="u",
            smtp_password=SecretStr("p"),
            smtp_host="h",
            smtp_port=0,
        )


def test_atomicity_last_save_wins(tmp_db: ConnectionProvider) -> None:
    """After two saves, load() returns a consistent object from the last save."""
    repo = make_repo(tmp_db)
    repo.save(
        _make_creds(
            smtp_user="first@example.com",
            smtp_password="pass1",
            smtp_host="smtp1.example.com",
            smtp_port=587,
        )
    )
    repo.save(
        _make_creds(
            smtp_user="second@example.com",
            smtp_password="pass2",
            smtp_host="smtp2.example.com",
            smtp_port=465,
        )
    )

    loaded = repo.load()
    assert loaded is not None
    # All fields must come from the second save — no field mixing.
    assert loaded.smtp_user == "second@example.com"
    assert loaded.smtp_password.get_secret_value() == "pass2"
    assert loaded.smtp_host == "smtp2.example.com"
    assert loaded.smtp_port == 465


def test_updated_at_advances_on_each_save(tmp_db: ConnectionProvider) -> None:
    """updated_at in the DB reflects the clock at each save call."""
    clock = FakeClock()
    repo = make_repo(tmp_db, clock)

    repo.save(_make_creds())
    t1 = _get_updated_at(tmp_db)

    clock.advance(10)
    repo.save(_make_creds(smtp_host="newer.example.com"))
    t2 = _get_updated_at(tmp_db)

    assert t2 > t1, "updated_at must advance between saves"


def _get_updated_at(provider: ConnectionProvider) -> str:
    conn = provider.get()
    row = conn.execute(
        "SELECT updated_at FROM smtp_credentials WHERE id = 1"
    ).fetchone()
    assert row is not None
    return row[0]


def test_use_default_false_round_trip(tmp_db: ConnectionProvider) -> None:
    """use_default=False is stored as 0 and loaded back as False (int↔bool)."""
    repo = make_repo(tmp_db)
    repo.save(_make_creds(use_default=False))

    # Verify raw DB value is 0 (int)
    conn: sqlite3.Connection = tmp_db.get()
    raw = conn.execute(
        "SELECT use_default FROM smtp_credentials WHERE id = 1"
    ).fetchone()[0]
    assert raw == 0

    loaded = repo.load()
    assert loaded is not None
    assert loaded.use_default is False


def test_smtp_password_secret_str_repr_is_redacted(tmp_db: ConnectionProvider) -> None:
    """SecretStr repr does not expose the plaintext password (ADR-017)."""
    repo = make_repo(tmp_db)
    repo.save(_make_creds(smtp_password="super_secret"))
    loaded = repo.load()
    assert loaded is not None
    # str() and repr() must NOT contain the password
    assert "super_secret" not in str(loaded.smtp_password)
    assert "super_secret" not in repr(loaded.smtp_password)
    # Only get_secret_value() reveals it
    assert loaded.smtp_password.get_secret_value() == "super_secret"


def test_from_name_set_round_trip(tmp_db: ConnectionProvider) -> None:
    """from_name with a Cyrillic string survives save/load unchanged."""
    repo = make_repo(tmp_db)
    repo.save(_make_creds(from_name="Монитор"))
    loaded = repo.load()
    assert loaded is not None
    assert loaded.from_name == "Монитор"


def test_from_name_none_round_trip(tmp_db: ConnectionProvider) -> None:
    """from_name=None is stored as NULL and loaded back as None.

    Guards the ``creds.from_name or None`` normalisation in save() and
    ``row["smtp_from_name"] or None`` in load() — removing either ``or None``
    would cause a falsy empty-string to survive as "" instead of None.
    """
    repo = make_repo(tmp_db)
    repo.save(_make_creds(from_name=None))
    loaded = repo.load()
    assert loaded is not None
    assert loaded.from_name is None


def test_pre_migration_row_loads_as_none(tmp_db: ConnectionProvider) -> None:
    """A row written with smtp_from_name=NULL (pre-v3 migration semantics) loads as None.

    Simulates the state of an existing row after v2→v3 migration: ALTER TABLE
    ADD COLUMN smtp_from_name TEXT sets existing rows to NULL. Verifies that
    load() maps NULL → None correctly — the ``or None`` guard in load() line 81.
    """
    conn: sqlite3.Connection = tmp_db.get()
    # Insert a row that has smtp_from_name explicitly NULL, mimicking what
    # ALTER TABLE ADD COLUMN produces for pre-existing rows.
    conn.execute(
        """
        INSERT INTO smtp_credentials
            (id, smtp_user, smtp_password, smtp_host, smtp_port,
             use_default, smtp_from_name, updated_at)
        VALUES (1, 'legacy@smtp.test', 'pass', 'smtp.test', 587, 1, NULL,
                '2024-01-01T00:00:00')
        """
    )
    conn.commit()

    repo = make_repo(tmp_db)
    loaded = repo.load()
    assert loaded is not None
    assert loaded.from_name is None
