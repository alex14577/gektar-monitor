# Точка возобновления сессии (обновлено 13.05.2026 после сессии #7)

Контекст для следующей сессии Claude Code. Прочитать первым, потом — `architecture.md` + `decisions-log.md` (stub-MOC; нужные секции — в `docs/architecture/`, `docs/decisions/`).

## Где остановились (сессия #7, 13.05.2026)

**Закрыты 2 bd-issues (Wave 2B часть 2): `akv.3`, `vgm.3`.** Commits `48e90d3`, `0117ee4`.

### Что сделано в сессии #7

1. **bd `akv.3` closed** (commit `48e90d3`) — миграция SQLite v1→v2:
   - `src/fis_monitor/infra/sqlite/migrations_v1_to_v2.py`. Часть A: `notifications` через 12-step rebuild (CREATE _new + INSERT SELECT + DROP old indexes + DROP + RENAME + CREATE v2 partial indexes). v1-строки → `status='sent'`, `attempt_no=1`, `last_attempt_at=sent_at`. Часть B: `smtp_credentials` через `ALTER TABLE ADD COLUMN smtp_host/smtp_port` с DEFAULT + CHECK (ADR-020).
   - Функция работает ВНУТРИ runner BEGIN IMMEDIATE — без своих BEGIN/COMMIT/ROLLBACK, без `PRAGMA user_version`. FK-toggling опущен (notifications не имеет FK).
   - `migrations.py`: добавлены `MIGRATION_V1_TO_V2 = Migration(1, 2, apply=v1_to_v2)` + factory `default_migration_runner()`.
   - 6 интеграционных тестов (data preservation, индексы, idempotency via TOCTOU, middle-failure atomicity, factory).
   - FIXME ADR-019 §R4-M8 (про невыполнимый ALTER NOT NULL) — снят.
   - **Known limitation**: после migration колонки `smtp_credentials` имеют порядок `id, smtp_user, smtp_password, use_default, updated_at, smtp_host, smtp_port` (ADD COLUMN клеит в конец), отличный от `schema.sql`. Безопасно только при доступе по именам (sqlite3.Row + явный список колонок) — задокументировано в docstring `_migrate_smtp_credentials`.

2. **bd `vgm.3` closed** (commit `0117ee4`) — import-linter контракты (ADR-006):
   - `.importlinter` в корне репо. Два контракта: `layers` ((composition)|(app) → web → services → infra → domain) + `domain_purity` (forbidden: sqlite3, requests, fastapi, playwright, smtplib).
   - Адаптации vs ADR-006 verbatim: `composition` и `app` опциональны через `(...)` (ещё не существуют — 8ov.1/8ov.2); `include_external_packages = True` обязательно для domain_purity (без флага stdlib/3rd-party не резолвится).
   - `pyproject.toml`: `import-linter>=2.0` в dev-deps.
   - `src/fis_monitor/services/__init__.py` — пустой, превращает namespace-пакет в regular (иначе import-linter не обходит layer).
   - `tests/test_import_linter_contracts.py` — happy path + negative (временный config с заведомо нарушенным контрактом). `_resolve_lint_imports()` ищет бинарник рядом с `sys.executable`, `pytest.skip` если отсутствует.
   - README: секция `## CI / Quality gates`.

3. **Vault обновлён**:
   - `docs/glossary.md`: записи `v1_to_v2 (migration)`, `import-linter (контракты архитектуры)`.
   - ADR не создавался (тривиальная реализация существующего design).

4. **Изменения playbook'а / memory**:
   - Memory `sub-agent-mandatory-for-writer-tasks` обновлена: **writer-default = `general-purpose`** (нет конфликтной персоны Laravel/Livewire / cloud-microservices, нет skill auto-trigger'а). Senior Developer / Software Architect — фоллбэк. Backend Architect — избегать (skill-hijack). Reviewer-default остаётся `Code Reviewer` (профильная персона работает).
   - В session #7 ещё использовался Senior Developer (отработал штатно — 3 раза), но **в следующей волне пробуем general-purpose** как первый выбор.

### Состояние репо в конце сессии #7

- Working tree: clean (HEAD `0117ee4`)
- Branch: `master`
- Tests: **303 passed, 2 skipped** (8 новых: 6 миграционных + 2 import-linter)
- Ruff: чисто по новому коду (2 legacy RUF001 в `tests/domain/conftest.py:31`, известно)
- import-linter: `2 kept, 0 broken` на текущем коде
- Git remote: НЕТ (local-only)
- bd: 15 closed, 63 open, 24 blocked, 39 ready, 0 in_progress

### С чего стартовать сессию #8

**Wave 2C — оставшиеся P0/P1:**

- **`bye.4`** P0 — `SmtpEmailNotifier` (manual STARTTLS + Message-ID). Зависит от `akv.7` (SmtpCredentialsRepository) — **проверить разблокирован ли** перед стартом. Если ещё нет — взять `akv.7` или `akv.5` первыми.
- **`8ov.1`** P1 — Composition: Infra + Services dataclasses (split Container). Разблокирует `8ov.2` (build_container).
- **`arl`** P1 — Test: NotifierDispatcher маппит NotifyResult.detail в ErrorCategory без утечки в SSE. Зависит от dispatcher (пока не реализован).

**Заметки для оркестратора:**
- **Writer-default**: попробовать `general-purpose` (sonnet) на первой же задаче — например, на 8ov.1 (нет сложной доменной специфики, идеальный кандидат для проверки). Сохранить результат сравнения в bd memory.
- 8ov.1 создаёт пакет `fis_monitor/composition/` (или модуль) — это активирует optional layers в `.importlinter` (контракт начнёт фактически проверять composition→web→services→infra→domain топологию). После реализации убрать `(...)` в `.importlinter` (необязательно, layers tolerant). 
- Параллельность: 8ov.1 (`composition/`) + bye.4 (`infra/smtp/`) — разные файлы, но bye.4 имеет deps. Уточнить перед волной.
- Reviewer ДО `bd close`, model `sonnet` по дефолту.

### Memory заметки (обновлено в session #7)

- `sub-agent-mandatory-for-writer-tasks` — переписано: writer-default = `general-purpose`, Backend Architect — избегать. Решение принято после ретро session #7 + явного вопроса пользователя про разницу промптов Backend Architect vs Senior Developer. Подтверждение: оба шаблона (Laravel/Livewire premium UI и cloud-microservices) — мёртвый груз для Python/SQLite MVP; персона `general-purpose` чище.

---

## Где остановились (сессия #6, 13.05.2026)

**Закрыты 3 bd-issues (Wave 2B часть 1): `akv.4`, `vgm.1`, `1zk`.** Commit `7dcdcbb`.

### Что сделано в сессии #6

1. **bd `akv.4` closed** — `MigrationRunner` Protocol в `domain/interfaces.py` переписан с thin stub `run(target)` → `list_migrations()` + `run_pending(conn, from, to)` + `__call__` seam (совместимо с `Callable` сигнатурой из `init_db`). `SqliteMigrationRunner` в `infra/sqlite/migrations.py`: BEGIN IMMEDIATE + TOCTOU re-check `PRAGMA user_version == from_version` + greedy chain build + atomic `PRAGMA user_version` update в одной tx + rollback on exception. `Migration` frozen dataclass с `apply: Callable[[Connection], None]` (apply работает ВНУТРИ runner's tx). Новые DomainError: `ConcurrentMigrationError`, `MigrationChainBroken` (PII-safe). Runner ships empty — v1→v2 регистрация в akv.3.

2. **bd `vgm.1` closed** — root `tests/conftest.py`: `schema_sql` (session-scoped, fail-fast при отсутствии файла), `tmp_db_path`, `tmp_db` (function-scope `ConnectionProvider` с применённым `init_db(schema_sql)`). `tests/factories.py`: plain-function `make_lot` / `make_notification` / `make_settings`. Легаси `tests/domain/conftest.py:make_lot` fixture оставлен (backward compat).

3. **bd `1zk` closed** — закрыт в том же коммите как часть TOCTOU re-check в `SqliteMigrationRunner.run_pending` (Major из ревью akv.2 учтён в дизайне akv.4).

4. **Vault обновлён**:
   - `docs/glossary.md`: 6 новых/обновлённых записей — `MigrationRunner` (полностью переписан), `SqliteMigrationRunner`, `Migration`, `ConcurrentMigrationError`, `MigrationChainBroken`, `tmp_db (pytest fixture)`.
   - ADR не создавался (тривиальная реализация существующего design).

5. **Орк-инцидент**: оба запущенных `Backend Architect` (sonnet) sub-agent'а самовольно запустили skill `fewer-permission-prompts` (модифицировали `.claude/settings.json`) вместо своих writer-промптов. Откатил через `git checkout`, написал код сам, прогнал Code Reviewer (sonnet) — APPROVE с Majors (применены), commit. **Feedback пользователя**: сам писать запрещено даже при сбое sub-agent'а — пробовать других писателей (Senior Developer / Software Architect / general-purpose). Сохранено в bd memory: `sub-agent-mandatory-for-writer-tasks`, `backend-architect-skill-hijack`.

### Состояние репо в конце сессии #6

- Working tree: clean (commit `7dcdcbb`)
- Branch: `master`
- Tests: **295 passed, 2 skipped** (29 новых)
- Ruff: чисто по новому коду (2 legacy RUF001 в `tests/domain/conftest.py:31`, известно)
- Git remote: НЕТ (local-only)
- bd: 13 closed, 65 open, 26 blocked, 39 ready

### С чего стартовать сессию #7

**Wave 2B часть 2:**

- **`akv.3`** P0 — v1→v2 rebuild migration (12-step pattern из SQLite docs). Теперь регистрируется через `SqliteMigrationRunner(migrations=[Migration(1, 2, apply=_v1_to_v2)])`. См. ADR-019 §R4-M8 FIXME (SQL невыполним в ALTER TABLE — нужен rebuild). Файл: `infra/sqlite/migrations_v1_to_v2.py` (или внутри `migrations.py` как функция).
- **`bye.4`** P0 — `SmtpEmailNotifier` (manual STARTTLS + Message-ID). **ЖДЁТ `akv.7`** (state.db SMTP host SSOT) — проверить разблокирован ли.
- **`vgm.3`** P1 — import-linter CI контракты (ADR-006). Независимый от других P0/P1.
- **`8ov.1`** P1 — Composition: Infra + Services dataclasses (split Container).

**Заметки для оркестратора:**
- `akv.3` будет использовать `Migration` dataclass + `SqliteMigrationRunner` registry из akv.4. Pre-write extraction: ADR-019 §R4-M8 (полная цитата SQL невыполнимой версии) + SQLite docs про 12-step pattern.
- Параллельность: `akv.3` (`infra/sqlite/migrations*.py`) + `vgm.3` (`pyproject.toml` / `importlinter.cfg`) — разные файлы.
- **ОБЯЗАТЕЛЬНО**: writer ВСЕГДА через sub-agent (правило сессии #6). При сбое одного агента — пробовать другого (Senior Developer / Software Architect / general-purpose), НЕ писать самому.
- Reviewer ДО `bd close`, sonnet по дефолту.

### Memory заметки (новые в session #6)

- `backend-architect-skill-hijack` — Backend Architect sub-agent дважды самовольно запустил skill `fewer-permission-prompts`. Избегать его для writer-задач.
- `sub-agent-mandatory-for-writer-tasks` — в этом проекте писать код самому запрещено. При сбое sub-agent'а — переключаться на другого, не писать вручную.

---

## Где остановились (сессия #5, 13.05.2026)

**Закрыты 2 таски волны 2A (с одним раундом ревью) + 2 follow-up bd-issues.**
Closed: `akv.2` (init_db pre-flight user_version), `tic.1` (ThreadEventBus).

### Что сделано в сессии #5

1. **bd `akv.2` closed** (commit `aef8609`) — `init_db()` в `infra/sqlite/init_db.py`. Five-branch algorithm: fresh DB / ==latest / >latest RuntimeError / <latest+runner / <latest no-runner MigrationRequired. Post-runner verify user_version (Major 2 из ревью). Новое исключение `MigrationRequired(DomainError)` в `domain/errors.py` (PII-safe: from_version/to_version только, без путей). `MigrationRunner` — type alias `Callable[[Connection, int, int], None]`, не Protocol (брейншторм: конкретный класс в akv.3). 9 тестов. FIXME из `docs/architecture/03-protocols.md:177` снят.

2. **bd `tic.1` closed** (commit `8807fe6`) — `ThreadEventBus` в `infra/sse/bus.py`. Реализует `EventBus` Protocol. Routing по `event.priority` ClassVar: normal = put_nowait + drop-from-tail; critical = blocking put(timeout=2.0) + force-unsubscribe slow consumer + per-type slot update. Public helper `last_critical(EventClass)` (НЕ в Protocol — LSP friction задокументирован). MVP scope: **in-memory only** (расхождение с ADR-008 R3-C5 / 07-concurrency §7.3 которое требует state-table persistence с TTL=1h — отложено до создания StateRepository, follow-up `12y`). 19 тестов (включая AST-parse инвариант: bus.py не импортирует sqlite3).

3. **Follow-up bd-issues (P2)**:
   - `12y` — `StateRepository` для last_critical_event:* persistence (зависимость для расширения tic.1 до full canon).
   - `1zk` — `SqliteMigrationRunner` должен re-check user_version внутри своей первой BEGIN IMMEDIATE и raise `ConcurrentMigrationError` (TOCTOU defence-in-depth для akv.3, Major 1 из ревью akv.2).

4. **Оркестрация по playbook** (см. `bd memories orchestrator-playbook`):
   - Брейншторм-фаза (5-10 мин, сам, без sub-agent) перед стартом — фиксировались micro-decisions через AskUserQuestion.
   - Pre-write extraction-шаг в промптах writer-агентов (цитаты canon, grep, line-ranges).
   - 2 параллельных writer-агента (sonnet) — grep-пересечение файлов пусто (`infra/sqlite/` vs `infra/sse/`).
   - Reviewer (sonnet) ДО `bd close`. Оба ревью — APPROVE с Major'ами.
   - 2 параллельных fix-агента (sonnet). Все правки по canon, спорные deferred в follow-up bd.
   - `pytest` + `git show --stat` оркестратор прогонял сам — не верил summary sub-agent'ов.

5. **Vault changes**:
   - `docs/architecture/03-protocols.md` строка 177: FIXME → "Fixed in akv.2".
   - `docs/architecture/07-concurrency.md` §7.3: TODO-note про in-memory slots → follow-up `12y`.
   - `docs/glossary.md`: добавлены `MigrationRequired`, `init_db()`, `ThreadEventBus`; обновлена запись `MigrationRunner` (Protocol → текущий type alias + future Protocol в akv.3).
   - ADR не создавался (тривиальные impl-задачи без новых архитектурных решений).

### Состояние репо в конце сессии #5

- Working tree: clean (commit `8807fe6`)
- Branch: `master`
- Tests: **266 passed, 2 skipped**
- Ruff: чисто по новому коду (2 RUF001 legacy в `tests/domain/conftest.py`, известно)
- Git remote: НЕТ (local-only repo, `git push` не применим)
- bd: 10 closed, 68 open, 0 in_progress, 30 blocked, 38 ready

### С чего стартовать сессию #6

**Волна 2B — оставшаяся P0 + готовые P1:**

- **`akv.3`** P0 — `SqliteMigrationRunner v1→v2` через 12-step rebuild pattern (FIXME из ADR-019 ext). Зависит от akv.2 (закрыт) + см. follow-up `1zk` (re-check user_version внутри BEGIN IMMEDIATE).
- **`bye.4`** P0 — `SmtpEmailNotifier` с manual STARTTLS + Message-ID. Зависит от `akv.7` (state.db SMTP host SSOT) — проверить разблокирован ли.
- **`vgm.1`** P1 — pytest `tmp_db` fixture + factories. Нужен ещё до repository-тасок (akv.5/akv.6).
- **`vgm.3`** P1 — import-linter CI контракты (ADR-006).
- **`8ov.1`** P1 — Composition: Infra + Services dataclasses (split Container).

**Заметки для оркестратора:**
- `akv.3` — встроить в промпт writer'а инструкцию из `1zk` (re-check внутри runner's first BEGIN IMMEDIATE).
- Параллельность: `akv.3` (`infra/sqlite/migrations.py`) + `vgm.1` (`tests/conftest.py`) — разные файлы, можно параллельно.
- Reviewer **sonnet** по дефолту (правило пользователя).
- В каждом Agent-вызове **явно указывать `model: "sonnet"`** в коде + в текстовых апдейтах пользователю (правило пользователя сессии #5).

### bd-issues с устаревшими doc-refs

12+ open-issue упоминают `architecture.md` / `decisions-log.md` в description (атомарные доки в `docs/architecture/` и `docs/decisions/`). Список (актуализировано): `akv.3`, `8ov`, `8ov.4`, `a4t.1`, `tic`, `tic.3`, `bye.9`, `plg.3`, `vgm`, `vgm.5`, `12y`, `1zk`. Ссылки работают через stub-MOC, но при работе оркестратор должен подставлять конкретные `docs/architecture/<file>.md` / `docs/decisions/ADR-NNN-<slug>.md`.

---

## Где остановились (сессия #4, 13.05.2026)

**Закрыты 3 таски волны 1 (с двумя раундами ревью) + реструктуризация Obsidian vault + обновление DoD/playbook.**
Closed: `531.2` (Protocols interfaces.py), `akv.1` (ConnectionProvider), `0t8` (DiagnosticsExcludePolicy).

### Что сделано в сессии #4

1. **bd `531.2`/`akv.1`/`0t8` closed** — параллельная волна 1 на Sonnet writer-агентах. Первый раунд ревью (Sonnet) дал 3x NEEDS-WORK; fix-агенты (Sonnet) починили все blocker-ы; второй раунд ревью на **Opus** → 3x APPROVE.
   - `531.2`: 21 Protocol в `domain/interfaces.py`, 29 тестов. Закрыл follow-up `z9d` (перенос EventSubscription/ConfigSubscription из models.py). `Notifier` получил все 6 ClassVars (channel_id/display_name/description/config_schema/recipient_label/recipient_placeholder) — ADR-001 обновлён.
   - `akv.1`: `ConnectionProvider` в `infra/sqlite/connection.py` с `_closed` flag (исправлен M1: cross-thread `close_all()` теперь делает provider non-reusable, raise RuntimeError). PRAGMA по ADR-007. ADR-007 обновлён про `wal_autocheckpoint` defence-in-depth duplicate. 7 тестов.
   - `0t8`: `DiagnosticsExcludePolicy` в `services/diagnostics/exclude_policy.py`. **Critical fix**: добавлено `notifications.recipient` в EXCLUDED_DB_FIELDS (PII leak). Реализован `filter_state_keys()` (allowlist + forbidden-substring defence). 49 тестов.

2. **Vault restructuring** (branch `docs/restructure-vault`, merged `fb3ccd5` + fix `01305d0`):
   - `decisions-log.md` (635 строк) → 22 atomic ADR в `docs/decisions/ADR-NNN-<slug>.md` + stub-MOC.
   - `architecture.md` (1635 строк) → 14 atomic + stub-MOC в `docs/architecture/`.
   - `data-model.md` (428 строк) → 5 atomic + stub-MOC в `docs/data-model/`.
   - 17 root-доков перенесены в `docs/{web,ops,product,parser}/` через `git mv` (history preserved).
   - 460/463 wiki-link валидны (3 placeholder'а в CLAUDE.md инструкциях — не реальные линки).
   - Verifier-скрипт `.tools/vault_link_check.py` оставлен в репо.
   - Средний размер ноты — ~58 строк (vs 200-1635 раньше) — sub-agents теперь читают целиком.

3. **DoD актуализирован**: `docs/tasks/<bd-id>.md` запрещены (директория удалена); vault обновляется только при наличии новых знаний (ADR / glossary / architecture sections). См. `bd memories dod-per-task-includes-obsidian-vault-update-docs`.

4. **Orchestrator playbook записан** (после ретроспективы 1-го раунда ревью):
   - `bd memories orchestrator-playbook` — 9 правил (brainstorm перед таской, pre-write extraction-шаг, reviewer ДО close, параллельность только при пустом grep-пересечении, fake-impl method-call coverage, verify pytest/git show самим, reviewer для critical = opus).
   - `bd memories sub-agent-doc-reading` — sub-agents читают vault выборочно; цитировать canon в промпте, указывать атомарные файлы, pre-flight grep.

5. **Follow-up bd-issues созданы** (P3/P4, не блокеры):
   - `2uc` — race window в `ConnectionProvider._open` (`_closed` без lock).
   - `fx8` — `filter_state_keys` non-str key контракт не задокументирован.
   - `rbm` — IPv6 paths не редактируются в `redact_error`.

### Состояние репо в конце сессии #4

- Working tree: clean
- Branch: `master` HEAD = после fixes/merge (см. `git log`)
- Tests: 240+ passed, 2 skipped
- Ruff: чистый по новому коду (2 RUF001 в `tests/domain/conftest.py:31` — legacy от 531.1, кириллическое "ХК" в названии ОГВ, не блокер)
- Git remote: НЕТ (local-only repo, `git push` не применим)

### С чего стартовать сессию #5

**Волна 2 — P0 FIXME + параллельная фича:**

- **`akv.2`** P0 — `init_db()` pre-flight `PRAGMA user_version` check (FIXME из architecture.md §3.1 → ныне `docs/architecture/...`). Использует `ConnectionProvider` (закрытый).
- **`akv.3`** P0 — `MigrationRunner v1→v2` через 12-step SQLite rebuild pattern (FIXME из ADR-019 ext). Зависит от `akv.2`.
- **`tic.1`** P1 — `ThreadEventBus` (Layer 0, ADR-008) с `publish()` + priority routing + per-type slots. Параллельно с akv.2/akv.3 (разные файлы).

**Заметки для оркестратора (по playbook'у):**
- `akv.2` + `akv.3` — последовательно (akv.3 зависит от akv.2 фактически).
- `tic.1` — параллельно (grep-пересечение пусто).
- **Pre-write extraction**: для `akv.2` → выписать из `docs/architecture/03-protocols.md` и ADR-007 точные PRAGMA-инварианты; для `tic.1` → выписать из ADR-008 + `docs/architecture/07-concurrency.md` (или эквивалентный split-файл) все требования к EventBus.
- **Reviewer на sonnet** (по правилу пользователя — не opus). Critical-эскалация на opus — только при явном запросе.

### bd-issues с устаревшими doc-refs

12 open-issue упоминают `architecture.md` / `decisions-log.md` в description (см. `bd show` для каждого). Ссылки технически работают (stub-MOC существуют), но при работе над ними оркестратор должен mentally заменять на конкретные `docs/architecture/<file>.md` / `docs/decisions/ADR-NNN-<slug>.md`. Список: `akv.2`, `akv.3`, `8ov`, `8ov.1`, `8ov.4`, `a4t.1`, `tic`, `tic.3`, `bye.9`, `plg.3`, `vgm`, `vgm.5`.

---

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
2. `docs/architecture.md` — MOC, атомарные ноты в `docs/architecture/` (15 файлов, ~17 секций)
3. `docs/decisions-log.md` — MOC, атомарные ADR в `docs/decisions/` (ADR-001..022)
4. `docs/data-model.md` — MOC, атомарные ноты в `docs/data-model/` (lot/notifications/settings/sse/errors)
5. `docs/notifications.md` — state machine отправки
6. `docs/db/schema.sql` — канон схемы
7. `docs/onboarding.md` — FSM
8. `docs/ops/runbook.md` — known-limitations

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
