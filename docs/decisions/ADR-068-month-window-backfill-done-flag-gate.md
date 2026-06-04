# ADR-068 — Месячное окно backfill, персистентная галка `backfill.done`, гейт монитора

**Status**: Accepted
**Date**: 2026-06-04
**Deciders**: Backend
**Tags**: backfill, monitor-cycle, state, delta-trigger, fetcher, pagination, sse-status
**Fixes**: bd gektar-monitor-fsm (месячное окно + ранний выход), bd gektar-monitor-k31 (галка + гейт), bd gektar-monitor-1iw (HTTP-таймаут fetcher)
**Amends**: [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] §Walk (неограниченный обход заменён месячным окном + потолком; единый глобальный проход; resume всегда со стр. 1); [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]] Decision §delta-trigger (база сравнения — теперь `total_last`, не `count_active()`)

---

## Context

До этого ADR `BackfillService` обходил **весь каталог** страница за страницей без временного ограничения. Это влекло три проблемы:

1. **Объём и время**: полный каталог (сотни лотов, 5–20+ страниц) грузился при каждом триггере — даже когда нужны только «свежие» лоты.
2. **Бесконечный цикл при delta-trigger**: `maybe_start` сравнивал донорский `total_count` с глобальным `count_active()` (ADR-064). После завершения backfill с месячным окном `count_active() < total_count` (в БД только часть каталога) → delta всегда положительная → backfill триггерился каждый цикл.
3. **Нет гарантии «хотя бы один полный прогон»**: `MonitorCycleService` мог опрашивать донора до того, как БД заполнена достаточно для корректного delta-сравнения.

---

## Decision

### 1. Месячное окно backfill (fsm)

Обход идёт **newest-first** (`sort=-DATE_CREATE`). Остановка — **раннее на первом лоте, чей `date_create < cutoff`**, где `cutoff = clock.now() − 30 дней` (`_BACKFILL_WINDOW_DAYS = 30`). Ранний выход = **успешное завершение** (галка ставится, см. п.3).

Страховочный потолок `max_pages = _BACKFILL_MAX_PAGES = 5` (100 лотов при `per_page=50`) обеспечивает завершение при сбое парсинга даты. Достижение потолка тоже считается **успехом** — галка ставится.

Naive `date_create` коэрсится в UTC перед сравнением (защита от `.replace(tzinfo=UTC)` parity).

### 2. Единый глобальный проход (fsm + ADR-064)

Поскольку донорский `region=` — no-op (ADR-064), backfill делает **один** проход (`settings.regions[0]`) — не повторяет запросы на каждый регион. Паттерн полностью аналогичен monitor_cycle и full_scan (ADR-064 §Дополнение).

`start_resume()` — alias для `start()` (всегда со страницы 1, idempotent upsert).

### 3. Персистентная галка `backfill.done` (k31)

Новый ключ `STATE_KEY_DONE = "backfill.done"` в `state`-таблице SQLite через `StateRepository` Protocol.

**Условия установки галки (success path):**
- Ранний выход: найден лот старше cutoff — backfill завершён корректно.
- Потолок страниц: обход остановлен на `max_pages=5` без ошибок.
- Нормальное завершение итератора (каталог исчерпан до cutoff — возможно при малом каталоге).

**Галка НЕ ставится при:**
- `cancel()` / `threading.Event` stop.
- `SessionExpiredError` (протухшая сессия ЕСИА).
- Сетевых ошибках / HTTP-таймауте.
- `ParseBugError` (сбой парсинга DOM).

После неуспешного прогона `_running` сбрасывается в `False`, пользователь видит `SseCycleError`.

### 4. Гейт `MonitorCycleService` (k31)

`MonitorCycleService` проверяет `state_repo.get("backfill.done")` в начале каждого цикла планировщика:

- Галка **не установлена** → **ноль head-poll циклов**; публикуется `SseStatus(state="awaiting_backfill")` (один раз, флаг `_awaiting_published`).
- Галка **установлена** → нормальный head-poll.

`StateRepository` инжектируется как `state_repo: StateRepository | None`. При `None` (legacy/тестовый путь без репо) гейт считается пройденным (`backfill_done = True`).

**Старт без галки**: lifespan вызывает `backfill_service.start_resume(stop_event)` автоматически. После успешного прогона `BackfillService` вызывает `monitor_cycle.request_run_now()` (sentinel-очередь `maxsize=1`) — будит монитор немедленно.

**Первичный рендер**: все роуты, строящие `build_monitor_vm`, передают `awaiting_backfill=not backfill.is_done()`. Шаблон `_header_status.html.jinja` учитывает значение `awaiting_backfill` при отображении состояния.

### 5. Новая база delta-trigger: `backfill.total_last` (fsm + ADR-064-supersede)

Новый ключ `STATE_KEY_TOTAL = "backfill.total_last"` — донорский `total_count` с **первой страницы** последнего успешного прогона.

`maybe_start` вычисляет delta как:
```
delta = site_total − total_last_backfill
```
где `total_last_backfill = state_repo.get("backfill.total_last")`. При отсутствии ключа (cold-start до первого прогона) fallback = `db_count` (сохраняет инвариант D3 ADR-064).

**Отрицательная delta** (лоты удалены апстримом): backfill **не** триггерится, но `total_last` обновляется до нового `site_total` (last-writer-wins TOCTOU допустим для приблизительной базы).

Это закрывает структурную расходимость «`count_active() < total_count` навсегда» — теперь при следующем цикле после успешного backfill `total_last ≈ site_total → delta ≈ 0 → no trigger`.

### 6. HTTP-таймаут fetcher + флаги ошибок (1iw)

`PaginatedListFetcherProto.iterate` получает новые параметры:
- `raise_on_network_error: bool = False` — при `True` сетевые ошибки пробрасываются caller'у.
- `raise_on_parse_error: bool = False` — при `True` `ParseBugError` пробрасывается.

`BackfillService._run` передаёт оба флага как `True`; `FullScanService` оставляет дефолт `False` (не затронут).

Также добавлен параметр `page_start: int = 1` и `total_callback: Callable[[int], None] | None` — для сохранения `total_last` со страницы 1 до первой итерации лотов.

---

## Invariants

**B1. Галка только при полном успехе.** `backfill.done` устанавливается исключительно на success-path (_ранний выход по cutoff_, _потолок страниц_, _нормальное завершение_). Cancel/SessionExpired/network/ParseBug — галку не ставят.

**B2. Ноль head-poll циклов до галки.** `MonitorCycleService` не выполняет ни одного head-poll пока `state_repo.get("backfill.done") is None`. Статус `awaiting_backfill` публикуется как `SseStatus.state` вместо `active/warning/error`.

**B3. `total_last` фиксируется со страницы 1.** `total_callback` вызывается после получения первой страницы — до любой ошибки на последующих страницах. Это гарантирует актуальность базы delta-trigger после успешного прогона.

**B4. Отрицательная delta не триггерит, но обновляет базу.** При `site_total < total_last` backfill не запускается, однако `STATE_KEY_TOTAL` обновляется до нового `site_total` — предотвращает накопление устаревшей базы при массовом снятии лотов.

**B5. resume = всегда со страницы 1.** `start_resume()` эквивалентно `start()`. Возобновление прерванного прогона начинается со стр. 1 (idempotent upsert), а не с вычислимой позиции `count//20+1`.

---

## Alternatives Considered

| Альтернатива | Причина отклонения |
|---|---|
| Возрастной фильтр «год» (1 год вместо 30 дней) | Отвергнут пользователем: избыточный объём (лоты >1 мес практически не выигрываются), время прогона неприемлемо |
| Фикс. кап «5 страниц без проверки даты» | Промежуточный вариант; заменён месячным окном с early-stop — корректнее семантически, кап остался лишь как страховка |
| Resume по формуле `page_start = count//20 + 1` | Отвергнут: `count_active()` не коррелирует с «сколько страниц мы уже видели» (лоты могут дублироваться, деактивироваться); стр. 1 + idempotent upsert надёжнее |
| Fail-open гейт (пропустить один цикл если `state_repo is None`) | Отвергнут: в prod `state_repo` всегда передаётся; fail-open скрыл бы ошибки конфигурации. Решено конечным HTTP-таймаутом вместо гейтирования по таймауту ожидания |
| `threading.Event` как источник истины для «backfill done» | Отвергнут: теряется при рестарте процесса → каждый рестарт = новый полный прогон. Персистентная галка в SQLite — SSOT между запусками |
| `count_active()` как база delta-trigger (ADR-064 оригинал) | Структурно несходима при месячном окне: `count_active() < total_count` навсегда → бесконечный backfill. Заменена на `total_last` |

---

## Consequences

### Positive
- Backfill завершается предсказуемо быстро (≤5 страниц = ≤100 лотов за прогон при нормальном каталоге).
- Монитор не опрашивает донора до готовности БД — нет ложных дельт на холодном старте.
- Delta-trigger сходится: после успешного прогона `total_last ≈ site_total → no spurious re-trigger`.
- Явный `SseStatus.state = "awaiting_backfill"` — пользователь видит корректный статус вместо «активен» при пустой БД.

### Negative
- Лоты старше 30 дней НЕ попадают в БД через backfill. Пользователь не увидит исторические лоты в ленте. Это осознанный продуктовый выбор.
- При сбое на первых страницах (ParseBugError) галка не ставится → монитор остаётся в `awaiting_backfill` до ручного перезапуска backfill или починки парсера.
- `state_repo` стал обязательной зависимостью `BackfillService` (был инжектируемым опционально через `MonitorCycleService`).

---

## References

- `src/fis_monitor/services/backfill.py` — `_BACKFILL_MAX_PAGES`, `_BACKFILL_WINDOW_DAYS`, `STATE_KEY_DONE`, `STATE_KEY_TOTAL`, `BackfillService._run`, `maybe_start`
- `src/fis_monitor/services/monitor_cycle.py` — k31 gate (`backfill_done` check, `awaiting_backfill` publish)
- `src/fis_monitor/services/paginated_list_fetcher.py` — `raise_on_network_error`, `raise_on_parse_error`, `page_start`, `total_callback`
- `src/fis_monitor/domain/interfaces.py` — `PaginatedListFetcherProto` расширенная сигнатура, `StateRepository`
- `src/fis_monitor/domain/models.py` — `SseStatus.state` `"awaiting_backfill"` literal
- `src/fis_monitor/web/monitor_vm.py` — `awaiting_backfill` в `build_monitor_vm`
- `src/fis_monitor/web/templates/partials/_header_status.html.jinja` — рендер `awaiting_backfill`
- `src/fis_monitor/app.py` — auto-resume в lifespan
- `src/fis_monitor/composition.py` — wiring `state_repo` в `BackfillService` + `MonitorCycleService`
- `tests/unit/services/test_backfill.py` — месячное окно, galka, maybe_start с total_last
- `tests/unit/services/test_backfill_is_backfill_flag.py` — galka set/no-set пути
- `tests/unit/services/test_monitor_cycle_backfill_gate.py` — awaiting_backfill gate
- `tests/integration/services/test_backfill_state_repo.py` — StateRepository integration
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — BackfillService оригинальный дизайн
- [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]] — delta-trigger глобальный count (частично superseded в части базы сравнения)
- [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]] — политика пагинации по сервисам
