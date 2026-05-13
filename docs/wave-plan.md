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

- [ ] `8ov.1` — Infra + Services dataclasses (split Container) · haiku · `composition/`
- [ ] `bye.1` — RequestsHttpClient с retry · haiku · `infra/http/`
- [ ] `bye.6` — PlaywrightLoginSession · sonnet · `infra/playwright/`
- [ ] `bye.8` — FileLocker (OS-level) · haiku · `infra/locker/`
- [ ] `bye.5` — BrowserSseNotifier · haiku · `infra/sse/browser_sse_notifier.py` (договориться об именах с `tic.3`)

### Wave 3 — Services tier 1

- [ ] `a4t.4` — NotifierRegistry (ждёт `bye.4`, `bye.5`) · sonnet
- [ ] `a4t.5` — OnboardingService (ждёт `akv.7`) · sonnet
- [ ] `a4t.6` — SettingsService + SmtpTestService (ждёт `akv.7`) · sonnet
- [ ] `a4t.2` — EnrichmentService (ждёт `bye.1`, `bye.2`) · haiku
- [ ] `akv.8` — CyclesRepository · haiku · `repositories/cycles.py`
- [ ] `tic.2` — EventSubscription + ConfigSubscription · haiku · `infra/event_bus/subscriptions.py`

### Wave 4 — Services tier 2 + EventBus fan-out

- [ ] `a4t.3` — NotifierDispatcher (consumer_loop + retry + recovery) · sonnet · ждёт `a4t.4`, `akv.6` · **HIGH risk** (state-machine race conditions)
- [ ] `a4t.1` — MonitorCycleService · sonnet · ждёт `a4t.2`, `akv.5`, `akv.8`, `bye.1`, `bye.2`
- [ ] `tic.3` — SSE fan-out · sonnet · ждёт `tic.2`

### Wave 5 — Composition assembly

- [ ] `8ov.2` — build_container (5 слоёв топологически) — **критический путь, одиночка** · sonnet · ждёт `8ov.1`, `a4t.1`, `a4t.3`, `a4t.4`

### Wave 6 — FastAPI Depends providers

- [ ] `8ov.4` — Depends() providers per use case · haiku · ждёт `8ov.2`

### Wave 7 — CSRF + read-side services

- [ ] `oxy.1` — CSRF middleware · sonnet · ждёт `8ov.4` · **MED risk** (security)
- [ ] `a4t.7` — DiagnosticsService · haiku
- [ ] `a4t.8` — LotQueryService · haiku
- [ ] `arl` — Test: Dispatcher маппит NotifyResult.detail в ErrorCategory · sonnet · ждёт `a4t.3`

### Wave 8 — Web routes fan-out

- [ ] `oxy.3` — Routes lots + notifications + diagnostics · haiku
- [ ] `oxy.4` — Routes auth · haiku · ждёт `bye.6`
- [ ] `oxy.5` — Routes settings + onboarding · haiku · ждёт `a4t.5`, `a4t.6`
- [ ] `oxy.6` — SSE endpoints · haiku · ждёт `tic.3`
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
