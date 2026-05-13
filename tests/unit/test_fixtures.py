"""Tests for project-wide fixtures: tmp_db isolation + factories."""

from fis_monitor.domain.models import Lot, NotificationRecord, Settings
from fis_monitor.infra.sqlite.connection import ConnectionProvider
from tests.factories import make_lot, make_notification, make_settings

# ---------------------------------------------------------------------------
# tmp_db fixture
# ---------------------------------------------------------------------------


def test_tmp_db_yields_connection_provider(tmp_db: ConnectionProvider) -> None:
    assert isinstance(tmp_db, ConnectionProvider)
    conn = tmp_db.get_connection()
    assert conn is not None


def test_tmp_db_applies_schema_user_version(tmp_db: ConnectionProvider) -> None:
    conn = tmp_db.get_connection()
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == 2


def test_tmp_db_uses_wal_journal_mode(tmp_db: ConnectionProvider) -> None:
    conn = tmp_db.get_connection()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_tmp_db_has_canonical_tables(tmp_db: ConnectionProvider) -> None:
    conn = tmp_db.get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {r[0] for r in rows}
    # Canon tables from docs/db/schema.sql.
    assert {"lots", "lots_history", "notifications", "smtp_credentials", "state"} <= tables


def test_tmp_db_isolation_writes_marker(tmp_db: ConnectionProvider) -> None:
    """Each test gets a fresh DB file. Order-independent: pytest's tmp_path
    is unique per test, so this marker cannot leak into other tests no
    matter what order tests run."""
    conn = tmp_db.get_connection()
    conn.execute("INSERT INTO state(key, value) VALUES ('marker_a', '1')")
    conn.commit()
    row = conn.execute("SELECT value FROM state WHERE key='marker_a'").fetchone()
    assert row is not None and row[0] == "1"


def test_tmp_db_isolation_marker_absent(tmp_db: ConnectionProvider) -> None:
    """Companion to the writer above — this test runs against a *different*
    tmp_path-rooted DB file (function-scope), so the previous test's marker
    is invisible regardless of test execution order."""
    conn = tmp_db.get_connection()
    row = conn.execute("SELECT value FROM state WHERE key='marker_a'").fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def test_make_lot_defaults_are_valid() -> None:
    lot = make_lot()
    assert isinstance(lot, Lot)
    assert lot.id == 12345
    assert lot.is_active is True


def test_make_lot_overrides_applied() -> None:
    lot = make_lot(id=42, region="Хабаровск")
    assert lot.id == 42
    assert lot.region == "Хабаровск"


def test_make_notification_defaults_are_pending_state() -> None:
    """Defaults match the freshly-reserved state from ADR-019."""
    n = make_notification()
    assert isinstance(n, NotificationRecord)
    assert n.status == "pending"
    assert n.attempt_no == 0
    assert n.last_attempt_at is None
    assert n.sent_at is None


def test_make_notification_overrides_applied() -> None:
    n = make_notification(status="permanent_fail", attempt_no=3)
    assert n.status == "permanent_fail"
    assert n.attempt_no == 3


def test_make_settings_defaults_are_valid() -> None:
    s = make_settings()
    assert isinstance(s, Settings)
    assert s.mode == "local"
    assert s.regions == [1, 2]


def test_make_settings_overrides_applied() -> None:
    s = make_settings(mode="server", interval_minutes=5)
    assert s.mode == "server"
    assert s.interval_minutes == 5
