# ADR-066 — SSE-лента: membership-фильтр на per-connection предикате (паритет с page-load)

**Status**: Accepted
**Date**: 2026-06-03
**Deciders**: Backend, SRE, Architecture (brainstorm — Explore + Software-Architect + SRE)
**Tags**: sse, subscription, region_subscriptions, membership, event-filter, per-connection, backfill
**Fixes**: bug gektar-monitor-i7n (SSE push/backfill не применял membership-фильтр → неподписанные лоты протекали в live-ленту)
**Extends**: [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] (per-connection predicate-слой)
**Mirrors to SSE**: [[decisions/ADR-065-feed-visibility-subject-membership|ADR-065]] (membership-видимость на page-load)
**See also**: [[decisions/ADR-062-region-subscription-namespace|ADR-062]] (namespace), [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]] (БД содержит все субъекты pocket-набора), [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]] §BrowserSseNotifier (superseded для membership)

---

## Context

[[decisions/ADR-065-feed-visibility-subject-membership|ADR-065]] (u7p) ввёл membership-видимость для **page-load** ленты: `LotFilters.filter_subscribed_subjects=True` добавляет в SQL подзапрос
`(region_id IS NULL OR region_id IN (SELECT region_id FROM region_subscriptions))`.

SSE-путь этого фильтра **не применял**:

- `BrowserSseNotifier.send()` публикует в шину **все** лоты (по дизайну ADR-039 §BrowserSseNotifier);
- `BackfillService` публикует `SseLotNew(is_backfill=True)` так же — без фильтра;
- per-connection `make_sse_view_filter` ([[decisions/ADR-052-sse-view-filter-propagation|ADR-052]]) проверял **только** sidebar view-фильтр (subjects/area из cookie); при пустом sidebar → always-true sentinel. `region_subscriptions` не запрашивался нигде на SSE-пути.

**Следствия:** (1) живая лента и backfill доставляли в браузер карточки неподписанных субъектов, хотя при reload SQL их скрывает → рассинхрон reload-vs-live (нарушение Intent B, ADR-065); (2) счёт `#feed article.lot` в DOM недостоверен как «показано подписанных» → блокировал корректный счётчик `Показано N из M` (gektar-monitor-6jg).

Допущение ADR-039 «`BrowserSseNotifier` получает все лоты, т.к. в БД только лоты подписанных регионов» сломал [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]]: донорский `region=` — no-op, БД содержит лоты **всех** субъектов pocket-набора независимо от подписки.

---

## Decision

**Membership-фильтр применяется на том же per-connection predicate-слое, что и view-фильтр ([[decisions/ADR-052-sse-view-filter-propagation|ADR-052]]).** Один предикат, скомпонованный в `GET /events`, покрывает **и** live, **и** backfill: оба пути публикуют `SseLotNew` в общую `ThreadEventBus`, и каждое событие проходит через `SseStreamer.stream(event_filter=...)` для каждого соединения. `BrowserSseNotifier`, `BackfillService`, `LotQueryService` — **не изменены**.

### Почему не publish-time фильтр

ADR-052 отверг publish-time фильтрацию для **per-connection** view-фильтров (у каждого клиента свой cookie). Membership — **глобальное** состояние аккаунта (таблица `region_subscriptions`, схема `(region_id PK, subscribed_at)`, без `user_id` — приложение single-account), поэтому возражение «suppress для всех подписчиков» здесь не работает (нам и нужно suppress для всех). Но per-connection слой всё равно выигрывает по cohesion/coupling: publish-time перенёс бы region-domain знание в `infra/sse/browser_sse_notifier.py` и `services/backfill.py` (оба получили бы зависимость от `RegionSubscriptionRepository`), а backfill дёргал бы БД в горячем цикле (сотни лотов). Per-connection слой оставляет notifier/backfill агностиками, а БД читается **один раз** на connect.

### Реализация

1. **Accessor.** Новый метод Protocol `RegionSubscriptionRepository.list_subscribed_region_ids() -> frozenset[int]` (+ `SqliteRegionSubscriptionRepository`: `SELECT region_id FROM region_subscriptions`). Читает region_id **из таблицы** → не нарушает [[decisions/ADR-062-region-subscription-namespace|ADR-062]] (никаких hardcoded subject-id). Page-load inline SQL-подзапрос (`lot_query.py`) **не разделяется** с этим accessor'ом намеренно: разные уровни абстракции (SQL-фрагмент в query-builder vs Python-значение для predicate-замыкания), разный тайминг (per-query vs per-connection snapshot). Консолидация потребовала бы SQL↔Python мост — сложнее, чем две независимые реализации.

2. **Predicate.** Новая чистая фабрика `services/sse_view_filter.make_sse_membership_filter(subscribed_region_ids: frozenset[int]) -> Callable[[SseEvent], bool]`. `make_sse_view_filter` **не изменён** (отдельная фабрика — single responsibility: view-фильтр = пользовательские предпочтения отображения; membership = access-инвариант).

3. **Композиция.** `web/routes/events.py._build_event_filter` читает snapshot `repo.list_subscribed_region_ids()` **один раз** на connect, строит membership-предикат и комбинирует с view-предикатом: `lambda e: membership(e) and view(e)`. Membership применяется **всегда** (даже при отсутствующем/битом `view_filters` cookie → возвращается membership-only предикат; `None`-pass-through больше не возникает). Repo инжектится через `get_region_subscription_repo` (DI-паттерн как `get_sse_streamer`).

### Инварианты (зеркалят ADR-065 V1–V3 на SSE-предикат)

- **V1-SSE.** `lot.region_id ∈ subscribed` → событие проходит.
- **V2-SSE.** `region_id ≠ None` и `∉ subscribed` → подавлено.
- **V3-SSE.** `region_id IS None` → проходит (паритет ADR-065 V3; неатрибутируемый лот не теряется).
- **V4-SSE.** Не-`SseLotNew` события (`cycle.done`, `status`, ping) → проходят всегда (membership применяется только к `lot.new`).
- **V5-SSE.** **Пустой** `subscribed` (нет подписок) → подавляет **все** region-bearing лоты, пропускает `region_id None`. Это паритет с page-load: `filter_subscribed_subjects=True` + пустая таблица → подзапрос `IN ()` → проходит только NULL. Fresh-onboarding пользователь видит тот же пустой результат, а не нефильтрованный поток. **Нет** fast-path, пропускающего всё на пустом множестве.
- **Backfill.** `is_backfill=True` следует тому же правилу (не инспектируется предикатом; покрыт автоматически, т.к. проходит через тот же per-connection фильтр).

### Snapshot / staleness

Snapshot membership берётся на connect (как view-фильтр в ADR-052). Изменение подписок mid-connection → предикат устаревает до reconnect. Допустимо: онбординг (step 1) возвращает `HX-Redirect` (полная навигация → новый `EventSource`); мутации фильтров → `HX-Trigger: filter-changed` → reconnect ([[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] amendment m72b). Live-sync подписок без reconnect — вне scope.

`frozenset` иммутабелен → thread-safe для чтения в async-стриме без локов; предикат выполняется в event loop после возврата `_drain_one`, не трогает `sse_executor` → не ухудшает shutdown-лаг (gektar-monitor-1iz).

---

## Consequences

- SSE-лента (live + backfill) и page-load теперь паритетны по membership-видимости; reload и live дают одинаковый набор.
- DOM-счёт `#feed article.lot` достоверен как «показано подписанных» → разблокирует счётчик `Показано N из M` (gektar-monitor-6jg).
- `BrowserSseNotifier`/`BackfillService` остаются агностиками membership (ADR-052 OCP сохранён); `SseStreamer` по-прежнему получает только `Callable`.
- Перф: один PK-скан таблицы ≤19 строк на connect + O(1) проверка `in frozenset` на событие.
- View-фильтр (`subjects`) композится поверх membership через short-circuit `and`: пользовательское сужение поверх membership-гейта (semantically intended; ADR-052 §Filter semantics).

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| Publish-time фильтр в `BrowserSseNotifier.send` + backfill loop | Переносит region-domain знание в infra/service-слой; backfill дёргает БД per-lot в горячем цикле; противоречит ADR-052 (SseStreamer агностик); требует инжекта repo в два несвязанных компонента. |
| Только backfill publish-фильтр | Чинит backfill-leak, но не live-push; рассинхрон остаётся. |
| Hybrid (publish-time грубо + per-connect точно) | Дублирование membership-оценки, split-brain при расхождении, без перф/корректностного выигрыша. |
| Расширить `make_sse_view_filter` параметром `subscribed_ids` | Смешал бы два concern'а (view-предпочтения + access-инвариант) в одной фабрике → нарушение SRP/cohesion. Отдельная фабрика + комбинатор чище. |
| Передавать bound IN-list subject-id в predicate | Двигает namespace-знание в call-site → нарушение ADR-062. Accessor читает таблицу самодостаточно. |
| Переиспользовать page-load SQL-подзапрос для SSE | Разные уровни абстракции (SQL-фрагмент vs Python-значение); мост SQL↔Python сложнее двух независимых реализаций. |

---

## References

- `src/fis_monitor/domain/interfaces.py` — `RegionSubscriptionRepository.list_subscribed_region_ids`
- `src/fis_monitor/infra/sqlite/repositories/region_subscriptions.py` — SQLite impl
- `src/fis_monitor/services/sse_view_filter.py` — `make_sse_membership_filter`
- `src/fis_monitor/web/routes/events.py` — `_build_event_filter` композиция, snapshot на connect
- `src/fis_monitor/web/deps.py` — `get_region_subscription_repo`
- `tests/unit/services/test_sse_view_filter.py::TestMembershipFilter` — Layer 1 (V1–V5, backfill)
- `tests/unit/web/routes/test_events_filter.py` — Layer 4 (wiring, fake repo)
- [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]], [[decisions/ADR-065-feed-visibility-subject-membership|ADR-065]], [[decisions/ADR-062-region-subscription-namespace|ADR-062]]
- [[glossary#region_subscription]]
