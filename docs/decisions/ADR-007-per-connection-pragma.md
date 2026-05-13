# ADR-007: Per-connection PRAGMA vs persistent

**Context.** Часть PRAGMA сохраняется в файле БД (`journal_mode`), часть — атрибут коннекта (`busy_timeout`).

**Decision.** **Persistent в `schema.sql`**: `journal_mode=WAL`, `auto_vacuum=INCREMENTAL`, `wal_autocheckpoint`, `user_version`. **Per-connection в `ThreadLocalConnectionProvider._configure`**: `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=OFF`, `temp_store=MEMORY`, `cache_size=-20000`, `mmap_size=268435456`.

Также `wal_autocheckpoint=1000` дублируется per-connection (R4-minor) для гарантии consistent behaviour после restore from backup, где `schema.sql` мог не выполниться. Безвредный duplicate persistent-значения (defence-in-depth).

**Consequences.** Нет «забытого PRAGMA» после reconnect. `schema.sql` декларативен, `_configure` — единственное место setup-а.

См. также: [[decisions-log]], [[architecture/07-concurrency]], `db/schema.sql`.
