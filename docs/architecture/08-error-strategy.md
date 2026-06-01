# 8. Стратегия ошибок и Result-типы

## Двухконтурная модель

**Контур 1: внутренние ошибки (баги, инварианты) — exception.**
- `DomainError` базовый, подклассы: `SchemaAnomalyError`, `InvariantViolationError`.
- Ловятся в `MonitorCycleService.run_forever()` → пишутся в `cycles.error`, цикл переходит в exponential backoff.
- НЕ перехватываются в use case'ах поштучно — `except Exception: pass` запрещён.

**Контур 2: ожидаемые failure modes (network, SMTP, парсинг) — `Result`-тип.**
- Для `Notifier.send()` уже задано в [[notifications]]: `NotifyResult(ok, detail, retryable)`.
- Расширяем на `HttpClient`? **Нет.** `requests`-ошибки — exception (`requests.RequestException` → ловится в адаптере и поднимается как `UpstreamError` с категорией: `Network`, `Http4xx`, `Http5xx`, `RedirectToLogin`).
- Для парсера — разделение ([[architecture/03-protocols]] §3.6.2): **`ParseBugError`** (контракт сломан — баг, поднимается в cycle.error) vs **`ParserVersionMismatch`** (lazy reparse, НЕ ошибка цикла). Universal `ParseError` deprecated в пользу двух подкатегорий.

**Почему так:**
- Result везде → код шумный, каждый use case разворачивает Result-цепочку. Python — не Rust, нет `?`-оператора.
- Exception везде → теряется явная семантика «это нормальный сценарий, retry в другом канале» vs «это баг, не игнорируй».
- Граница — нотификации: один канал упал → остальные идут. Тут Result даёт чёткую структуру для retry-логики (по `retryable` флагу) и записи в `notifications.detail`.

## Категории UpstreamError

```python
class UpstreamError(Exception):
    category: Literal["network", "http_5xx", "http_4xx", "redirect_login", "timeout"]
```

`MonitorCycleService` смотрит на `category`:
- `redirect_login` → поднять `session_expired`, no-op до релогина.
- `http_5xx`, `timeout`, `network` → exponential backoff, не пишем в `last_seen_at` при full_scan.
- `http_4xx` (кроме 401/403) → log, retry следующего цикла.

См. [[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]], [[data-model/errors]].

## SessionExpiredError — auth-failure, отдельная категория

`SessionExpiredError` — подкласс `DomainError` (**не** `UpstreamError`/`ParseBugError`).
Поднимается из `list_parser.parse()` при обнаружении ЕСИА-login-страницы (сессионная кука
истекла), **не** является DOM-багом или сетевой ошибкой.

**Поведение в `PaginatedListFetcher.iterate()`:**
- Перехватывается явно (`except SessionExpiredError`) ПЕРЕД `except ParseBugError`,
  логируется на уровне WARNING (**без** `exc_info`), затем **re-raise**.
- Частично отданные до обрыва строки остаются у caller (тот же контракт, что для
  `ParseBugError`/`UpstreamError`).

**Callers (Backfill / FullScan) при получении `SessionExpiredError`:**
1. Публикуют `SseSessionExpired(timestamp=...)` в `EventBus`.
2. Прекращают обход оставшихся регионов (`break`/`return`).
3. `BackfillService._process_region` дополнительно re-raise-ит, чтобы `_run()` мог
   поймать и прервать цикл.
4. `FullScanService._fetch_region_ids_paginated` re-raise-ит после publish;
   `run_once` ловит в цикле регионов и делает `break`.
5. `FullScanService._fetch_region_ids_single_page` публикует событие и re-raise-ит —
   единый механизм с paginated-путём: `run_once` ловит `SessionExpiredError`, ставит
   `all_regions_completed=False` и делает `break`.

**Реаутентификация — ручная:** `SseSessionExpired` → UI-modal + `SessionExpiredEmailService`.
Авто-refresh из Backfill/FullScan **не реализуется** (ADR-063).

**Идемпотентность:** дублированные `SseSessionExpired` при нескольких регионах безвредны —
`SessionExpiredEmailService` идемпотентен per-epoch.

См. [[decisions/ADR-063-session-expired-iterate-propagation|ADR-063]].
