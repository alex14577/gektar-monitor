# ADR-005: Concurrency — soft-yield, retry SQLITE_BUSY, без unified writer-queue

**Context.** Decisions-log упоминал «единую очередь» для приоритезации. Ревью DBA: централизованная queue добавляет много сложности, прибыли мало.

**Decision.** «Единая очередь» из decisions-log трактуется **как SQLite writer-lock на уровне WAL**, не Python writer-thread. Приоритезация реализуется через:
1. `busy_timeout=5000` per-connection.
2. **Retry SQLITE_BUSY с jitter обязателен на ВСЕХ writers** (5 попыток, exponential backoff с jitter).
3. **`cycle_in_progress` — SOFT-YIELD флаг**: enrichment проверяет → `sleep(50ms)`. Это **не mutex**, не блокирует при сбое cycle.
4. Full_scan коммитит батчами по 50 + `sleep(50ms)` между батчами.

**Consequences.** Простая модель, нет priority inversion, нет нового потока-арбитра. Цена — каждый writer должен реализовать retry-обёртку (одна функция-декоратор в `infra/sqlite/`).

См. также: [[decisions-log]], [[architecture/07-concurrency]].
