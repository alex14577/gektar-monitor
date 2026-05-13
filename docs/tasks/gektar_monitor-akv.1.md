---
bd-id: gektar_monitor-akv.1
title: ConnectionProvider — per-thread SQLite, connection registry, PRAGMA
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/infra/sqlite/__init__.py
  - src/fis_monitor/infra/sqlite/connection.py
  - tests/unit/infra/sqlite/__init__.py
  - tests/unit/infra/sqlite/test_connection.py
---

# ConnectionProvider — per-thread SQLite, connection registry, PRAGMA

## Что сделано

Реализован `ConnectionProvider` в `infra/sqlite/connection.py` — Layer 0 компонент без зависимостей от domain. Создан пакет `infra/sqlite/` с `__init__.py`. Написаны 5 TDD-тестов (5/5 green), ruff clean.

## Почему так

### Per-thread изоляция

`threading.local()` хранит `conn` per-thread. Идемпотентный `get_connection()` возвращает существующий коннект без блокировки — zero overhead для повторных вызовов в том же потоке. `check_same_thread=False` в `sqlite3.connect()` обязателен: sqlite3 иначе запрещает закрыть соединение из shutdown-потока, отличного от создавшего.

### Регистрация вместо WeakSet

`sqlite3.Connection` в CPython 3.12 не поддерживает слабые ссылки (`weakref.WeakSet` бросает `TypeError`). Используем `dict[int, sqlite3.Connection]` (id -> conn) под `threading.Lock`. Семантика идентична WeakSet: запись удаляется в `_clear_thread_local()` и `close_all()`, утечек нет. `_clear_thread_local()` — internal helper для тестов; production-вызывать не нужно.

### PRAGMA порядок (ключевой trade-off)

`PRAGMA auto_vacuum = INCREMENTAL` должен быть установлен ДО `PRAGMA journal_mode = WAL` на свежей БД. Переход в WAL-режим фиксирует `auto_vacuum` в заголовке файла — последующий `PRAGMA auto_vacuum = INCREMENTAL` возвращает 0 (no-op). Порядок в `_configure()`:
1. `auto_vacuum = INCREMENTAL`
2. `journal_mode = WAL`
3. остальные per-connection PRAGMA

Это отклонение от порядка в ADR-007 (там `journal_mode` первый) — зафиксировано здесь как implementation detail.

### Per-connection PRAGMA (ADR-007)

- `busy_timeout = 5000` — писатель ждёт 5 с перед `SQLITE_BUSY`
- `synchronous = NORMAL` — баланс durability/производительность
- `foreign_keys = OFF` — FK enforced на уровне сервисов (ADR-007)
- `auto_vacuum = INCREMENTAL` — inplace; требует `PRAGMA incremental_vacuum` для фактической очистки
- `temp_store = MEMORY`, `cache_size = -20000`, `mmap_size = 268435456` — производительность
- `wal_autocheckpoint = 1000` — дубль persistent-значения; важен после restore из backup

`close_all()` использует snapshot `list(registry.values())` до `registry.clear()` — паттерн из [[architecture#3.1]] (R3-minor), защита от RuntimeError при мутации во время итерации.

## Связи

- Закрывает: `bd gektar_monitor-akv.1`
- Следует: [[decisions-log#ADR-007]], [[architecture#3.1]]
- Новые термины: [[glossary#ConnectionProvider]]

## Follow-up

Разблокирует:
- `akv.5` — `SqliteLotRepository` (принимает `ConnectionProvider` через DI конструктора)
- `akv.9` — `init_db()` / миграции (вызывает `provider.get_connection()` для применения schema.sql)

FIXME (из architecture.md): перед первым релизом v2→v3 `init_db()` должен проверять `PRAGMA user_version` и запускать `MigrationRunner` при необходимости.
