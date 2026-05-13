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
5. **Pytest + git show --stat сам** — не доверять «tests green» из summary.
6. **Reviewer ДО `bd close`** — `Agent(subagent_type="Code Reviewer", model="sonnet")` (для critical — `opus`).
7. **Writer фиксит blocker'ы** → reviewer прогоняет повторно.
8. **Vault update — ОБЯЗАТЕЛЬНЫЙ ШАГ после APPROVE.** Сразу после approve-ревью, ДО `bd close`, явно ответить на 4 вопроса и обновить vault если хоть один = да:
   - Принято ли архитектурное решение, которого нет в ADR? → новый `docs/decisions/ADR-NNN-<slug>.md` + ссылка в `decisions-log.md`.
   - Введён ли новый термин / класс / Protocol / паттерн? → запись в `docs/glossary.md`.
   - Изменился ли контракт / поток / инвариант, описанный в `docs/architecture/<NN>.md` или `docs/data-model/<topic>.md`? → обновить соответствующий файл.
   - Появилось ли known-limitation / non-obvious trade-off, который стоит зафиксировать? → glossary или соответствующий ADR §Consequences.

   Если ответ на все 4 — **нет** (тривиальный fix без новых знаний), явно записать в Session log: «vault не трогаем — нет новых знаний». Это форсирует осознанный check, а не silent skip.

   НЕ создавать `docs/tasks/<id>.md` и per-task логи — контекст работы хранится в bd (description/notes) и git-коммитах (SSOT).

9. **Commit + `bd close`** — DoD: tests green, ruff clean, import-linter clean (если затронуты слои), vault check выполнен (см. шаг 8). Commit включает и код, и vault-изменения одним пушем.
10. **Отметить галочку** в этом документе + кратко в Session log (с пометкой что добавлено в vault или явное «vault: no-op»).

**Параллельность в волне:**
- Несколько writer-агентов одновременно ТОЛЬКО при нулевом пересечении целевых файлов (grep-check ДО старта).
- 3 параллельных writer-агента — комфортный максимум для одной сессии (3 review-цикла, 3 коммита). 5+ — риск regression.

**DoD per task:** tests green + ruff clean + `bd close` + (optional) vault update. Без `git push` — repo local-only.

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

- [ ] `akv.5` — LotRepository.upsert + compute_changes + _sync_geo · sonnet · `repositories/lots.py`
- [x] `akv.6` — NotificationsRepository state machine · sonnet · `repositories/notifications.py`
- [x] `akv.7` — SettingsRepository + SmtpCredentialsRepository · sonnet · `repositories/settings.py`, `repositories/smtp_credentials.py`
- [x] `bye.4` — SmtpEmailNotifier (manual STARTTLS + Message-ID) · sonnet · `infra/smtp/email_notifier.py`
- [ ] `bye.2` — ListParser + DetailParser · sonnet · `infra/parsers/`

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

**Итог wave:** 3/5 closed (akv.5 и bye.2 — на Wave 1 продолжение или Wave 2). Suite: 363 passed / 2 skipped. Vault: 5 новых glossary-записей. Все 3 таски потребовали fix-round после первого review — reviewer-ДО-close механика спасла от security blocker (server_hostname binding) и data-corruption blocker (naive datetime в БД).

**Разблокировано Wave 1:** `a4t.3` (Dispatcher ждал akv.6) — теперь ready. `a4t.5`/`a4t.6` (ждали akv.7) — ready. `a4t.4` (ждал bye.4 + bye.5) — partial (нужен ещё bye.5).

**Следующая сессия #9:** Wave 1 хвост (`akv.5` lots-repo + `bye.2` parsers) либо сразу Wave 2 leaf-nodes (`8ov.1`, `bye.1`, `bye.5`, `bye.6`, `bye.8`). Параллельные writer-агенты × 3-5 — workflow подтверждён рабочим.

**Заметки для следующего оркестратора:**
- general-purpose (sonnet) как writer-default — **подтверждено**: 3/3 агента отработали без skill-hijack или persona-drift. Backend Architect остаётся в чёрном списке.
- opus-reviewer на security-critical (bye.4) поймал blocker (`server_hostname=endpoint.original_host` правильно — но canon-divergence в DI signature который sonnet-reviewer мог упустить). Continue this pattern: P0/security → opus, остальное → sonnet.
- vault-check-after-APPROVE workflow (шаг 8) сработал: для каждой APPROVE-ed таски добавлены glossary-записи. No silent skips.

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
