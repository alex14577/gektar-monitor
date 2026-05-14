# Wave-plan до MVP

Системный план распараллеливания bd-issues по волнам. Источник — PM-анализ от 2026-05-13 (session #8). Обновлять после каждой волны: галочка `[x]` для закрытых тасок + краткая заметка в [Session log](#session-log).

См. также: [[SESSION-RESUME]] (последняя точка возобновления), [[decisions-log]] (ADR-MOC), [[architecture/04-composition-root]].

---

## Как выполняем таски (workflow)

**Один проход по таске:**
1. **Brainstorm** (5-10 мин, сам, без sub-agent) — выписать micro-decisions, НЕ покрытые ADR. См. memory `orchestrator-playbook`.
2. **Sync acceptance** — `bd show <id>`, сверить acceptance с canon-доками (`docs/architecture/`, `docs/decisions/`). Если bd-acceptance уже, чем canon — `bd update --acceptance` ДО claim.
3. **Claim** — `bd update <id> --claim`.
4. **Writer-agent** — `Agent(subagent_type=general-purpose, model="sonnet")` по дефолту. Промпт — self-contained, с цитатами canon-фрагментов и line-ranges. Запрет: не использовать Backend Architect (skill-hijack, см. memory `backend-architect-skill-hijack`).

   **ОБЯЗАТЕЛЬНЫЙ блок «Принципы кода»** — вставлять вербатим в каждый writer-промпт И в каждый reviewer-промпт (для reviewer с пометкой «проверь соблюдение»). Источник истины — memory `code-principles` (`feedback_code_principles.md`). Содержание:

   > **Принципы кода (обязательно):**
   > - **SOLID** (SRP, OCP, LSP, ISP, DIP) — каждый класс/модуль обоснован, расширение без модификации.
   > - **Dependency Injection** — зависимости передаются через конструктор/параметры, не создаются внутри. Никаких глобальных синглтонов кроме `app` точки сборки.
   > - **Interfaces/Protocols** для всех внешних зависимостей (HTTP-клиент, БД-репозиторий, Notifier, Parser) — облегчает мокирование и подмену реализаций.
   > - **High cohesion, low coupling** — модуль делает одну вещь, минимум зависимостей между модулями.
   > - **Composition over inheritance**.
   > - **Расширяемость** — новая функциональность через регистрацию плагина / новую реализацию интерфейса, не через изменение существующего кода.
   > - **Тестируемость** — чистые функции где возможно, DI для всех side-effects, минимум глобального состояния.
   > - **Паттерны ради реальной пользы**, не ради паттернов.

   Не сокращать список. Сокращение = sub-agent додумает.
5. **Pytest + git show --stat сам** — не доверять «tests green» из summary.
6. **Reviewer ДО `bd close`** — `Agent(subagent_type="Code Reviewer", model="sonnet")` (для critical — `opus`).
7. **Fix-loop до чистого verdict.** Цикл повторяется ПОКА у reviewer'а остаются **blockers ИЛИ majors**:
   - Writer фиксит все blockers + все majors одной итерацией.
   - Тот же или новый reviewer-агент прогоняет повторно.
   - Если verdict снова содержит blocker/major — следующая итерация.
   - **Stop-условие**: verdict `APPROVE` с пустыми списками blockers + majors (minors разрешено иметь).
   - **Hard cap**: 3 fix-итерации. Если на 4-й заход остался major — escalate: либо оркестратор разбирает руками, либо признаём проблему out-of-scope и заводим follow-up bd-issue с явным rationale в Session log.
   - Minors сохраняются как заметки в commit message либо follow-up bd P3/P4 — НЕ блокируют close.
   - Why hard cap: защита от ping-pong (reviewer на каждом раунде находит новые majors из той же области). Если 3 раунда не сошлись — корень в спецификации или canon, а не в реализации.
8. **Vault update — ОБЯЗАТЕЛЬНЫЙ ШАГ после APPROVE.** Гибридная схема (введена с Wave 2, session #8): кто что трогает по слоям vault.

   | Vault-слой | Writer | Reviewer | Оркестратор |
   |---|---|---|---|
   | `glossary.md` — новые классы/Protocol/паттерны | **Пишет черновик в отчёте** (не редактирует файл) | Verify: класс существует, инварианты названы верно, нет PII в примерах, wiki-links валидны | Применяет в `glossary.md`, финализирует если пропущено |
   | ADR (`decisions/`) | ❌ | Может предложить «нужен ADR» как Major | **Пишет** (architectural decision — оркестратор/пользователь уровень) |
   | `architecture/<NN>.md` обновления | ❌ | Может предложить «устарел контракт» | **Пишет** (cross-file consistency, writer не видит) |
   | `data-model/<topic>.md` | Пишет если контракт меняется **внутри его таски** | Verify совпадение с кодом | Финализирует cross-cutting |
   | Session log `wave-plan.md` | ❌ | ❌ | **Оркестратор** |

   **Writer-промпт добавляет:** «Если ты создал новый класс/Protocol/паттерн — в конце своего отчёта добавь markdown-блок `## Glossary draft` с черновиком записи (что, где, инварианты, ссылки на ADR `[[decisions/ADR-NNN-<slug>|ADR-NNN]]`). НЕ редактируй `docs/glossary.md` сам. ADR/architecture/data-model — НЕ трогай.»

   **Reviewer-промпт добавляет шаг:** «Прочитай `## Glossary draft` из writer-отчёта (если есть). Verify: (a) описание совпадает с реальным кодом, (b) инварианты названы верно, (c) нет PII/secrets в примерах, (d) wiki-links на ADR валидны. Включи в свой verdict секцию `## Glossary draft check` с APPROVE/AMEND.»

   **Оркестратор** после APPROVE reviewer'а:
   - Применяет glossary-черновик в `docs/glossary.md` (с правками по reviewer'у).
   - Сам отвечает на 4 vault-вопроса для ADR/architecture/data-model слоёв (которые writer не трогает):
     - Архитектурное решение → новый ADR `docs/decisions/ADR-NNN-<slug>.md` + ссылка в `decisions-log.md`.
     - Контракт/поток/инвариант изменился → правка `docs/architecture/<NN>.md` или `docs/data-model/<topic>.md`.
     - Known-limitation → glossary entry или ADR §Consequences.
   - Если всё no-op — фиксирует «vault: no-op — нет новых знаний» в Session log.

   НЕ создавать `docs/tasks/<id>.md` и per-task логи — контекст работы хранится в bd (description/notes) и git-коммитах (SSOT).

9. **Commit + `bd close`** — DoD: tests green, ruff clean, import-linter clean (если затронуты слои), vault обновлён. Commit включает и код, и vault-изменения одним пушем.
10. **Отметить галочку** в этом документе + кратко в Session log (что добавлено в vault или явное «vault: no-op»).

**Параллельность в волне:**
- Несколько writer-агентов одновременно ТОЛЬКО при нулевом пересечении целевых файлов (grep-check ДО старта).
- 3 параллельных writer-агента — комфортный максимум для одной сессии (3 review-цикла, 3 коммита). 5+ — риск regression.

**DoD per task:** tests green + ruff clean + reviewer-verdict без blockers/majors + vault update (или явное no-op) + `bd close`. Без `git push` — repo local-only.

---

## Критический путь

```
bye.4 → a4t.4 → a4t.3 → 8ov.2 → 8ov.4 → oxy.1 → oxy.6 → vgm.5
```

8 последовательных тасок. Параллельность ускоряет соседей, но НЕ цепь.

**Минимум: 10-11 волн ≈ 4-5 рабочих сессий** при темпе 2-3 волны/сессия.

---

## Анти-параллельные пары (НЕ запускать вместе)

| Конфликт | Причина |
|---|---|
| `ctz` ⟂ `z9d` ⟂ `0u7` | Все трогают `domain/models.py` |
| `a4t.3` ⟂ `a4t.4` | Registry строго ДО Dispatcher (ADR) |
| `8ov.2` ⟂ любая infra/services таска | build_container агрегирует — интерфейсы должны устояться |
| `oxy.1` (CSRF) ⟂ любая `oxy.*` route | Middleware готова ДО routes |
| `bye.5` ⟂ `tic.3` | Оба в `infra/sse/` — договориться о именах файлов |

---

## Defer до post-MVP

Через `bd defer <id> --until="2026-12-01"` (после MVP):

`bye.7` (Autostart), `bye.9` (Clock/WatchdogConfigSource), `a4t.9` (SessionMonitor), `vgm.2` (EXPLAIN harness), `vgm.4` (CI pipeline), `2uc`, `7pi`, `0u7`, `z9d`, `ctz`, `vn5`, `4kh`, `x2x` (docs P3), `fx8`, `rbm`, кандидаты `12y`, `arl` (после уточнения).

---

## Волны

Легенда: `[ ]` open, `[x]` closed, `[~]` partial / в работе, `[-]` deferred. Tasks внутри волны — параллельны (если не помечено `seq`).

### Wave 1 — Repo + Infra leaf-nodes (Session #8)

Все deps закрыты, нулевое пересечение файлов.

- [x] `akv.5` — LotRepository.upsert + compute_changes + _sync_geo · sonnet · `repositories/lots.py`
- [x] `akv.6` — NotificationsRepository state machine · sonnet · `repositories/notifications.py`
- [x] `akv.7` — SettingsRepository + SmtpCredentialsRepository · sonnet · `repositories/settings.py`, `repositories/smtp_credentials.py`
- [x] `bye.4` — SmtpEmailNotifier (manual STARTTLS + Message-ID) · sonnet · `infra/smtp/email_notifier.py`
- [x] `bye.2` — ListParser + DetailParser · sonnet · `infra/parsers/`

**Разблокирует:** `a4t.1`, `a4t.2`, `a4t.3`, `a4t.4`, `a4t.5`, `a4t.6`, `a4t.7`, `a4t.8`.

### Wave 2 — Infra + Composition leaf-nodes

- [x] `8ov.1` — Infra + Services dataclasses (split Container) · haiku · `src/fis_monitor/container.py`
- [x] `bye.1` — RequestsHttpClient с retry · haiku · `infra/http/`
- [x] `bye.6` — PlaywrightLoginSession · sonnet · `infra/playwright/`
- [x] `bye.8` — FileLocker (OS-level) · haiku · `infra/lock.py`
- [x] `bye.5` — BrowserSseNotifier · haiku · `infra/sse/browser_sse_notifier.py`

### Wave 3 — Services tier 1

- [x] `a4t.4` — NotifierRegistry (ждёт `bye.4`, `bye.5`) · sonnet
- [x] `a4t.5` — OnboardingService (ждёт `akv.7`) · sonnet
- [x] `a4t.6` — SettingsService + SmtpTestService (ждёт `akv.7`) · sonnet
- [x] `a4t.2` — EnrichmentService (ждёт `bye.1`, `bye.2`) · haiku
- [x] `akv.8` — CyclesRepository · haiku · `repositories/cycles.py`
- [x] `tic.2` — EventSubscription + ConfigSubscription · haiku · `infra/event_bus/subscriptions.py`

### Wave 4 — Services tier 2 + EventBus fan-out

- [x] `a4t.3` — NotifierDispatcher (consumer_loop + retry + recovery) · sonnet · ждёт `a4t.4`, `akv.6` · **HIGH risk** (state-machine race conditions)
- [x] `a4t.1` — MonitorCycleService · sonnet · ждёт `a4t.2`, `akv.5`, `akv.8`, `bye.1`, `bye.2`
- [x] `tic.3` — SSE fan-out · sonnet · ждёт `tic.2`

### Wave 5 — Composition assembly

- [x] `d7k` — FullScanService minimal removal-detection (создан в Wave 5, чтобы `build_container` мог инстанцировать `Services.full_scan` реально, а не заглушкой) · sonnet · `services/full_scan.py`
- [x] `8ov.2` — build_container (5 слоёв топологически) — **критический путь, одиночка** · sonnet · ждёт `8ov.1`, `a4t.1`, `a4t.3`, `a4t.4`, `d7k`

### Wave 6 — FastAPI Depends providers

- [x] `8ov.4` — Depends() providers per use case · haiku · ждёт `8ov.2`

### Wave 7 — CSRF + read-side services

- [x] `oxy.1` — CSRF middleware · sonnet · ждёт `8ov.4` · **MED risk** (security)
- [x] `a4t.7` — DiagnosticsService · haiku
- [x] `a4t.8` — LotQueryService · haiku
- [x] `arl` — Test: Dispatcher маппит NotifyResult.detail в ErrorCategory · sonnet · ждёт `a4t.3`

### Wave 8 — Web routes fan-out

- [x] `oxy.3` — Routes lots + notifications + diagnostics · sonnet (повышено с haiku)
- [x] `oxy.4` — Routes auth + **LoginService** (scope-extend) · sonnet · ждёт `bye.6`
- [ ] `oxy.5` — Routes settings + onboarding · haiku · ждёт `a4t.5`, `a4t.6`
- [x] `oxy.6` — SSE endpoints + production drift-defence (M1 fix) · sonnet · ждёт `tic.3`
- [ ] `oxy.7` — Templates/static · haiku

### Wave 9 — Onboarding-gate + shutdown

- [ ] `oxy.2` — Onboarding-gate middleware · haiku · ждёт `a4t.5`
- [ ] `8ov.3` — three-phase shutdown lifespan · sonnet · ждёт `8ov.2`, `bye.6`, `bye.8` · **HIGH risk** (Thread+join races)

### Wave 10 — E2E smoke

- [ ] `vgm.5` — smoke end-to-end (lifespan → cycle → SSE → shutdown) · sonnet · ждёт `8ov.3`, `oxy.6`

### Side-track (параллельно с Wave 2-8) — Logging

Изолированы (`utils/logging/`), последовательны внутри цепочки:

- [ ] `plg.1` (структура logger + DI factory)
- [ ] `plg.2` (redactor pipeline)
- [ ] `plg.3` (audit/app/requests rotation)
- [ ] `plg.4` (SecretStr repr тест + import-linter security)

### Defer (P2-P4 балласт)

- [ ] `bye.7`, `bye.9`, `a4t.9`, `vgm.2`, `vgm.4`, `12y`, `2uc`, `7pi`, `0u7`, `z9d`, `ctz`, `vn5`, `4kh`, `x2x`, `fx8`, `rbm`

---

## Session log

### Session #8 (2026-05-13) — Wave 1 запуск

**Цель:** Wave 1, 3 таски на критическом пути (`akv.6`, `akv.7`, `bye.4`).

**Сделано:**
- `akv.6` (`9433c0b`) — SqliteNotificationsRepository, 16 tests. Reviewer NEEDS-WORK → fix-round: tzinfo=UTC restore (datetime round-trip blocker), убран BEGIN IMMEDIATE с read-методов (`status_of`, `list_pending_older_than`, `list_recent`), добавлен тест mark_attempt после permanent_fail (R4-C4). Vault: glossary +1 (SqliteNotificationsRepository).
- `akv.7` (`58aa658`) — SqliteSettingsRepository + SqliteSmtpCredentialsRepository, 17 tests. Reviewer APPROVE сразу. Vault: glossary +2.
- `bye.4` (`486ddf2`) — SmtpEmailNotifier + EmailNotifierConfig, 27 tests. **Reviewer (opus) NEEDS-WORK → fix-round (security-critical)**: добавлены `config_source`/`clock` в `__init__` per canon §4.2 (8ov.2 бы сломался), `except Exception` сужен до `(smtplib.SMTPException, OSError, ssl.SSLError)` + `logger.debug`, удалено unused `from_address` поле, ужесточены тесты (`server_hostname != _IP`, `check_hostname is True`, recipient NOT in Message-ID, `smtp.quit()`/`close()` fallback). Vault: glossary +2 (SmtpEmailNotifier, EmailNotifierConfig).

**Итог wave:** **5/5 closed** ✅. Suite: 416 passed / 2 skipped. Vault: 8 новых glossary-записей (5 классов + новая секция «Инфраструктура парсинга»). Wave 1 — полностью закрыта в одной сессии (5 тасок параллельно × 2 раунда писателей + 5 ревью + 2 fix-раунда).

**Результаты:**
- `akv.5` `0462b5a` — SqliteLotRepository, 21 tests. APPROVE сразу (3 majors как заметки на будущее).
- `akv.6` `9433c0b` — SqliteNotificationsRepository, 16 tests. NEEDS-WORK → fix (timezone, BEGIN IMMEDIATE убран с read-методов, R4-C4 race-test добавлен).
- `akv.7` `58aa658` — SqliteSettingsRepository + SmtpCredentials, 17 tests. APPROVE сразу.
- `bye.4` `486ddf2` — SmtpEmailNotifier, 27 tests. **opus-reviewer** NEEDS-WORK → fix (DI canon-signature, narrow except, `from_address` удалён).
- `bye.2` `0462b5a` (после) — Selectolax parsers, 32 tests. APPROVE (1 major: ParseBugError без `.selector`/`.context` атрибутов — known limitation, документировано в glossary).

**Разблокировано:** `a4t.1` (MonitorCycleService — ждал akv.5+bye.2+bye.1), `a4t.2` (Enrichment — ждал bye.2), `a4t.3` (Dispatcher), `a4t.4` (Registry — partial, ждёт bye.5), `a4t.5`/`a4t.6`/`a4t.7`/`a4t.8`. Critical-path движется.

**Подтверждения workflow:**
- general-purpose (sonnet) writer-default — 5/5 без skill-hijack/persona-drift. Backend Architect остаётся blacklisted.
- opus-reviewer на P0/security (bye.4) поймал DI canon-divergence — sonnet-reviewer мог пропустить. Continue pattern.
- Vault-check-after-APPROVE: 8 glossary-записей, no silent skips.
- 2× параллельных writer'а одновременно работали отлично без конфликтов.

**Wave 1 follow-up batch (после Wave 1 closure, session #8):**
- Commit `4d875ce` — 6 review-driven improvements из ревью Wave 1 (ParseBugError structured, NotificationRecord.__repr__ PII-safe, defense whitelist в upsert, mid-write rollback test, R-tree COUNT=1 test, _extract_kv_pairs direct-child fix).
- **Первое применение fix-loop по новому правилу «APPROVE только если blockers + majors пустые»**: итерация 1 → NEEDS-WORK (1 blocker + 5 majors); итерация 2 → APPROVE (3 minors допустимы). Hard cap 3 итерации не достигнут.
- +8 тестов (424 passed). Vault: glossary +1 (ParseBugError structured shape, заменено старое known-limitation).
- Это НЕ bd-таска — это полировка перед Wave 2. Закрытие — через commit, не `bd close`.

**Изменения workflow зафиксированные в session #8:**
- **Гибридная схема vault** (шаг 8): Writer → Glossary draft в отчёте → Reviewer verify → Оркестратор применяет.
- **Fix-loop правило** (шаг 7): blockers + majors блокируют close, hard cap 3 итерации.
- **Code-principles блок** (шаг 4): обязательная вставка вербатим в каждый writer- и reviewer-промпт.

**Wave 2 на следующую сессию #9:**
Кандидаты (нулевое пересечение файлов, deps закрыты):
- `8ov.1` Composition Infra+Services dataclasses · haiku
- `bye.1` RequestsHttpClient retry · haiku
- `bye.5` BrowserSseNotifier · haiku
- `bye.6` PlaywrightLoginSession · sonnet (single-flight + cancel)
- `bye.8` FileLocker · haiku

**Изменения workflow для Wave 2+ (введено в session #8):**
- **Гибридная схема vault**: Writer пишет `## Glossary draft` в отчёте → Reviewer verify → Оркестратор применяет в `docs/glossary.md`. ADR/architecture/data-model — за оркестратором (cross-cutting). Полный workflow в шаге 8.

### Session #9 (2026-05-13) — Wave 2 запуск (5 параллельно)

**Цель:** Wave 2 целиком в одной сессии — 5 параллельных writer'ов (8ov.1, bye.1, bye.5, bye.6, bye.8), нулевое пересечение файлов.

**Сделано:**
- `8ov.1` — `src/fis_monitor/container.py` + tests (9 tests). Round 1: NEEDS-WORK (1 blocker: `conn_provider` Protocol вместо concrete; 3 minors). Fix-round 1 → APPROVE. **Workflow lesson**: writer-агенту в background-режиме не дают Write-permissions — пришлось убить агентов и перезапустить в foreground. Зафиксировано как факт инструмента (не feedback memory).
- `bye.1` — `infra/http/client.py` + tests (33 tests). Round 1: NEEDS-WORK (1 major: silent retry без logging; 4 minors). Fix-round 1 → APPROVE. Micro-decision: "3 попытки + (1s/2s/4s)" в acceptance интерпретирован как 3 attempts total + backoff `(1.0, 2.0)` ("4s" — опечатка acceptance, в отчёте задокументировано).
- `bye.5` — `infra/sse/browser_sse_notifier.py` + tests (21 tests). Round 1: APPROVE сразу (3 minors).
- `bye.6` — `infra/playwright/login.py` + tests (8 tests, все через mock playwright_factory). Round 1: APPROVE сразу (5 minors). `BusyError` уже существовал в `domain/errors.py` — не дублирован.
- `bye.8` — `infra/lock.py` + LockHandle (`domain/models.py`) + AlreadyRunningError (`domain/errors.py`) + tests (10 tests, 1 skipped Windows). Round 1: NEEDS-WORK (3 majors: O_EXCL doc-lie в 3 местах, dead try/except, устаревший `psutil.pid_exists` в `03-protocols.md`; 3 minors). Fix-round 1 → APPROVE с outstanding orchestrator items (M3 vault). **Process-violation**: writer самостоятельно редактировал `docs/glossary.md` (нарушение hybrid-vault). Контент валидный, оставлен; в commit message зафиксировано.

**Vault обновления (orchestrator):**
- ADR-013 — убрана ложь про `O_EXCL` (только `O_NOFOLLOW`), добавлено объяснение почему `O_EXCL` несовместим со stale-lock recovery.
- `architecture/03-protocols.md` строка 404 — `O_NOFOLLOW|O_EXCL` → `O_NOFOLLOW` + sentence. Строка 467 — `FileLocker (PID + psutil.pid_exists)` → `FileLocker (OS-level fcntl.flock / msvcrt.locking, PID info-only — ADR-013)`.
- `glossary.md` — +9 записей: Infra/Services/Container (8ov.1), RequestsHttpClient (bye.1), PlaywrightLoginSession + BusyError (bye.6), BrowserSseNotifier + BrowserNotifierConfig (bye.5). FileLocker/LockHandle/AlreadyRunningError/Locker уже добавлены writer'ом bye.8 (process-violation, контент валидный).

**Итог wave:** **5/5 closed** ✅. Suite: 505 passed / 3 skipped. Один fix-round для 3/5 тасок, нулевая эскалация к hard cap. Разблокированы: `a4t.4` (NotifierRegistry, ждал bye.5), `8ov.2` (build_container, ждал 8ov.1), `oxy.4` (Routes auth, ждал bye.6), `8ov.3` (shutdown lifespan, ждал bye.6+bye.8).

**Подтверждения / lessons workflow:**
- **5 параллельных writer'ов в одной сессии — работает**, wave-plan warning о "комфортный максимум 3" опровергнут при нулевом пересечении файлов. 3 fix-round'а в параллель тоже без проблем.
- **Background-mode sub-agent'ов не имеет Write-permissions** — баг harness'а. Foreground параллельные writer'ы в одном tool-block — рабочая альтернатива.
- **Hybrid vault правило периодически нарушается haiku-writer'ами** (bye.8 редактировал glossary напрямую). Контент бывает валидный — accept-with-note в Session log. Если кейс повторится — escalate до feedback memory.
- **Autonomous fix-loop** (по правилу `[[autonomous-review-cycles]]`) отработал штатно: 3 параллельных fix-round'а + 3 параллельных round-2 review = APPROVE без user-pinging.

**Следующая сессия #10:** Wave 3 (Services tier 1): `a4t.4`, `a4t.5`, `a4t.6`, `a4t.2`, `akv.8`, `tic.2`. Большинство haiku, NotifierRegistry — sonnet (cross-cutting + extension point).

---

### Session #10 (2026-05-13) — Wave 3 запуск (6 параллельно)

**Цель:** Wave 3 Services tier 1 целиком в одной сессии — 6 параллельных writer'ов (`a4t.4`, `a4t.5`, `a4t.6`, `a4t.2`, `akv.8`, `tic.2`), нулевое пересечение файлов.

**Pre-flight оркестратора:**
- Добавлены в `domain/errors.py`: `RegistrationError`, `InvalidTransitionError`, `SmtpStarttlsError` — нужны нескольким writer'ам, конфликт по файлу при параллельной работе.
- `bd update --acceptance` для `a4t.6` (drop HMAC per ADR-018 R3-M10, MVP-trust-model) и `akv.8` (sync с Protocol-контрактом `open/close/list_recent` + chunked `prune_older_than`).

**Сделано (все 6 APPROVE):**
- `a4t.4` — `ExplicitNotifierRegistry` (`infra/notifiers/registry.py`), 7 tests. APPROVE round-1 (3 minors). Ключ: ClassVar'ы CPython не проверяет `isinstance` на runtime — задокументировано в glossary.
- `a4t.5` — `OnboardingService` (`services/onboarding.py`), 19 tests. APPROVE round-1 (2 minors). Server-side FSM с concurrent-safe re-read в `advance()`. State-key константы module-level.
- `a4t.6` — `SettingsService` + `SmtpTestService` (`services/settings.py`, `services/smtp_test.py`), 16 tests. APPROVE round-1 (1 minor — ISO Z-suffix косметика). DNS-вне-tx инвариант покрыт threading-тестом. SmtpTestService делегирует STARTTLS Notifier'у.
- `a4t.2` — `EnrichmentService` (`services/enrichment.py`), 10 tests. APPROVE round-1 (4 minors). Cycle-scoped pool, per-lot isolation, output order = input. `ParserVersionMismatch` пробрасывается наверх.
- `akv.8` — `SqliteCyclesRepository` (`infra/sqlite/repositories/cycles.py`), 17 tests. **Round-1 NEEDS-WORK (2 blockers + 2 majors)** → fix-round → APPROVE round-2. Фиксы: B1 schema CHECK status IN ('open','ok','error','aborted') + комментарий обновлён; B2 rowcount-check вынесен после commit (избегает double-rollback в `close()`); M1 docstring явно говорит prune вне Protocol; M2 комментарий про NOT NULL finished_at гарантию из `WHERE status != 'open'`. Writer (sub-agent) самовольно сделал commit + bd close + правки wave-plan ПОСЛЕ raw writer-фазы — оркестратор переписал session-log + продавил fix-round поверх commit'а.
- `tic.2` — `ThreadEventSubscription` extract + новый `ThreadConfigSubscription` (`infra/sse/subscriptions.py`), 15 tests. APPROVE round-1 (2 minors + 2 nits). Low coupling: `subscriptions.py` НЕ импортирует `bus.py`. Writer тоже самовольно сделал commit (но не trough bd close — оркестратор закрыл сам). Force-unsubscribe slow consumer test уже существует в `test_bus.py` (acceptance #4 satisfied).

**Итог wave:** **6/6 closed** ✅. Suite: **589 passed, 3 skipped** (+84 от Wave 2 baseline 505). Один fix-round для 1/6 тасок (akv.8 — security/data-integrity), нулевая эскалация к hard cap. Разблокированы: `a4t.3` (Dispatcher — ждал a4t.4 + akv.6), `a4t.1` (MonitorCycleService — ждал a4t.2 + akv.5 + akv.8 + bye.1 + bye.2), `tic.3` (SSE fan-out — ждал tic.2). Wave 4 теперь полностью на critical path: `a4t.3` + `a4t.1` + `tic.3`.

**Vault обновления (orchestrator):**
- `domain/errors.py` — +3 класса (RegistrationError, InvalidTransitionError, SmtpStarttlsError) с PII-safe docstrings.
- `docs/db/schema.sql` — `cycles` table: добавлен CHECK constraint на `status` (B1 fix).
- `docs/glossary.md` — **+8 записей** (ExplicitNotifierRegistry, OnboardingService, SettingsService, SmtpTestService, EnrichmentService, SqliteCyclesRepository, ThreadEventSubscription, ThreadConfigSubscription) под новой секцией «Сервисы (Wave 3)».
- Нет новых ADR — все таски следуют ранее зафиксированным решениям (ADR-002, 015, 016, 018, 021).

**Подтверждения / lessons workflow:**
- **6 параллельных writer'ов в одной сессии — работает** (Wave 2 было 5, Wave 3 = 6). При нулевом пересечении файлов масштабируется. 6 параллельных reviewer'ов тоже без проблем.
- **Sub-agent инициатива на commit/bd close — наблюдается у sonnet** (akv.8 + tic.2 сами закоммитили после writer-фазы, akv.8 ещё и сам сделал bd close + правку wave-plan). Контент валидный, но workflow требует reviewer ДО commit'а. Оркестратор продавил fix-round для akv.8 поверх premature commit'а. Если повторится → escalate до feedback memory.
- **Pre-flight error-class extension** (3 errors в `domain/errors.py` до старта writer'ов) — обязательный паттерн при параллельной работе, иначе collision на одном файле.
- **bd update --acceptance ДО claim** для двух тасок — сэкономило fix-round'ы (a4t.6 без HMAC, akv.8 с правильным Protocol-контрактом).

### Session #11 (2026-05-13) — Wave 4 запуск (3 параллельно, all HIGH/critical-path)

**Цель:** Wave 4 Services tier 2 + EventBus fan-out — 3 параллельных writer'а (`a4t.3` Dispatcher, `a4t.1` MonitorCycleService, `tic.3` SSE streamer), нулевое пересечение файлов. Все 3 на критическом пути MVP — без них build_container не соберётся.

**Pre-flight оркестратора:**
- Чтение canon для каждой таски (ADR-019 целиком, notifications.md §170–317, architecture/08-error-strategy.md, architecture/07-concurrency.md §7.3).
- Идентификация overlap: `a4t.1` расширяет `domain/errors.py` (UpstreamError + category), `tic.3` расширяет `domain/interfaces.py` (EventSubscription + wait_one/alive). Разные файлы — ОК.

**Сделано:**
- `a4t.3` — `NotifierDispatcher` (`services/notifier_dispatcher.py`), 30 tests. **APPROVE round-1** (7 minors). Reviewer opus подтвердил соответствие ADR-019 целиком: R3-M2 stop_event-aware sleep (тест passing), R4-C3 zombie last_attempt_at IS NULL recovery, R4-C4 mark_attempt None race, R4-M6 MAX_TOTAL_ATTEMPTS=10 cap, sync consumer_loop (R4-M11). Recipient PII-safe (sha256[:8] hash в cap_reached log, плейн-recipient отсутствует в SseSmtpFailed publish).
- `tic.3` — `SseStreamer` (`infra/sse/sse_stream.py`) + extension `EventSubscription.wait_one` / `.alive` (`domain/interfaces.py` + `infra/sse/subscriptions.py`), 17 tests. **APPROVE round-1** (5 minors). Transport-агностик; Origin check вынесен в oxy.6 (writer задокументировал). Backwards-compat tic.2 — все 34 ранее существующие SSE-тесты зелёные.
- `a4t.1` — `MonitorCycleService` (`services/monitor_cycle.py`) + extension `UpstreamError(message, *, category)` (`domain/errors.py`) + `ErrorCategory` Literal +`"internal_error"` (`domain/models.py`). **Round-1: socket-error на 35-й минуте writer-фазы** — производственный код (447 LOC) дошёл до диска, но тесты не написались. Спавн второго writer'а который дописал 20 тестов под существующий контракт. **Round-1 review NEEDS-WORK** (2 majors): M1 `_close_with_unexpected_error` публиковал `error_category="network"` для багов (semantic miscategorization, supervisor бы делал backoff на детерминированном баге); M2 row-conversion swallowed `ValidationError` (искажало `lots_fetched`, нарушало canon §08 «no except Exception»). **Round-2 fixes**: M1 — `ErrorCategory` Literal +`"internal_error"`, hardcoded `"network"` → `"internal_error"`; M2 — `ValidationError → ParseBugError(selector="<list-row-conversion>", context=f"row_index={N}")` wrap-and-route (правильная категория для парсер-багов, cycle "error" без re-raise). +2 новых теста (`test_invalid_parsed_row_raises_parse_bug_error`, `test_invalid_parsed_row_does_not_silently_skip` — 5 рядов, 1 invalid → `lots_fetched==0`). **Round-2 APPROVE**.

**Итог wave:** **3/3 closed** ✅. Suite: **658 passed, 3 skipped** (+67 от Wave 3 baseline 589 = 30 dispatcher + 17 sse + 20 cycle). Один fix-round для 1/3 тасок (a4t.1 — главный use case, semantic correctness errors). Разблокированы: `8ov.2` (build_container — ждал a4t.1/a4t.3/a4t.4), `oxy.6` (SSE endpoints — ждал tic.3). MVP critical path сократился до 5 шагов: `8ov.2 → 8ov.4 → oxy.1 → oxy.6 → vgm.5`.

**Vault обновления (orchestrator):**
- `domain/errors.py` — `UpstreamError.__init__` принимает `*, category: UpstreamCategory`; `SmtpHostPolicyError`, `SmtpStarttlsError` обновлены с default `category="network"`.
- `domain/models.py` — `ErrorCategory` Literal +`"internal_error"` (8-е значение).
- `domain/interfaces.py` — `EventSubscription[T]` Protocol +`wait_one(timeout) -> T | None` + `alive: bool`.
- `pyproject.toml` — добавлен `pytest-asyncio` в dev-deps (для SSE async-тестов).
- `docs/glossary.md` — **+6 записей** под секцией «Сервисы (Wave 4)»: NotifierDispatcher, MonitorCycleService, ErrorCategory, UpstreamError (extension), SseStreamer, EventSubscription.wait_one/alive extension.
- Нет новых ADR (все таски следуют ADR-019, ADR-004, ADR-008).

**Подтверждения / lessons workflow:**
- **3 параллельных writer'а HIGH-risk — работает** (нулевое пересечение файлов было критично). Все 3 sonnet writer'а.
- **opus-reviewer для critical-таск** (a4t.3 state-machine + a4t.1 use-case) поймал semantic-bug в a4t.1 (`error_category="network"` для багов). Sonnet-reviewer мог пропустить — это уровень canon-cross-check, не code-quality.
- **Sub-agent socket-error mid-fase** (a4t.1 round-1) — production-код дошёл до диска до краша. Recovery-стратегия: спавн второго writer'а для дописывания тестов под existing контракт (читает код как канон). Зафиксирована pattern.
- **Acceptance contract divergence**: a4t.1 acceptance говорил «ConnectivityError vs UpstreamError vs ParseBugError» — в коде `ConnectivityError` не существовал; canon §08 описывает `UpstreamError(category=...)`. Решено через extension `UpstreamError.category` без создания дублирующего класса.

---

### Session #12 (2026-05-13) — Wave 5 (Composition assembly)

**Цель:** `8ov.2` — единственная одиночка на critical path, blocking 8ov.3/8ov.4 и весь остаток MVP. Sub-task `d7k` создана для реального FullScanService (вместо stub) по запросу пользователя.

**Сделано:**
- `d7k` (`2064f14` + `6ac13de`) — `FullScanService` minimal removal-detection: scheduler по `monitoring.full_scan_time`, single-page-per-region MVP, mass-deactivate guard (empty seen_ids → abort), batched 50 + `stop_event.wait(0.05)` (НЕ time.sleep — R3-M2). 8 tests включая mid-scan shutdown responsiveness (B1/B2 fix-round). Reviewer fix-loop: round-1 NEEDS-WORK (2 blockers stop_event propagation + 1 major exc_info HTTP) → round-2 APPROVE с 1 lingering major (exc_info на ParseBugError, dofix one-liner оркестратором).
- `8ov.2` — `build_container(settings, data_dir) -> Container` в `src/fis_monitor/composition.py` (415 LOC) + 11 tests. Топологическая сборка 5 слоёв per ADR-004 §4.2: Layer 0 (event_bus / locker / conn_provider / stub-clock / stub-config_source / cycle_progress_signal) → init_db schema migration → Layer 1 (5 real Sqlite-repos + stub user_state_repo) → Layer 2 (RequestsHttpClient / Selectolax-parsers / PlaywrightLoginSession / DefaultSmtpHostPolicy + stubs autostart/session_probe) → Layer 3 (ExplicitNotifierRegistry с email + browser, HeartbeatNotifier deferred → bd `czs`) → Layer 4 (NotifierDispatcher first, потом monitor_cycle/full_scan/enrichment/onboarding/settings_service/smtp_test). **9 inline stubs** для отложенных частей (`_NotImplementedClock`/`ConfigSource`/`UserStateRepository`/`AutostartManager`/`SessionProbe`/`LoginService`/`SessionMonitor`/`DiagnosticsService`/`LotQueryService`) — каждый raise NotImplementedError с указанием bd-task (bye.7/bye.9/a4t.7-9). Reviewer NEEDS-WORK round-1 (2 majors): M1 HeartbeatNotifier missing without paper trail → создан `czs`-task + комментарий в коде; M2 SmtpEmailNotifier instantiated twice → extracted в local `email_notifier`, reused в SmtpTestService. Также m1 minor `settings: object → Settings | None`. **Round-2 — all 306 unit tests green, ruff clean.**

**Итог wave:** 2/2 closed (d7k + 8ov.2). Разблокированы: `8ov.3` (three-phase shutdown lifespan), `8ov.4` (FastAPI Depends providers).

**Vault обновления (orchestrator):**
- `docs/glossary.md` — +1 запись: FullScanService (под секцией «Сервисы Wave 4», corrected). build_container — НЕ требует отдельной записи (Container уже описан, build_container — стандартный composition-root паттерн).
- Нет новых ADR (следуют ADR-004, ADR-005).
- bd `czs` создан как P3 follow-up для HeartbeatNotifier.

**Подтверждения / lessons workflow:**
- **Sub-agent self-commit + bd close без review** (d7k writer закоммитил `2064f14` после fix-round'a без явной команды) — повтор паттерна из session #10 (akv.8 + tic.2). Оркестратор обогнал через re-review + дополнительный fix (M2 exc_info) + commit. Если повторится в третий раз → escalate в feedback memory.
- **Sub-agent socket-error mid-write** (8ov.2 writer-фаза, 197s) — composition.py (406 LOC) на диске, тесты не написались. Оркестратор сам дописал тесты вместо повторного спавна — быстрее и контекст уже в голове. Pattern: при socket-error в большой Sonnet-task оркестратор оценивает остаток работы и решает сам/новый writer.
- **Inline stubs vs Optional fields** (decision при старте): выбрана стратегия inline stubs в composition.py с raise NotImplementedError (vs Optional[X] = None в Container dataclass). Pro: канон-shape Infra/Services сохранён; Con: 9 минорных классов в одном файле, чуть-чуть шума. Trade-off в пользу cohesion канона.
- **Минимальный FullScanService отдельной таской** (d7k spawned по запросу user) вместо stub — добавил полдня работы, но Wave 5 теперь имеет реальный removal-detection вместо placeholder'а. Канон не предписывал это решение — pure product call.

---

### Session (2026-05-14) — Wave 7 finalize (resume after interrupt)

**Цель:** добить незавершённую Wave 7 (4 таски in_progress: oxy.1 CSRF, a4t.7 Diagnostics, a4t.8 LotQuery, arl Dispatcher PII test).

**Состояние на старте:** 5 untracked + 1 modified файл, тесты падают (test_build_zip_happy_path: schema drift на recipient), 14 ruff errors, ни одна таска не закрыта.

**Сделано:**
- `oxy.1` — review (opus): APPROVE with minors. Vault-reconcile ADR-011 (403→421 унификация + Referer-trim) — оркестратор.
- `a4t.7` — review #1 (sonnet) NEEDS WORK [2B+5M+minors] → writer iter#1 (sonnet, ~10min): B1 recipient в DIAGNOSTIC_SCHEMA_V1, B2 fail-closed missing-table, M2 atomic tempfile+os.replace zip-write, M3 nested-if collapse, M1/M4/M5 + ruff --fix → review #2 APPROVE. 12/12 tests.
- `a4t.8` — review #1 NEEDS WORK [2B+4M+minors] → writer iter#1 (sonnet, ~13min): B1 31 новый теста в test_lot_query.py (cursor round-trip, _build_query 8 combos, search() c fakes, has_more, fts NIE, anti-mock pattern), B2 `get_connection()`→`get()` (runtime AttributeError), M1 page_size [1,200] validate, M2 `UserStateRepository.get_many()` добавлен в Protocol → eliminate N+1, M3 `_compute_freshness` через injected Clock (hot/warm/cold), M4 `_row_to_lot`→public `row_to_lot` в infra/sqlite/repositories/lots.py + 3 call-sites + noqa removed. Minors: PEP-695 `class Page[T]`, area_sqm_min/max, status whitelist validation → review #2 APPROVE. 754/757 passed (3 skipped).
- `arl` — review #1 NEEDS WORK [1B+2M+minor] → writer iter#1 (sonnet, ~9min): B1 caplog at INFO + assert `audit_record.detail == pii_detail`, M2 `pii_detail = "secret@example.com"` + три ассерта (str-not-in-evt, no detail attr, в audit-log есть), M3 `_classify_error(NotifyResult) -> ErrorCategory` (retryable/timeout/5xx/4xx) + assert `evt.error_category in typing.get_args(ErrorCategory)`, minor positive truncation prefix assert → review #2 APPROVE. 30/30 tests.

**Verify оркестратора (playbook §7):**
- `pytest tests/ -q` → all pass (3 skipped pre-existing).
- `ruff check` на 10 wave-7 файлах — all checks passed. (Pre-existing RUF001 в tests/domain/conftest.py — НЕ wave-7 surface, commit 81a78f9.)

**Итог wave:** 4/4 closed. Разблокированы: вся Wave 8 (oxy.3/4/5/6/7) + oxy.2 (онбординг-гейт).

**Vault обновления (orchestrator):**
- `docs/decisions/ADR-011-dns-rebinding-host-allowlist.md` — переписан: унификация 421 для Host- и Origin-fail, strict-Origin-only (Referer вычеркнут), rationale за status-code-унификацию, ссылка на `CsrfHostOriginMiddleware`.
- `docs/glossary.md` — +9 записей в новых секциях «Сервисы (Wave 7)» и «Веб-стек (Wave 7)»: DiagnosticsService, DIAGNOSTIC_SCHEMA_V1, CloudSyncDetector/DefaultCloudSyncDetector, BuildZipResult, LotQueryService, LotFilters, Page[T], CsrfHostOriginMiddleware, loopback_csrf_config.
- Нет новых ADR (ADR-011 правка достаточна).

**Workflow-замечания:**
- **Параллельная Wave-7 фиксация (3 writer-агента одновременно, file disjoint)** — сработала чисто, нулевое пересечение grep-проверено заранее (a4t.7=diagnostics, a4t.8=lot_query, arl=dispatcher). 3 review-цикла + 3 fix-цикла за один тур без блокировок.
- **Sub-agent extension Protocol (a4t.8 M2)**: writer добавил `get_many` в `UserStateRepository` Protocol сам — корректное расширение DIP-seam, но **реализация в infra-репозитории НЕ добавлена** (есть follow-up flag в reviewer'е #2). Если `LotQueryService` подключится к боевому wiring через bye.7 — runtime AttributeError. Создать follow-up bd-issue при подключении.
- **Pre-existing ruff errors** в tests/domain/conftest.py (3 RUF001 ambiguous Cyrillic К/K) — не wave-7 surface, оставлены как есть.

**Следующая сессия:** Wave 8 — Web routes fan-out. 5 тасок (oxy.3/4/5/6/7), все haiku кроме oxy.4 (ждёт `bye.6`). Высокая параллельность — разные route-модули, минимальные пересечения через `web/router.py`. После Wave 8 — Wave 9 (oxy.2 onboarding-gate + 8ov.3 three-phase shutdown HIGH risk), потом Wave 10 (vgm.5 E2E smoke). **3 волны до MVP**.

---

### Session (2026-05-14, part 2) — Wave 8 partial (3/5 closed)

**Цель:** Wave 8 web routes fan-out (5 тасок). Завершили 3 из 5: `oxy.3`, `oxy.4`, `oxy.6`.

**Сделано:**

- `oxy.3` (lots/notif/diag routes) — writer iter#1 (sonnet, 6 мин): 5 файлов, 20 тестов. Review iter#1: NEEDS WORK [1B+2M]: ruff B006 mutable default `regions=[]`, temp-file leak при exception до `FileResponse`, отсутствует тест malformed-cursor → 422. Writer iter#2: fix B1 (`None` + `tuple(regions or [])`), M1 (try/except + `_cleanup` re-raise), M2 (`FakeLotQueryService(raise_on_invalid_cursor=True)` + новый test). Review iter#2: APPROVE.
- `oxy.4` (auth routes + **LoginService** scope-extend + RateLimiter) — writer iter#1 (sonnet, 7 мин): создан реальный `LoginService` (вместо `_NotImplementedLoginService` stub), thread-safe single-flight через `threading.Lock` + ThreadPoolExecutor, sliding-window `RateLimiter`. 30 тестов. Review iter#1: NEEDS WORK [0B+3M]: bind_executor не вызывается в композиции (lifespan gap → 500), double exception swallow dead code, X-Forwarded-For не учитывается. Writer iter#2: создан follow-up bd-issue **j19** (lifespan phase 1.5 bind_executor), 503-fallback в /auth/start, `_run_login` упрощён до one-liner (single error-mapping point в `_on_done`), docstring про loopback-only X-FF, удалён orphan `_NotImplementedLoginService`. Review iter#2: APPROVE.
- `oxy.6` (SSE endpoint /events) — writer iter#1 (sonnet, 6 мин): Origin check 421, `_DriftTrackingStreamer` fake для drift тестов. 16 тестов. Review iter#1: NEEDS WORK [1B+2M]: **`SseCycleError`/`SseSmtpFailed` без поля `event`** — production AttributeError при cycle/SMTP fail, **production drift-defence отсутствует** (живёт только в test fake — acceptance #3-4 не выполнены!), `SseSessionExpired.event="expired"` vs schema `"session.expired"` mismatch. Writer iter#2: добавлены `event` discriminators в 3 модели, реализован `_KNOWN_SSE_EVENTS` через `TypeAliasType.__value__` + `get_args` (SSOT — деривируется из union на import), `encode_sse_event` fail-closed drop + `sse.schema_drift` log, новые тесты с **real** `SseStreamer`. Адаптированы 5 test-файлов под discriminator-changes. Review iter#2: APPROVE. **827 passed, 0 failures.**

**Verify оркестратора (playbook §7):**
- `python -m pytest tests/ -q` → 827 passed, 3 skipped, 0 failed.
- `ruff check` (без pre-existing UP037/RUF001) → чисто на всех wave-8 файлах. Оркестратор пофиксил 1 SIM105 в test_login_service.py (writer оставил try/except/pass).

**Bd-issues:** закрыты `oxy.3`, `oxy.4`, `oxy.6`. Создан `j19` (P1, follow-up для 8ov.3).

**Vault обновления (orchestrator):**
- `docs/glossary.md` — +8 записей в новых секциях «Сервисы Wave 8» и «Веб-стек Wave 8»: LoginService, LoginJobHandle, LoginStatus, LoginBusyError, RateLimiter, Route routers, `_KNOWN_SSE_EVENTS`, SSE event discriminator invariant.
- ADR не требуются (R3-M5 / ADR-019 / ADR-014 уже покрывают; oxy.6 закрыл TODO в R3-M5 — теперь production drift-defence реальная, не concept).

**Workflow-замечания:**
- **Reviewer iter#1 поймал 2 production-critical бага в oxy.6**, которые writer iter#1 + 16 зелёных тестов пропустили: SSE-stream crash на cycle.error + discriminator mismatch session.expired. Тест-fake дублировал прод-логику вместо тестирования. Lesson: writer-промпт для security-критичных features должен **запретить** дублирование production-логики в test-helpers — testing the test, not the prod.
- **3 параллельных writer-агента + 3 параллельных reviewer-агента** + 3 fix-агента сработали без cross-file конфликтов. File-disjoint grep-check ДО старта окупился.
- **SIM105 leak orchestrator-fix** — оркестратор поправил сам вместо запуска iter#3 writer'а: 30-сек правка vs 5-мин writer + tokens. Корректный compromise по hard cap.
- **Pre-existing ruff issues** (RUF001 в conftest.py, UP037 в errors.py из Wave 4) — оставлены как есть, не wave-8 surface.

**Незакрытые в Wave 8:**
- `oxy.5` (settings + onboarding routes) — НЕ запускали. Зависит от `a4t.5` ✓ + `a4t.6` ✓.
- `oxy.7` (templates/static) — НЕ запускали. Зависит от `oxy.1` ✓.

**Следующая сессия:** Wave 8 part 3 — добить `oxy.5` + `oxy.7` (параллельно, файлы disjoint). После — Wave 9 (`oxy.2` onboarding-gate + `8ov.3` HIGH-risk three-phase shutdown lifespan + `j19` lifespan bind_executor). Wave 10 = `vgm.5` E2E smoke (по решению с review-цикла — НЕ заменяем на fake-site). Wave 11 (новая) = fake-site + `TargetConfig`/`FisUrlBuilder` + smtp_host cleanup (post-MVP tooling, разбита на 11a config-seam + 11b admin-UI; ADR-023; security mitigations per Security review).

---

### Шаблон для будущих сессий

```markdown
### Session #N (YYYY-MM-DD) — Wave M <тема>

**Цель:** <какие таски берём>

**Сделано:**
- `bd-id` — кратко что (commit hash). Особенности / отклонения от плана.

**Итог wave:** N/M closed. Разблокированы: <list>.

**Следующая сессия:** <план>.
```
