# 7. Конкурентность и потокобезопасность

## 7.1 Кто с чем шарит память

| Ресурс | Кто пишет | Кто читает | Защита |
|---|---|---|---|
| `state.db` (SQLite) | monitor-cycle, enrichment, full-scan, web-handlers, notifier | те же | per-thread connection + `busy_timeout=5000` + **retry SQLITE_BUSY с jitter обязателен на всех writers** + **batch commit по 50 строк** в full_scan со `sleep(50ms)` между батчами |
| `Settings` (актуальный конфиг) | `WatchdogConfigSource.reload()` (1 поток) | все use cases | `Settings` — **immutable Pydantic BaseModel(frozen=True)**, swap по ссылке. **Обязательный паттерн**: `s = config_source.current()` ОДИН раз в начале workflow-шага, далее использовать локальную `s`. Reload применяется к следующему циклу. |
| `EventBus` subscribers | sync producers (cycle, notifier, session_monitor) | async SSE generators (FastAPI event loop) | `queue.Queue` thread-safe, fan-out — один Queue на подписчика. Normal: drop. Critical: block 2с + force-unsubscribe. |
| `last_known_id` cache | monitor-cycle | monitor-cycle | в БД, чтение перед каждым циклом |
| `session_expired` flag | session_monitor | monitor-cycle, enrichment, full_scan (проверяют на входе) | в БД (key `state.session_expired`), atomic |
| `cycle_in_progress` флаг | monitor-cycle (set/clear) | enrichment (проверяет) | **SOFT-YIELD** через `threading.Event` (in-memory, инжектирован DI как `Infra.cycle_progress_signal`). Enrichment видит `is_set()` → `sleep(50ms)`, **не mutex**. Потеря на рестарте OK (флаг — soft-yield, не для durability). НЕ персистится в БД. |
| Playwright `profile/` | login_session (на деманд) | RequestsHttpClient (читает cookies при следующем запросе) | mutex `profile_lock` + **single-flight** на headed-login: вторая попытка возвращает «уже идёт» |
| Notifier queue (in-memory) | monitor-cycle (продюсер) | NotifierDispatcher consumer | `queue.Queue`; persistence нотификаций — только в БД через `notifications` (idempotency) |

## 7.2 Приоритеты задач (общая очередь? нет)

Decisions-log говорит «приоритет: monitor > enrichment > full_scan». Это **не очередь** — это политика конкуренции за SQLite-writer-lock. **«Единая очередь» из decisions-log трактуется как SQLite writer-lock на уровне WAL** (см. [[decisions/ADR-005-concurrency-soft-yield-retry-busy|ADR-005]]). Никакого централизованного Python writer-thread не делаем.

Реализуется через раздельные потоки и комбинацию:

- **`busy_timeout=5000`** на каждом коннекте + **retry SQLITE_BUSY с jitter** (`time.sleep(random.uniform(0.01, 0.05) * (2**attempt))`, max 5 попыток) — обязателен на ВСЕХ writers (cycle, enrichment, full_scan, web-handlers, notifier).
- **`cycle_in_progress` SOFT-YIELD флаг**: enrichment проверяет перед каждой записью → если установлен, `sleep(50ms)` и повторно проверяет. Это **не mutex**, не блокирует, не вызывает priority inversion при сбое cycle.
- **Батчинг full_scan**: коммит по 50 строк + `sleep(50ms)` между батчами — отпускает write-lock.

**Альтернатива (единый writer-thread) — отвергнута**: всё упирается в один поток, добавляет сложности (queue, протокол результата), прибыли мало.

## 7.2.bis SQLite maintenance

- **WAL checkpoint**: раз в час maintenance-таска делает `PRAGMA wal_checkpoint(RESTART)`. **Не TRUNCATE** — RESTART успешно работает при наличии активных читателей (дочекпоинтит до позиции текущего reader-а и блокирует новых писателей пока reader не отпустит), а TRUNCATE при readers фактически no-op. На самотёк-checkpoint между maintenance-окнами полагаемся через `wal_autocheckpoint=1000` (PASSIVE).
- **Incremental vacuum**: раз в сутки `PRAGMA incremental_vacuum` (требует `auto_vacuum=INCREMENTAL` в schema, см. [[decisions/ADR-007-per-connection-pragma|ADR-007]]). Без этого DB-файл растёт без переиспользования free-pages.
- **Курсоры**: все cursor'ы в repo — `with conn.execute(...) as cur` или явный `cur.close()`. Длинные итерации (`needing_enrichment(limit=N)`) — fetch в список, закрытие курсора перед обработкой.
- **`lots_history` retention**: 1 год. Индекс `idx_history_changed_at`.
- **`cycles` retention**: 90 дней.
- **`notifications` retention (R3-M7)**: `permanent_fail` старше 90 дней удаляются.
- **Chunked DELETE с sleep (R3-M7)** — ОБЯЗАТЕЛЬНЫЙ паттерн для всех maintenance-DELETE (history, cycles, notifications). Один большой `DELETE WHERE changed_at < ...` блокирует writer-lock на десятки секунд при росте таблицы; busy_timeout у конкурирующих writers исчерпывается. Pattern:
  ```python
  while True:
      with conn:  # auto-commit
          cur = conn.execute(
              "DELETE FROM lots_history WHERE rowid IN ("
              "  SELECT rowid FROM lots_history WHERE changed_at < ? LIMIT 1000)",
              (cutoff,),
          )
          if cur.rowcount == 0:
              break
      if stop_event.wait(0.1):
          return
  ```
  100ms sleep между чанками отпускает write-lock для cycle/enrichment/notifier. 1000 строк/chunk даёт ~50-100ms работы — компромисс между throughput и latency для конкурирующих writers.
- **`list_presence` в `lots_history`**: НЕ писать каждый цикл; писать только при `is_active 1→0` / `0→1` (после переоценки 2-х циклов отсутствия — см. removal-detection).
- **`lot_html_archive` retention** (R4-Minor): **в MVP не чистим**. Рост ~30 МБ/год (gzip HTML) при ~5к лотов в год приемлем для local-installation. Если когда-нибудь объём станет проблемой — добавим retention в maintenance (например, удалять архив для лотов где `inactive_since < now - 1y`).

## 7.3 SSE fan-out (sync → async, 1 → N)

```
[monitor-cycle thread]
  └─► event_bus.publish(SseLotNew(...))
            │
            ▼
   ┌──────────────────────────┐
   │ ThreadEventBus           │  держит set[Queue]
   │  - subscribers: list     │  под threading.Lock
   │  - publish():            │
   │     for q in subscribers:│
   │       q.put_nowait(evt)  │  ← non-blocking, drop при переполнении
   └─────┬────────┬───────────┘
         │        │       (по одной очереди на вкладку)
         ▼        ▼
   [SSE gen #1] [SSE gen #2]   ← async generators в FastAPI
        │            │
        │ await loop.run_in_executor(None, q.get, timeout=15)
        │ if timeout → yield ping
        │ else      → yield event
        ▼            ▼
     Tab #1       Tab #2
```

**Решения (priority на событии, см. [[architecture/03-protocols]] §3.5):**
- **`event.priority == "normal"`** (`SseLotNew`, UI-нотификации): `put_nowait` на subscriber-queue с `maxsize=100`. При переполнении — **drop-from-tail** (старые UX-события можно потерять, БД source of truth).
- **`event.priority == "critical"`** (`SseSessionExpired`, `SseCycleError`, `SseSmtpFailed`): blocking `put(timeout=2.0)`. При timeout — force-unsubscribe slow consumer + `logger.warning` (через payload-redactor по `SsePayloadSchema`) + **persist last critical** в таблицу `state` по **per-type ключам** (R3-C5): `last_critical_event:session`, `last_critical_event:cycle`, `last_critical_event:smtp` — value = JSON только из whitelist-полей, TTL 1 час каждый. На reconnect новая подписка доливает ВСЕ pending слоты в TTL. Per-type slots: пачка из session.expired + cycle.error за окно TTL не теряет предыдущее событие (как при single-slot). Whitelist полей: см. [[data-model/sse]].
- **Watchdog на slow consumer**: если очередь подписчика > 50 — маркер `slow`. После 3 публикаций со slow — force-unsubscribe + `subscription.close()`.
- **Subscription** — context-manager: при дисконнекте удаляет свой Queue из набора.
- **SSE-generator** в роуте: `await asyncio.wait_for(loop.run_in_executor(sse_executor, q.get), timeout=15)` → keep-alive ping при timeout. В `finally` ГАРАНТИРОВАННО `subscription.unsubscribe()`.

**SSE security:**
- Принудительная проверка `Origin === http://127.0.0.1:8080` или `http://localhost:8080`; без Origin → 403. EventSource всегда same-origin Origin шлёт, поэтому это безопасно.
- Onboarding-gate middleware покрывает `/sse/*`.
- Никакого `Access-Control-Allow-Origin: *` нигде, ни в одном роуте.
- Integration-тест: `tests/integration/test_sse_security.py` — Origin: null → 403, Origin: evil.com → 403, без Origin → 403.

## 7.4 Immutable DTO

Все Pydantic-модели передаются между потоками — **`model_config = ConfigDict(frozen=True)`** на `Lot`, `LotDTO`, `Settings`, `CycleResult`, всех SSE-событиях. Никаких mutable shared structures.

## 7.5 Threading + Playwright (выделенные executor'ы)

| Executor | max_workers | Назначение | Почему отдельный |
|---|---|---|---|
| `pw-login` | 1 | Headed-login Playwright. Один долгоживущий `sync_playwright()` instance (cold-start ~1.5с, переиспользуется между попытками). | Не делится с anyio threadpool. Sync Playwright API не thread-safe. |
| `sse-wait` | 64 | `q.get()` в SSE-generator. | Не делится с FastAPI handler'ами — медленные подписчики не съедают handler-пул. |
| `enrich` | 10 | EnrichmentService параллельные fetch. | Изолированный bound с use case'ом. |
| FastAPI default | uvicorn-default | sync HTTP handlers (`def`). | Standard. |

**Single-flight + hard-deadline + cancel** на headed-login ([[decisions/ADR-014-two-phase-shutdown|ADR-014]] ext, R3-C3):
- `LoginService` хранит `current_job: LoginJob | None` под `threading.Lock`. Если есть current — handler `/auth/login` возвращает существующий `job_id`. Иначе создаёт новый job и submits в `pw_executor`.
- **Hard deadline** — `open_headed_login(deadline=300.0)` (5 минут). По истечении worker возвращает `LoginOutcome(success=False, error="timeout")`. Без этого пользователь, закрывший вкладку без логина, оставляет навсегда висящий headed-Chromium → блок при shutdown.
- **`LoginService.cancel_active_job()` публичный**: thread-safe, идемпотентный. Вызывает `current_job.session.cancel()` (под `threading.Lock`) — `browser.close()` извне worker-thread развернёт `page.wait_for_url` с `TargetClosedError`. Доступен из:
  1. UI: HTMX-кнопка «Отменить вход» в модалке прогресса (после 30с появления).
  2. Shutdown phase 1.5 (см. [[architecture/04-composition-root]] §4.4 lifespan).

**Прогресс login** идёт через SSE: handler `POST /auth/login` возвращает `202 Accepted` + `{job_id, sse_url: "/sse/login/{job_id}"}`. События: `login.starting`, `login.window_open`, `login.completed{success: bool, error?: str}`.

## 7.6 Hot-reload config (WatchdogConfigSource)

- Подписка на **директорию** `data_dir`, фильтр по basename `config.json` (не на сам файл — atomic save через temp+rename даёт `Created/Moved`, а не `Modified`).
- Обрабатывать `created | modified | moved` одинаково.
- **Debounce 300мс**: коалесцируем серию событий (текстовые редакторы пишут пачкой).
- Pipeline: atomic read → Pydantic validate → **swap `_current` только при успехе**. Невалидное → `logger.warning` + старый `Settings` живёт, никакого frozen-app.
- **Diff-лог на INFO** (`app.jsonl`) — ТОЛЬКО счётчики и булы (no PII):
  - `recipients: count 2 → 3` (не сами адреса)
  - `regions changed: {1} → {1, 2}` (домен валидируется — это enum)
  - `smtp.host changed: true` (БЕЗ значения)
  - `interval_minutes: 15 → 5` (числовой scalar — это OK, не PII)
- **Полные значения config-diff** — отдельный append-only `audit.jsonl`, **физически исключённый** из `DiagnosticsService` (как `smtp_credentials`). Файл собирается ТОЛЬКО для on-disk audit, не для отправки клиенту. См. [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]], секция «audit.jsonl isolation».
- **ACL на `config.json`** ([[ops/runbook]]/installer-чеклист): Windows — `Users: read, %USERNAME%: full`; Linux — `chmod 600`. Если кто-то с правами админа пишет в config — это вне нашей threat model.
