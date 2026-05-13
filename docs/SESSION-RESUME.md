# Точка возобновления сессии (обновлено 13.05.2026 после сессии #3)

Контекст для следующей сессии Claude Code. Прочитать первым, потом — `architecture.md` + `decisions-log.md`.

## Где остановились (сессия #3, 13.05.2026)

**Закрыты 2 таски + сетап Obsidian-vault.** Closed: `531.3` (compute_changes), `bye.3` (SmtpHostPolicy с DNS-blocklist).

### Что сделано в сессии #3

1. **bd `531.3` closed** (commit `6efb52c`) — pure `compute_changes(old, new, tracked)` в `domain/diff.py`. `ALLOWED_TRACKED_FIELDS` SSOT через `typing.get_args(TrackedField)`. `auction`/`list_presence` → `NotImplementedError` (fail fast, не утекает AttributeError из BEGIN IMMEDIATE tx). 19 тестов.
2. **bd `bye.3` closed** (commits `5efdebe` + `fa44942`) — `DefaultSmtpHostPolicy.resolve_and_check`. Прошло Code Review + Security Engineer + повторное ревью. Финальное состояние:
   - Thread-safe DNS timeout через `concurrent.futures.ThreadPoolExecutor.submit().result(timeout)` вместо process-global `socket.setdefaulttimeout`.
   - TLD blocklist расширен `arpa` (RFC 8375 home.arpa, reverse-DNS зоны).
   - Guard от malformed resolver output (non-tuple/wrong-length/empty sockaddr/non-str ip/non-subscriptable).
   - `@runtime_checkable` Protocol, `assert→raise` для `-O`.
   - 49 тестов + 1 skip (real-DNS smoke).
3. **MVP scope зафиксирован**: без enrichment, без авторизации, без catalog. ≈27 тасок без FIXME (см. §MVP-план ниже).
4. **Obsidian vault setup** (commit `6ddfd9c`): `docs/.obsidian/` создан, `docs/tasks/` с шаблоном. **DoD расширен**: каждая закрытая bd-таска требует `docs/tasks/<bd-id>.md` + ADR в `decisions-log.md` для архитектурных решений + запись в `glossary.md` для новых терминов. CLAUDE.md обновлён. Записано в bd memory.
5. **Backfill Obsidian** (commit `688d886`): задним числом созданы task notes для 4 закрытых тасок (531.1, c0u, 531.3, bye.3), 9 новых записей в `glossary.md`, добавлен **ADR-022** (ALLOWED_TRACKED_FIELDS SSOT + SmtpHostPolicyError hierarchy).

### С чего стартовать следующую сессию

- `bd ready` — параллельная волна 1 (разные файлы, нет deps):
  - **`531.2`** Protocols в `domain/interfaces.py` (Sonnet)
  - **`akv.1`** ConnectionProvider в `infra/sqlite/` (Sonnet)
  - **`0t8`** Diagnostics exclude-list (Sonnet)
- После волны 1 — `akv.2` + `akv.3` (FIXME init_db/migration) последовательно, плюс `tic.1` EventBus параллельно.
- `bye.4` (SmtpEmailNotifier manual STARTTLS) — разблокирован, но требует BEGIN IMMEDIATE state.db (зависит от `akv.7`).

### MVP-scope (без enrichment, без auth, без catalog)

Дропнуто из MVP: `a4t.2` enrichment, `a4t.9` session monitor, `bye.6` PlaywrightLoginSession, `oxy.4` auth routes, `oxy.3` catalog routes, `oxy.7` templates. **MVP ≈ 27 тасок без FIXME** (29 включая 2 P0 FIXME).

### DoD per task (актуализировано в сессии #4)

Каждая закрытая bd-таска ОБЯЗАНА:
1. Tests + ruff green, code committed
2. `bd close <id>`
3. Obsidian vault обновляется **только если таска принесла новые знания**:
   - ADR в `docs/decisions-log.md` — для архитектурного решения
   - Запись в `docs/glossary.md` — для нового термина/класса/Protocol
   - Обновление существующих доков (`architecture.md` и т.п.) — если изменились описываемые контракты
4. **НЕ создавать** `docs/tasks/<bd-id>.md` (директория удалена в сессии #4) — контекст таски хранится в bd (description/notes) и git-логе.

При делегировании суб-агенту оркестратор включает эту инструкцию в промпт и явно запрещает создание per-task файлов.

## Где остановились (сессия #2, 13.05.2026)

**Domain DTO слой готов** (commits `81a78f9` + `e5551cb`). Закрыты bd `531.1` (базовые DTO) и `c0u` (extension для Protocol-швов). 85 unit-тестов green. Сейчас стоп перед стартом `531.2` Protocol-интерфейсов.

### Что сделано в сессии #2
1. **PM-декомпозиция**: 63 bd-issues (9 эпиков + 54 sub-tasks + 6 follow-up из ревью).
2. **bd 531.1 closed** — Domain Pydantic DTOs (Lot, FieldChange, LotUpsertResult, ResolvedSmtpEndpoint, SsePayloadSchema, LotPublicDTO/UserDTO с raw_json exclude, ErrorCategory, SseCycleError/SmtpFailed, SmtpCredentials, DomainError hierarchy). Critical security fix: убран `SseCycleError.message` (PII vector).
3. **bd c0u closed** — DTO extension: Settings tree, LotUserState, OnboardingState StrEnum, CycleResult, NotificationRecord (ADR-019 state machine), NotifierConfig, ParsedListRow/Detail, HttpResponse, LockHandle, NotifyResult, LoginOutcome (Literal), SessionStatus, SseSessionExpired/LotNew/LotStatus, SseEvent union (PEP 695), EventSubscription[T] generic Protocol, ConfigSubscription. data-model.md обновлён (SESSION_EXPIRED whitelist).
4. **6 follow-up bd-issues** из ревью: `z9d` (move Subscription Protocols в interfaces.py в рамках 531.2), `0u7` (split models.py), `0t8` (diagnostics PII exclude-list для DiagnosticsService), `arl` (NotifierDispatcher NotifyResult.detail leak test), `vn5` (ADR pydantic[email]), `7pi` (LotUserState.note max_length).
5. **3 follow-up из 531.1**: `ctz` (SmtpCredentials pickle hardening), `x2x` (Message-ID hash known-limit docs), `4kh` (errors.py PII-in-args docstring).

### С чего стартовать следующую сессию
- `bd ready` — следующая в очереди: **531.2 Protocols (interfaces.py)**. Brainstorm-документ уже подготовлен в сессии #2 (см. transcript) — решения: файл `interfaces.py` (по canon, НЕ `protocols.py`), ConnectionProvider исключить, UserStateRepository включить, runtime_checkable только Notifier+Clock, MigrationRunner Protocol НЕ создавать. 17 Protocol-ов: 4 Layer-0 + 7 Layer-1 + 7 Layer-2 + 1 Notifier.
- После 531.2 закроется и `z9d` (перенести EventSubscription/ConfigSubscription).
- Параллельно с 531.2 можно запускать P0 FIXME: `akv.2` (init_db PRAGMA user_version) и `akv.3` (Migration v1→v2).

### Архитектура завершена

## Что сделано в этой сессии (13.05.2026)

1. **5 раундов архитектурных правок** через Software Architect (часть на Opus для critical), каждый раунд — 4 параллельных ревьюера.
2. Добавлены **ADR-014..021** в `decisions-log.md`:
   - ADR-014 Two-phase shutdown
   - ADR-015 SMTP host validation (resolve_and_check + manual STARTTLS)
   - ADR-016 Repository invariants (BEGIN IMMEDIATE, identifier whitelist, _sync_geo приватный)
   - ADR-017 Secrets handling (SecretStr + crash-dump exclude)
   - ADR-018 Onboarding FSM server-enforced
   - ADR-019 Notification state machine (reserve → mark_attempt → sent/permanent_fail) + at-least-once + Message-ID
   - ADR-020 SMTP host SSOT = state.db (защита от config-write-vector)
   - ADR-021 Manual STARTTLS (`docmd("STARTTLS") + wrap_socket(server_hostname=original_host)`)
3. Создан **`docs/onboarding.md`** — FSM с guards (5 states, transitions, серверная валидация)
4. **`schema.sql`** расширена: `notifications` state machine (status/attempt_no/last_attempt_at, sent_at NULLable, idx_notifications_pending partial, idx_notifications_sent_at DESC partial), `smtp_credentials.smtp_host/smtp_port` колонки, conditional FTS triggers, _sync_geo контракт, retention chunked DELETE.
5. **`data-model.md`**: `FieldChange`, `LotUpsertResult`, `ResolvedSmtpEndpoint`, `SsePayloadSchema` whitelist, `ErrorCategory` Literal enum, `LotPublicDTO` vs `LotUserDTO`.
6. **`notifications.md`**: sync `consumer_loop` (без asyncio), at-least-once + детерминированный Message-ID `<{lot_id}.{channel}.{sha256(recipient)[:16]}@fis-monitor.local>`, MAX_TOTAL_ATTEMPTS=10 hard-cap, `mark_attempt -> int | None`.
7. **9 FIXME/TODO/Note** вставлено в доки по итогам R5 (остаточные минорные пункты).

## bd-эпики и таски

**Все старые bd-таски удалены в конце предыдущей сессии.** Они покрывали только bootstrap (T1-T4) и не отражали скоуп после 5 раундов архитектурного ревью.

**В новой сессии первым шагом — Senior Project Manager делает полную декомпозицию архитектуры в bd-issues.** См. ниже.

## С чего начинать следующую сессию — Project Manager decomposition

**Шаг 0 (обязательно первым делом):**

Спавн **Senior Project Manager** через unified-workflow с задачей:

> Создать полную bd-декомпозицию проекта fis-monitor на основе финальной архитектуры (`docs/architecture.md`, `docs/decisions-log.md` ADR-001..021, `docs/data-model.md`, `docs/db/schema.sql`, `docs/notifications.md`, `docs/onboarding.md`).
>
> **Что создать в bd:**
> - **9 epic-ов** соответствующих слоям/доменам (см. список ниже)
> - **40-60 sub-tasks** с реалистичным scope (2-5 дней каждая) и acceptance criteria
> - **Dependency graph**: parent-child (epic → sub-task) + blocks (sequential) между слоями
> - **TDD-задачи** как отдельные issues или как acceptance criteria внутри implementation-задач (см. 31 TDD-тест из R5)
> - **FIXME-pickup задачи** для 2 обязательных предкоммитных правок (migration v1→v2 SQL rebuild + init_db pre-flight)

### 9 epic-ов для декомпозиции

1. **Domain layer** — Pydantic DTO (Lot, FieldChange, LotUpsertResult, ResolvedSmtpEndpoint, SsePayloadSchema), Protocol-швы (~15), `domain/diff.py::compute_changes`, ErrorCategory Literal
2. **Repositories** — LotRepository (BEGIN IMMEDIATE + _sync_geo R-tree), NotificationsRepository (state machine reserve→attempt→sent/permanent_fail), SettingsRepository, SmtpCredentialsRepository (singleton), CyclesRepository, ConnectionProvider (per-thread WeakSet), MigrationRunner
3. **Infrastructure adapters** — RequestsHttpClient, ListParser/DetailParser (selectolax), SmtpHostPolicy (resolve_and_check), SmtpEmailNotifier (manual STARTTLS + Message-ID), BrowserSseNotifier, PlaywrightLoginSession (headed + cancel), AutostartManager Windows/Linux, FileLocker (OS-level)
4. **Services (use cases)** — MonitorCycleService, EnrichmentService, NotifierDispatcher (consumer_loop + retry + recovery), OnboardingService (FSM), SmtpTestService, SettingsService, DiagnosticsService (schema-snapshot fail-closed), LotQueryService, SessionMonitor
5. **EventBus + SSE** — ThreadEventBus с publish(event) + priority routing, EventSubscription, ConfigSubscription, last_critical_event per-type slots, force-unsubscribe slow consumer
6. **Composition root** — Infra/Services dataclasses, build_container 5 слоёв, three-phase shutdown lifespan, ThreadSupervisor, pw_executor с Thread+join(5.0)
7. **Web layer** — CSRF middleware (Host allow-list + Origin whitelist), FastAPI routes (lots/settings/auth/notifications/diagnostics), SSE endpoints с Origin check, onboarding-gate middleware, /auth/cancel + rate-limit
8. **Logging/observability** — audit.jsonl, app.jsonl, requests.jsonl, redactor pipeline (RecipientFilter + StackPIIFilter), structured JSON, ротация посуточно 30 дней
9. **Tests + tooling** — pytest tmp_db fixture (WAL), factory functions, EXPLAIN QUERY PLAN harness, concurrent-writer helper, import-linter CI config, 31 integration-тест из R5 финального отчёта

### TDD-задачи (31 тест из R5) — обязательны для acceptance

Передать Project Manager-у полный список из 31 теста (см. секцию «TDD-чеклист» этого файла). Каждый тест либо отдельная bd-task, либо acceptance criteria в parent-implementation task.

### Обязательные FIXME (создать как высокоприоритетные bd-issues)

1. **Migration v1→v2 SQL переписать через 12-step rebuild pattern** (FIXME в `decisions-log.md` ADR-019 ext)
2. **`init_db()` pre-flight `PRAGMA user_version` check** (FIXME в `architecture.md` §3.1)

### Что уже физически создано на диске (НЕ переделывать)

- `pyproject.toml` с зафиксированным стеком (Python 3.12+, FastAPI, sse-starlette, selectolax==0.4.8, playwright==1.58.0, и т.д.)
- `.gitignore`
- `README.md`
- `src/fis_monitor/` структура пустых пакетов (`__init__.py`) по `project-structure.md`
- `tests/` структура (unit/integration/fixtures)
- `claude-design/` готовые templates+static от дизайнера

PM учитывает это как done — не дублирует в bootstrap-задачи.

## Прежний план (для справки — старые таски удалены)

**Прочитать в порядке:**
1. `docs/SESSION-RESUME.md` (этот файл)
2. `docs/architecture.md` — финальная архитектура (~700+ строк, 5 раундов ревью)
3. `docs/decisions-log.md` — ADR-001..021
4. `docs/data-model.md` — Pydantic DTO
5. `docs/notifications.md` — state machine отправки
6. `docs/db/schema.sql` — канон схемы
7. `docs/onboarding.md` — FSM
8. `docs/runbook.md` — known-limitations

**bd-команды:**
```bash
bd show gektar_monitor-wtr  # T2 — текущая in_progress
bd ready                     # проверить что T2 ready
```

## Принципы кода (обязательные для всех агентов)

См. `/home/alex/.claude/projects/-home-alex-dev-gektar-monitor/memory/feedback_code_principles.md` — **обязательно встраивать в промпт каждого writer/reviewer-агента** как отдельный блок.

- SOLID (SRP/OCP/LSP/ISP/DIP)
- Dependency Injection через конструктор
- Protocol/ABC для всех внешних зависимостей
- High cohesion, low coupling
- Composition over inheritance
- Расширяемость через регистрацию плагина
- Тестируемость через мок-Protocol

## Топология сборки (5 слоёв из ADR-004)

1. **Layer 0** — Clock, EventBus, ConnectionProvider, Locker, ConfigSource
2. **Layer 1** — Репозитории (зависят от Layer 0)
3. **Layer 2** — HTTP, Parser, LoginSession, SmtpHostPolicy
4. **Layer 3** — Notifiers (registry собран ДО dispatcher), AutostartManager
5. **Layer 4** — Use cases (dispatcher до cycle, всё остальное)

## TDD-чеклист (31 integration-тест, обязательны перед merge каждого слоя)

Полный список — в финальном отчёте R5 (см. transcript этой сессии). Ключевые группы:

**Domain / Repository (10):** LotRepository.upsert atomicity, compute_changes TOCTOU, _sync_geo 5 переходов, notifications state machine, list_pending_older_than zombie, MAX_TOTAL_ATTEMPTS cap, recovery после kill -9 с Message-ID, idx_notifications_pending EXPLAIN, idx_notifications_sent_at DESC EXPLAIN, Migration v1→v2 idempotency.

**Infrastructure (8):** SmtpHostPolicy blocklist (RFC1918/loopback/cloud-meta/IPv4-mapped/TLD), Manual STARTTLS happy + cert mismatch fail, DNS-rebinding 421, CSRF coverage всех state-changing routes, SSE Origin check, Watchdog atomic temp+rename + debounce, SmtpCredentials atomicity, WAL checkpoint(RESTART) под reader.

**Services / Composition (7):** Three-phase shutdown clean/slow/hung + lock release гарантирован, pw_executor Thread+join(5.0), SSE force-unsubscribe slow consumer, single-flight headed-login, Onboarding FSM bypass через query-param, build_container topological order, SettingsService DNS вне tx.

**Web / Diagnostics (3):** schema drift fail-closed generic UI, audit.jsonl no-op в cloud-sync, diagnostic.zip exclude `*.dmp`/`core.*`.

**Logging / Security (3):** redactor coverage (RecipientFilter, StackPIIFilter), SmtpCredentials.password `repr()='***'`, import-linter контракты CI.

## Known limitations (приемлемы для MVP single-user)

| Limitation | Где зафиксировано |
|---|---|
| At-least-once SMTP с Message-ID dedup | ADR-019 ext, notifications.md «Семантика доставки» |
| Windows WaitToKillAppTimeout=5s — inflight потери при shutdown машины | ADR-014, runbook |
| Write-on-state.db = full takeover в trust-model | ADR-019 |
| HMAC на smtp_test_last_result_ok — known-limit | ADR-018 |
| WSL2/Docker loopback pierce — для server-mode перейти на Unix socket | runbook |
| Single-consumer recovery bottleneck при N>10 pending | notifications.md TODO |

## Что НЕ покрыто архитектурой (решать в коде)

- pytest `tmp_db` fixture (tempfile + WAL + init_db) — нужно в `tests/conftest.py`
- Factory-boy для domain (`make_lot()`, `make_notification()`)
- MigrationRunner Protocol — определить интерфейс
- `RequestsHttpClient` retry policy
- `ThreadPoolExecutor` enrichment size обоснование
- `queue.Queue.maxsize` для notifier (рекомендация maxsize=10000)
- `BrowserSseNotifier.send()` поведение при EventBus переполнении

## Артефакты дизайнера

`/home/alex/dev/gektar_monitor/claude-design/` — готовые продакшен templates+static:
- `HANDOFF.md` — таблица всех эндпоинтов
- `templates/feed.html.jinja`, `templates/base.html.jinja`, `templates/partials/`, `templates/onboarding/`
- `static/app.css`, `static/app.js`
- Demo HTML для визуальной сверки

Скопировать в `src/fis_monitor/web/templates/` и `src/fis_monitor/web/static/` в рамках T3.

## Команда для возобновления

В новой сессии: «Прочитай `docs/SESSION-RESUME.md` и начинаем с Project Manager декомпозиции».

**Первое действие в новой сессии:**
1. `bd list` — убедиться что issues пустой (после очистки прошлой сессии)
2. Спавн **Senior Project Manager** через unified-workflow по инструкции из секции «С чего начинать следующую сессию — Project Manager decomposition» выше
3. Проверить созданные epic-и и зависимости (`bd ready`, `bd list`)
4. Стартовать первую ready task через профильного агента (Backend Architect / Frontend Developer / SRE / etc.)

## Memory

`/home/alex/.claude/projects/-home-alex-dev-gektar-monitor/memory/`:
- `MEMORY.md` — индекс (ADR-001..021)
- `feedback_code_principles.md` — обязательный блок для всех writer/reviewer-агентов

## Что почищено в конце сессии

- `monitor.zip` (77KB) — удалён, был распакован в `claude-design/`
- Документация в `docs/` обновлена и согласована (5 раундов правок)
- ZK Steward audit: удалено 8 устаревших/orphan-файлов (`brainstorm-*.md`, `architecture-review.md`, `design-prompt.md`, `todo-verify-later.md`), починены битые wikilinks, `monitoring-plan.md` сжат 9.4KB→2.8KB
- MOC-блок добавлен в `decisions-log.md` (оглавление по 21 ADR + 12 ранним разделам)
- **Все 6 bd-issues удалены** — старые таски покрывали только bootstrap и не отражали скоуп после 5 раундов архитектурного ревью. В новой сессии Project Manager создаст полную декомпозицию с нуля.
- 9 FIXME/TODO/Note вставлено в доки по итогам R5 (остаточные минорные пункты)
