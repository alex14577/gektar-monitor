# ADR-063: SessionExpiredError propagation through PaginatedListFetcher.iterate()

**Status.** Accepted (bd gektar-monitor-sv3).

**Context.** `PaginatedListFetcher.iterate()` ловит `ParseBugError` явно, а всё прочее — generic `except Exception` → `logger.warning(exc_info=True)` + `return`. `SessionExpiredError` — `DomainError`, **не** подкласс `UpstreamError`/`ParseBugError` — поднимается из `list_parser.parse()` при обнаружении ЕСИА-login-страницы и проваливается в generic `except`.

Воспроизведено (рантайм-репро на реальном `PaginatedListFetcher`): при протухании сессии на странице N обход обрывается МОЛЧА (отдано 50 из 404), исключение не пробрасывается. Последствия:

- `BackfillService._process_region` и `FullScanService._fetch_region_ids_paginated` считают обрыв **нормальным концом каталога** → каталог недозагружен молча.
- `SseSessionExpired` НЕ публикуется (его публикуют только `MonitorCycleService` head-poll и `SessionMonitor`) → пользователь не уведомлён, реаутентификация не инициируется.

`FullScanService` уже различает нормальный конец (`pagination_completed=True`) и обрыв (`=False` → mass-deactivation подавлена) — поведение корректное, сохраняем.

**Decision.** `SessionExpiredError` пробрасывается из `iterate()`, callers реагируют публикацией события:

1. **`iterate()`**: добавить `except SessionExpiredError: raise` ПЕРЕД `except ParseBugError`/`except Exception` в parse-блоке. Частично отданные до обрыва строки остаются у caller (тот же контракт, что для `ParseBugError`/`UpstreamError` — задокументировать в docstring `iterate()` и `SessionExpiredError`).
2. **`BackfillService`**: `_process_region` — `except SessionExpiredError` → `self._event_bus.publish(SseSessionExpired(timestamp=self._clock.now()))` → `raise`; `_run()` — `except SessionExpiredError` → прекратить обход оставшихся регионов (`break`/return). `_event_bus`/clock уже инъецированы.
3. **`FullScanService`**: `_fetch_region_ids_paginated` — `except SessionExpiredError` ПЕРЕД `except Exception` → publish `SseSessionExpired`, `pagination_completed` остаётся `False` (mass-deactivation подавлена); `run_once` прекращает обход оставшихся регионов. Унифицировать `_fetch_region_ids_single_page` (сейчас ловит `SessionExpiredError` тихо без publish) — добавить тот же publish.
4. **Реаутентификация НЕ автоматизируется** — `SseSessionExpired` → существующий механизм (UI-modal + `SessionExpiredEmailService` + ручной `LoginService.start_login` через `/auth`). Авто-refresh вне scope.

**Alternatives considered.**

- **Sentinel/флаг на fetcher** (caller проверяет `last_error`): генератор держит побочное состояние об ошибке → low cohesion, high coupling, неидиоматично. **Отклонено.**
- **Callback из iterate**: лишний канал коммуникации, усиливает coupling. **Отклонено.**
- **Авто re-auth из backfill/full_scan**: изменение архитектуры (сейчас re-auth только по действию пользователя) — вне scope бага. **Отклонено.**

**Consequences.**

- Все callers `iterate()` обязаны обрабатывать `SessionExpiredError` (сейчас только `BackfillService`/`FullScanService`; `MonitorCycleService` `iterate()` не использует).
- `_running` в `BackfillService` не застревает — сброс гарантирован `finally` в `_worker`.
- Дублированный `SseSessionExpired` при нескольких регионах безвреден — `SessionExpiredEmailService` идемпотентен per-epoch.
- Недозагруженный при обрыве каталог доберётся следующим циклом после ручной реаутентификации (upsert идемпотентен).

См. также: [[decisions-log]], [[architecture/08-error-strategy]], [[decisions/ADR-036-headpoll-pagination|ADR-036]], [[glossary#SessionExpiredError]].
