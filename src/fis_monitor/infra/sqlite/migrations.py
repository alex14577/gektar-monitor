"""SQLite migration runner — Protocol implementation + Migration dataclass.

Architecture: infra/sqlite (Layer 2). Implements `MigrationRunner` Protocol
from `domain/interfaces.py`. Driven by `init_db()` (akv.2) when the database
`user_version` is older than the application's `latest_version`.

Greenfield-MVP note: schema.sql stamps `PRAGMA user_version = 2` directly, so
fresh installs are applied by `init_db()` via `executescript()` and bypass
this runner entirely. The runner exists to:
1. Provide a Protocol implementation for future v2→v3 migrations.
2. Cover legacy / dev / test databases that started life on an older version.
3. Defend against concurrent migration races (TOCTOU re-check, bd `1zk`).

The concrete v1→v2 migration is registered by bd `akv.3` — this module
provides only the framework.

See:
- `docs/decisions/ADR-019-notification-state-machine.md` §R4-M8
- `docs/decisions/ADR-016-repository-invariants.md` (BEGIN IMMEDIATE invariant)
- `docs/architecture/03-protocols.md` §3.1
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fis_monitor.domain.errors import (
    ConcurrentMigrationError,
    MigrationChainBroken,
)


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema version transition (e.g. v1 → v2).

    `apply` runs INSIDE the runner's BEGIN IMMEDIATE tx; it MUST NOT open its
    own transaction (`BEGIN`/`COMMIT`/`ROLLBACK`) and MUST NOT call
    `PRAGMA user_version` — the runner sets the version atomically with the
    migration body.

    Attributes:
        from_version: Source schema version (inclusive).
        to_version:   Target schema version (= from_version + 1 by convention,
                      but multi-step jumps are allowed if the SQL is idempotent).
        apply:        Callable that performs the actual schema change.
    """

    from_version: int
    to_version: int
    apply: Callable[[sqlite3.Connection], None]


class SqliteMigrationRunner:
    """Concrete `MigrationRunner` for SQLite.

    Responsibility (SRP): orchestrate registered `Migration`s within a single
    `BEGIN IMMEDIATE` writer transaction, with TOCTOU re-check on the
    actual `user_version` after acquiring the writer lock.

    Construction (DI):
        runner = SqliteMigrationRunner(migrations=[m_1_to_2, m_2_to_3])

    Usage (from `init_db`):
        init_db(provider, schema_sql=..., migration_runner=runner)
        # init_db calls: runner(conn, from_version, to_version)

    The class is also `Callable[[Connection, int, int], None]` via
    `__call__`, matching the signature accepted by `init_db`.
    """

    def __init__(self, migrations: Sequence[Migration] = ()) -> None:
        # Defensive copy + stable sort by from_version for deterministic
        # chain-building. Multiple migrations with the same from_version
        # are not allowed (ambiguous chain).
        sorted_migrations = sorted(migrations, key=lambda m: m.from_version)
        self._check_unique_from_versions(sorted_migrations)
        self._migrations: tuple[Migration, ...] = tuple(sorted_migrations)

    @staticmethod
    def _check_unique_from_versions(migrations: Sequence[Migration]) -> None:
        seen: set[int] = set()
        for m in migrations:
            if m.from_version in seen:
                raise ValueError(
                    f"Duplicate migration registered for from_version={m.from_version}"
                )
            seen.add(m.from_version)

    def list_migrations(self) -> Sequence[Migration]:
        """Return registered migrations, ordered by `from_version` asc."""
        return self._migrations

    def __call__(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        """Delegate to `run_pending`. Matches Callable signature in init_db."""
        self.run_pending(conn, from_version, to_version)

    def run_pending(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        """Apply chained migrations from `from_version` → `to_version`.

        Algorithm:
        1. `BEGIN IMMEDIATE` — acquire writer lock.
        2. Re-read `PRAGMA user_version`. If != from_version → rollback +
           raise `ConcurrentMigrationError` (TOCTOU defence, bd `1zk`).
        3. If from_version == to_version → commit and return (no-op).
        4. Build chain greedily: at each step find migration with
           `from_version == current`, else raise `MigrationChainBroken`.
        5. Apply each `Migration.apply(conn)` in order.
        6. `PRAGMA user_version = to_version`.
        7. `COMMIT`. Any exception → `ROLLBACK` + re-raise.

        Note: `PRAGMA user_version = N` cannot use parameter binding (PRAGMA
        rejects `?`). `to_version` is an int from the typed signature — no
        injection risk.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            actual = conn.execute("PRAGMA user_version").fetchone()[0]
            if actual != from_version:
                raise ConcurrentMigrationError(expected_version=from_version, actual_version=actual)

            if from_version == to_version:
                conn.commit()
                return

            chain = self._build_chain(from_version, to_version)
            for migration in chain:
                migration.apply(conn)

            # PRAGMA cannot bind parameters; to_version is a typed int.
            conn.execute(f"PRAGMA user_version = {int(to_version)}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _build_chain(self, from_version: int, to_version: int) -> list[Migration]:
        """Greedy chain-build. Raises `MigrationChainBroken` if no path."""
        by_from: dict[int, Migration] = {m.from_version: m for m in self._migrations}
        chain: list[Migration] = []
        current = from_version
        # Bound the loop by registered migration count + 1 as a hard upper
        # limit. The uniqueness invariant in __init__ guarantees no cycles,
        # so any valid chain visits at most `len(self._migrations)` steps;
        # the +1 is a sentinel for "we should have hit to_version by now".
        for _ in range(len(self._migrations) + 1):
            if current == to_version:
                return chain
            migration = by_from.get(current)
            if migration is None or migration.to_version <= current:
                raise MigrationChainBroken(from_version=from_version, to_version=to_version)
            chain.append(migration)
            current = migration.to_version
        # Fell out of bound without reaching to_version.
        raise MigrationChainBroken(from_version=from_version, to_version=to_version)


# ---------------------------------------------------------------------------
# Concrete migrations registry
# ---------------------------------------------------------------------------

from fis_monitor.infra.sqlite.migrations_v1_to_v2 import v1_to_v2  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v2_to_v3 import v2_to_v3  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v3_to_v4 import v3_to_v4  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v4_to_v5 import v4_to_v5  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v5_to_v6 import v5_to_v6  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v6_to_v7 import v6_to_v7  # noqa: E402
from fis_monitor.infra.sqlite.migrations_v7_to_v8 import v7_to_v8  # noqa: E402

MIGRATION_V1_TO_V2 = Migration(from_version=1, to_version=2, apply=v1_to_v2)
MIGRATION_V2_TO_V3 = Migration(from_version=2, to_version=3, apply=v2_to_v3)
MIGRATION_V3_TO_V4 = Migration(from_version=3, to_version=4, apply=v3_to_v4)
MIGRATION_V4_TO_V5 = Migration(from_version=4, to_version=5, apply=v4_to_v5)
MIGRATION_V5_TO_V6 = Migration(from_version=5, to_version=6, apply=v5_to_v6)
MIGRATION_V6_TO_V7 = Migration(from_version=6, to_version=7, apply=v6_to_v7)
MIGRATION_V7_TO_V8 = Migration(from_version=7, to_version=8, apply=v7_to_v8)


def default_migration_runner() -> SqliteMigrationRunner:
    """Factory: runner with registered v1→v2 … v7→v8 migration chains.

    Usage (composition root / init_db):
        runner = default_migration_runner()
        init_db(provider, schema_sql=schema, migration_runner=runner)

    Returns:
        SqliteMigrationRunner with MIGRATION_V1_TO_V2 … MIGRATION_V7_TO_V8 registered.
    """
    return SqliteMigrationRunner(
        migrations=[
            MIGRATION_V1_TO_V2,
            MIGRATION_V2_TO_V3,
            MIGRATION_V3_TO_V4,
            MIGRATION_V4_TO_V5,
            MIGRATION_V5_TO_V6,
            MIGRATION_V6_TO_V7,
            MIGRATION_V7_TO_V8,
        ]
    )
