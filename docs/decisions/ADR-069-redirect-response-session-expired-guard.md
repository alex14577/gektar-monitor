# ADR-069 — 3xx-redirect guard перед парсером: HTTP 302 от донора маппится в SessionExpiredError

**Status**: Accepted (bd gektar-monitor-31y)
**Date**: 2026-06-05
**Deciders**: Backend
**Tags**: session-expired, fetcher, monitor-cycle, parsing, error-strategy, redirect

---

## Context

Донор на неавторизованный запрос lot-list отвечает **HTTP 302** с пустым телом — редирект на
логин-страницу ЕСИА с токеном в Location/URL. При протухшей сессии `requests.Session` следует
редиректу (по умолчанию), но финальный ответ может возвращаться как 2xx с пустым HTML либо
как 3xx при частичном следовании. В прод-диагностике 2026-06-05 зафиксировано:
`status=302, text_len=0` (конфигурация без auto-follow).

**Прежнее поведение:** пустой `text` доходил до `SelectolaxListParser.parse()`:
- ESIA title-check: `<title>` отсутствует → не срабатывает.
- missing `<tbody>` → `ParseBugError("parse bug: selector='tbody' context='...'")`.

Категория `parse_bug` попадала в `SseCycleError` и `cycles.error`. Backfill аборировал с
`ParseBugError` (галка `backfill.done` не ставилась — ADR-068 §3). `SessionExpiredError` не
поднимался → `SseSessionExpired` не публиковался → `session_expired_email.py` молчал,
пользователь не получал уведомление о необходимости релогина.

`SessionMonitor` (ADR-046) публиковал `SseSessionExpired` независимо при следующем probe, но
в окне между 302 и probe-циклом система работала в состоянии ложного `parse_bug`.

Подтверждено прод-диагностикой (bd gektar-monitor-31y): логи `status=302, final_url=<esia-url>,
text_head=""` добавленные в d8a3a37 выявили природу ошибки.

---

## Decision

Добавить **3xx-redirect guard** в вызывающих `parse()`: до передачи HTML в парсер проверять
HTTP-статус ответа.

**Правило:** `300 <= response.status < 400 → raise SessionExpiredError("redirect status=N")`

Точки применения:

1. **`PaginatedListFetcher.iterate()`** — guard перед `list_parser.parse(html)`. Поднимает
   `SessionExpiredError`, который `iterate()` пробрасывает по контракту ADR-063 (`except
   SessionExpiredError: raise` стоит раньше `except ParseBugError`/`except Exception`).

2. **`MonitorCycleService._run_cycle_inner()`** — guard перед `list_parser.parse(html)` в
   head-poll пути. `raise SessionExpiredError(...)` внутри существующего `try`-блока,
   перехватывается существующим `except SessionExpiredError` — поведение идентично уже
   реализованному пути.

**`final_url` логируется** через `logger.warning(extra={...})`. **Запрет** помещать URL с
токенами в сообщение доменного исключения (`SessionExpiredError.message` — static строка
`"redirect status=N"`): PII-contract ADR-017.

Существующая `ParseBugError`-диагностика (status / final_url / text_head, добавленная в
d8a3a37) **сохранена** для подлинных DOM-смен (неожиданные 2xx без ожидаемой разметки).

---

## Alternatives Rejected

| Вариант | Причина отклонения |
|---|---|
| Инлайн-обработка `redirect_login` в `MonitorCycleService` (отдельная except-ветка) | Дублирует ~20 строк `except SessionExpiredError` блока; review: нарушение DRY + cohesion. Отклонено на review |
| `UpstreamError(category="redirect_login")` на HTTP-уровне в `RequestsHttpClient` | Не даёт существующего `SessionExpiredError`-флоу: downstream `SseSessionExpired` → email-идемпотентность → relogin-chain. `redirect_login` в `UpstreamError.category` описан в [[architecture/08-error-strategy]] как legacy-mapping, но `MonitorCycleService` его **не ловит** — пробрасывается в `SseCycleError(error_category="redirect_login")` без `SseSessionExpired`. Отклонено |
| Детект пустого `text` в `SelectolaxListParser.parse()` | Парсер не видит HTTP-статус и `final_url`. Patch в парсере нарушает single-responsibility (парсер — HTML-трансформатор, не HTTP-состояние). Отклонено |
| Детект ESIA-маркеров в `final_url` в `RequestsHttpClient` | HTTP-адаптер не должен знать о логике ЕСИА-редиректов (нарушает слоистость ADR-006). Отклонено |

---

## Consequences

- **Корректная категория при 302**: `SessionExpiredError` → `SseSessionExpired` → email +
  relogin prompt. Категория `parse_bug` больше не появляется при редиректах.
- **Галка `backfill.done` не блокируется**: `SessionExpiredError` не ставит галку (ADR-068 §3
  инвариант B1 сохранён), но больше не подменяется `ParseBugError`.
- **`ErrorCategory.redirect_login`** становится недостижимой в проде через `UpstreamError`-путь
  (донор отвечает 302, не `UpstreamError`): follow-up [[decisions/ADR-069-redirect-response-session-expired-guard|ADR-069]] → bd gektar-monitor-6rv.
- **Single-page fallback в `FullScanService._fetch_region_ids_single_page`** не покрыт guard-ом
  (HEAD-запрос, отдельный путь): follow-up bd gektar-monitor-ida.
- **Relogin-шторм исключён**: `SseSessionExpired` идемпотентен через `session_expired_email_sent`
  флаг в `session_expired_email.py` (per-epoch dedup).
- `SelectolaxListParser.parse()` и его контракт (ESIA title → missing tbody → empty tbody) **не
  изменён**: guard работает на уровне вызывающих, не парсера.

---

## References

- [[architecture/08-error-strategy]] — двухконтурная модель, `SessionExpiredError` vs `ParseBugError`
- [[decisions/ADR-063-session-expired-iterate-propagation|ADR-063]] — `SessionExpiredError` из `iterate()`, re-raise контракт
- [[decisions/ADR-068-month-window-backfill-done-flag-gate|ADR-068]] — инвариант B1 (галка только при полном успехе)
- [[decisions/ADR-046-session-monitor-combined-probe-and-publish|ADR-046]] — `SessionMonitor` как fallback-detector
- [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]] — PII в исключениях и логах
- [[glossary#SessionExpiredError]], [[glossary#ParseBugError]], [[glossary#SelectolaxListParser]]
- `src/fis_monitor/services/paginated_list_fetcher.py` — guard в `iterate()`
- `src/fis_monitor/services/monitor_cycle.py` — guard в `_run_cycle_inner()`
- `tests/unit/services/test_monitor_cycle.py`, `test_monitor_cycle_done.py`, `test_paginated_list_fetcher.py`
