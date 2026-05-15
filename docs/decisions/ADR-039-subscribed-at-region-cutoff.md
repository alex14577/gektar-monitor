# ADR-039 — subscribed_at region cutoff: per-region filter для подавления старых лотов

**Status**: Accepted
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

### Точка фильтра

`notifier_dispatcher.dispatch(lot, ...)` — domain-уровень. Один дополнительный check перед
`FilterMatcher.matches`:

```python
subscribed_at = self._region_sub_repo.get_subscribed_at(lot.region_id)
if subscribed_at is not None and lot.date_create < subscribed_at:
    return  # intentional suppression — лот старее подписки
```

Domain-уровень выбран намеренно: прикрывает `MonitorCycleService`, `FullScanService`,
`BackfillService` и любого будущего caller'а — единая точка контроля (H3 из ADR-036).

### Delta-trigger threshold

`BackfillService.maybe_start` порог: `len(parsed_lots) + 3`. TTL отсутствует. Логика: если
результатов в head-poll мало (ниже порога), backfill не нужен. `total_count` из `ParsedListPage`
(ADR-036 update) используется как primary signal; `count_active() == 0` остаётся fallback при
`total_count is None`.

### At-least-once SLO (ADR-019)

SLO определён над notifications, которые система **решает** отправить — не над universe of lots.
`subscribed_at` filter — намеренная suppression до стадии dispatch, не нарушение at-least-once.
Документируется явно в ADR-019 §«Intentional dispatch suppression».

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
- **`notifier_dispatcher`**: получает `RegionSubscriptionRepository` dep; check добавляется в
  `dispatch()`.
- **`WatchdogConfigSource`**: получает `RegionSubscriptionRepository` + `Clock` deps; реализует
  migration-логику (set-if-absent + delete).
- **`composition.py`**: `SqliteRegionSubscriptionRepository` создаётся и инжектируется в оба места.
- **Tests**: unit-тесты на `dispatch()` suppression + `WatchdogConfigSource` diff-логику.

---

## References

- [[decisions/ADR-019-notification-state-machine|ADR-019]] — at-least-once SLO + intentional suppression
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — BackfillService design
- [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]] — on_login_success trigger (secondary fallback)
- [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]] — ParsedListPage.total_count, delta-trigger
- [[glossary#subscribed_at]], [[glossary#delta-trigger]], [[glossary#region_subscription]]
- [[data-model/lot]] — `Lot.date_create` field
