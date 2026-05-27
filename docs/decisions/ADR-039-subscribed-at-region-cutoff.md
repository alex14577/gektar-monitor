# ADR-039 — subscribed_at region cutoff: per-region filter для подавления старых лотов

**Status**: Accepted (amended 2026-05-18, gn89: day-precision compare + pre-reserve hook; amended 2026-05-27: shared predicate SSOT + SQL-level feed cutoff)
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: notifications, suppression, region_subscriptions, filter, backfill, delta-trigger

---

## Context

При добавлении нового региона (`POST /settings/regions`) система делает backfill и монитор-цикл
начинает накапливать лоты. Без фильтра по времени подписки `notifier_dispatcher.dispatch()` отправит
уведомления по всем историческим лотам региона — спам при онбординге нового региона (issue ugqt).

`Lot.date_create` присутствует в доменной модели. Поле `publish_date` не существует — сравнение
ведётся только по `date_create`.

Пресидирование terminal-статусов в `NotificationsRepository` перед backfill рассматривалось (см.
Alternatives). Хранение `subscribed_at` в `config.json` через `Settings` отклонено: Pydantic-модель
`Settings` frozen, инициализируется без `Clock` — записывать wallclock-момент в ней нельзя.

---

## Decision

### Хранение

Новая таблица в **state DB** (та же SQLite-БД, что `notifications`, `region_subscriptions`):

```sql
CREATE TABLE IF NOT EXISTS region_subscriptions (
    region_id    INTEGER PRIMARY KEY,
    subscribed_at TIMESTAMP NOT NULL
);
```

Протокол `RegionSubscriptionRepository` (domain layer):

```python
class RegionSubscriptionRepository(Protocol):
    def get_subscribed_at(self, region_id: int) -> datetime | None: ...
    def set_if_absent(self, region_id: int, subscribed_at: datetime) -> None: ...
    def delete(self, region_id: int) -> None: ...
```

### Migration-логика

`WatchdogConfigSource._do_reload` — единственная точка, где известен diff «старые регионы → новые
регионы». Логика при перезагрузке config.json:

- Net-new регионы (в new, не в old): `region_subscription_repo.set_if_absent(region_id, clock.now())`.
  `set_if_absent` — idempotent; повторный вызов не перезаписывает.
- Удалённые регионы (в old, не в new): `region_subscription_repo.delete(region_id)`.
  При re-add — новый `subscribed_at = clock.now()`. Юзер пропустит лоты в окне отсутствия —
  приемлемо (семантика «re-subscription с нуля»).

`WatchdogConfigSource` получает `RegionSubscriptionRepository` и `Clock` через конструктор.

### Shared predicate SSOT (amendment 2026-05-27)

Логика day-precision compare вынесена в **единственный источник правды**:
`domain/subscription_cutoff.py` — `passes_subscription_cutoff(date_create, subscribed_at, *, region_id)`.

Функция импортируется везде, где нужна suppression-логика:
- `SubscribedAtFilteredNotifier.should_suppress` — email-канал, pre-reserve hook.
- `LotQueryService._build_query` — SQL-уровень (`apply_subscription_cutoff=True`).

SQL-выражение и Python-предикат разойтись не могут: Layer-3 equivalence-тест
(`tests/integration/services/test_lot_query_cutoff.py::test_sql_predicate_equivalence`)
сравнивает множества lot_id, возвращённые SQL-запросом и предикатом на одном датасете.

### SQL-level feed cutoff (amendment 2026-05-27)

Веб-страница «новые лоты» (feed, `/feed`) применяет cutoff на уровне SQL через
`LotFilters(apply_subscription_cutoff=True)`, которое устанавливается в
`_view_filters_to_lot_filters` (`web/feed_context.py`).

Механизм: `LotQueryService._build_query` при `apply_subscription_cutoff=True` добавляет:
```sql
LEFT JOIN region_subscriptions rs ON lots.region_id = rs.region_id
WHERE (lots.region_id IS NULL OR rs.subscribed_at IS NULL
       OR date(lots.date_create) >= date(rs.subscribed_at))
```

Ключевые инварианты:
- SELECT-список квалифицируется `lots.` при активном JOIN (column-name ambiguity:
  `region_id` существует и в `lots`, и в `region_subscriptions`).
- При `apply_subscription_cutoff=False` (путь `/lots` API) SQL-запрос структурно идентичен
  pre-cutoff версии — никаких изменений для внешних клиентов.
- Load-more пагинация (`/feed/more`) корректна: cursor-based keyset строится на qualified
  `lots.date_create`/`lots.id`; строки не дублируются при переходе страниц.

**BrowserSseNotifier (live SSE push)** — остаётся без фильтрации по дизайну.
Real-time уведомления в браузере должны отражать все новые события, не только «новые» из
subscription-window. Ограничение будет пересмотрено в отдельной задаче при необходимости.

### Точка фильтра (email-канал)

`SubscribedAtFilteredNotifier` — decorator-класс в `notifier_dispatcher.py`, оборачивает `SmtpEmailNotifier`.
Применяет subscribed_at check **только для email-канала** через `passes_subscription_cutoff`.

**Сравнение — day-precision** (amendment 2026-05-18, gn89):

```python
if lot.region_id is not None:
    subscribed_at = self._region_sub_repo.get_subscribed_at(lot.region_id)
    if subscribed_at is not None and lot.date_create.date() < subscribed_at.date():
        # suppress
```

Обоснование calendar-date compare: `lot.date_create` парсится из upstream-формата `DD.MM.YYYY` и
всегда имеет day-precision (`datetime(Y, M, D, 0, 0, 0, tzinfo=UTC)`, см.
`infra/parsers/list_parser.py:_parse_date`). `subscribed_at` — точный wallclock-момент
`Clock.now()`. При timestamp-precision-сравнении любой same-day лот (`00:00:00 < HH:MM:SS`)
суппрессируется ложно — это и был bug gn89: пользователь, подписавшийся днём, не получал
email о лотах того же дня. Сравнение по `.date()` восстанавливает intent («не флудить
ИСТОРИЧЕСКИМИ лотами») без false-positive на same-day.

**Pre-reserve suppression hook** (amendment 2026-05-18, gn89):

`SubscribedAtFilteredNotifier` выставляет публичный метод
`should_suppress(lot: LotPublicDTO) -> bool`. `NotifierDispatcher._send_one` вызывает его
**ДО** `reserve()`/`mark_attempt`/`send()`. Если возвращает `True`:

- **Fresh-dispatch path** (`status_of` = `None`): `_send_one` возвращается без `reserve()`.
  В `notifications` строка не создаётся. Аудит-таблица не содержит misleading `status='sent'`
  для лотов, для которых SMTP никогда не вызывался.
- **Recovery path** (`status_of` = `'pending'`): строка уже существовала с предыдущей
  попытки и теперь подпадает под suppression (например, регион был удалён и заведён
  заново — `subscribed_at` сместился вперёд). `_send_one` промотает строку в
  `permanent_fail` — иначе recovery-sweep бесконечно вытягивал бы её через
  `list_pending_older_than` без шанса на терминальный статус.

Метод `should_suppress` — duck-typed hook (не часть `Notifier`-Protocol), это сохраняет
минимальный Protocol-surface и оставляет дверь для будущих filter-декораторов с тем же
контрактом без модификации унаследованных нотификаторов (OCP).

`NotifierDispatcher.dispatch()` — фильтр убран. Dispatcher передаёт все лоты без предварительной проверки.
`BrowserSseNotifier` — регистрируется в registry без wrapper'а; browser-канал получает все лоты.

Composition root:
```python
registry.register(SubscribedAtFilteredNotifier(inner=email_notifier, region_sub_repo=region_sub_repo))
registry.register(BrowserSseNotifier(event_bus=event_bus))  # no filter
```

Разделение concerns: email — suppress old lots (spam protection); browser — всегда показывать (live UI feed).

### Delta-trigger threshold

`BackfillService.maybe_start` порог: `len(parsed_lots) + 3`. TTL отсутствует. Логика: если
результатов в head-poll мало (ниже порога), backfill не нужен. `total_count` из `ParsedListPage`
(ADR-036 update) используется как primary signal; `count_active() == 0` остаётся fallback при
`total_count is None`.

### At-least-once SLO (ADR-019)

SLO определён над notifications, которые система **решает** отправить — не над universe of lots.
`subscribed_at` filter — намеренная suppression до стадии dispatch, не нарушение at-least-once.
Документируется явно в ADR-019 §«Intentional dispatch suppression».

SLO применяется только к лотам, прошедшим suppression-check. Same-day лоты
(`date_create.date() == subscribed_at.date()`) **не** suppressed — входят в SLO.

Recovery-promotion suppressed `pending` → `permanent_fail` (см. §«Точка фильтра») — это
терминальный статус по ADR-019; SLO такие записи не покрывает (suppression — намеренная,
не доставка failed).

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| Pre-seed terminal-status в `NotificationsRepository` перед backfill | False audit-записи для лотов, которые никогда не проходили dispatch; race window между debounce `WatchdogConfigSource` и `monitor_cycle`; partial-failure при rollback backfill оставляет «фантомные» записи |
| `subscribed_at` в `config.json` / `Settings` | `Settings` frozen (Pydantic), нет `Clock` в domain-layer; изменение config.json при каждом `set` нарушает SRP `WatchdogConfigSource` |
| Изменить семантику `was_new` в `lots.upsert` | Ломает других callers (FullScan, ручной re-import); сложность растёт непропорционально пользе |
| Filter в route-handler `POST /settings/regions` | Не покрывает FullScan и BackfillService — лоты по-прежнему попадут через dispatch |

---

## Consequences

- **Schema**: новая таблица `region_subscriptions(region_id PK, subscribed_at)` в state DB.
- **Protocol**: `RegionSubscriptionRepository` — новый Protocol в domain layer.
- **`SubscribedAtFilteredNotifier`**: новый decorator-класс в `notifier_dispatcher.py`. Получает
  `RegionSubscriptionRepository` dep; применяет subscribed_at check только для email-канала в `send()`.
  `NotifierDispatcher.dispatch()` фильтр убран — dispatcher передаёт все лоты без фильтрации.
- **`WatchdogConfigSource`**: получает `RegionSubscriptionRepository` + `Clock` deps; реализует
  migration-логику (set-if-absent + delete).
- **`composition.py`**: `SqliteRegionSubscriptionRepository` создаётся и инжектируется в оба места.
- **Tests**: unit-тесты на `dispatch()` suppression + `WatchdogConfigSource` diff-логику.
- **`domain/subscription_cutoff.py`** (2026-05-27): новый модуль-SSOT с `passes_subscription_cutoff`.
  Импортируется `SubscribedAtFilteredNotifier` и `LotQueryService._build_query`.
- **`LotFilters.apply_subscription_cutoff`** (2026-05-27): bool-флаг (default=False) в `LotFilters`.
  Web-feed устанавливает `True`; `/lots` API-путь оставляет `False`.
- **`tests/unit/domain/test_subscription_cutoff.py`**: Layer-1 тест предиката (5 случаев).
- **`tests/integration/services/test_lot_query_cutoff.py`**: Layer-3 equivalence-тест SQL vs Python
  на реальном SQLite (all 5 scenarios + gn89 regression guard).

---

## References

- [[decisions/ADR-019-notification-state-machine|ADR-019]] — at-least-once SLO + intentional suppression
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — BackfillService design
- [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]] — on_login_success trigger (secondary fallback)
- [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]] — ParsedListPage.total_count, delta-trigger
- [[glossary#subscribed_at]], [[glossary#delta-trigger]], [[glossary#region_subscription]]
- [[data-model/lot]] — `Lot.date_create` field
