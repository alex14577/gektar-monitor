# ADR-065 — Лента: видимость по членству-в-подписке, date-cutoff только для email

**Status**: Accepted
**Date**: 2026-06-02
**Deciders**: Backend, Product (user)
**Tags**: feed, subscription, region_subscriptions, cutoff, lot_query, visibility
**Fixes**: bug gektar-monitor-u7p (счётчик/лента показывали лоты неподписанных субъектов и скрывали подписанные)
**Amends**: [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]] §«SQL-level feed cutoff» (2026-05-27 amendment)
**See also**: [[decisions/ADR-062-region-subscription-namespace|ADR-062]] (subject-id namespace), [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]] (один pocket-набор на все регионы)

---

## Context

Лента `/feed` применяла на SQL-уровне subscription **date-cutoff** (ADR-039 amendment
2026-05-27): `LotFilters(apply_subscription_cutoff=True)` добавлял
`LEFT JOIN region_subscriptions` + предикат
`(lots.region_id IS NULL OR rs.subscribed_at IS NULL OR date(date_create) >= date(subscribed_at))`.

Этот предикат **fail-open** по членству: лот субъекта, для которого нет строки в
`region_subscriptions` (`rs.subscribed_at IS NULL`), **показывался**.

**Симптом (bug u7p, regions=[2], воспроизведён):** при сидировании
`region_subscriptions` 10 субъектами Арктики с `subscribed_at=сегодня` лента
показывала ровно ДФО-лоты (субъекты без записи подписки → fail-open → показаны) и
**скрывала** подписанные Арктика-лоты (`date_create < subscribed_at` → отсечены
date-cutoff'ом). Счётчик `#feed-lot-count` = число лотов НЕподписанных субъектов.

**Корень — конфляция двух concern'ов в одном предикате:**

1. **Членство:** «субъект лота вообще подписан?»
2. **Date-cutoff:** «лот достаточно свежий (создан после подписки)?»

Fail-open ветка `subscribed_at IS NULL → показать` была корректна для **email-канала**
под допущением «если лот в БД — он из подписанного региона». [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]]
это допущение **сломал для ленты**: донорский `region=` — no-op, БД содержит лоты
**всех** субъектов pocket-набора (ДФО + Арктика) независимо от подписки. Лента читает
всю БД → видит ДФО-лоты без записи подписки → fail-open → показывает чужое.

---

## Decision

**Видимость в ленте определяется ЧЛЕНСТВОМ-в-подписке, а не датой.** Лента
показывает **все** активные лоты подписанных субъектов (есть строка в
`region_subscriptions`) **независимо от `date_create`**; лоты неподписанных
субъектов скрыты. Date-cutoff остаётся **только для email** (анти-спам,
[[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]] через
`passes_subscription_cutoff`).

**Продуктовое обоснование (Intent B, решение пользователя):** лента — браузинг-канал,
пользователь ожидает видеть весь активный каталог по своим подписанным субъектам, а не
только лоты, появившиеся после момента подписки. Date-cutoff осмыслен для email
(не флудить историей), но не для ленты.

### Реализация

Новый флаг `LotFilters.filter_subscribed_subjects: bool = False` (ортогонален
`apply_subscription_cutoff`). В `LotQueryService._build_where` при `True` добавляется
условие (подзапрос, **без** JOIN):

```sql
(region_id IS NULL OR region_id IN (SELECT region_id FROM region_subscriptions))
```

(квалификация `lots.` применяется через существующую переменную `col`, если JOIN
активен). `web/feed_context.py._view_filters_to_lot_filters` ставит
`filter_subscribed_subjects=True` вместо `apply_subscription_cutoff=True`.

### Инварианты

- **V1.** Подписанный субъект → **все** его активные лоты показаны (любая дата).
- **V2.** Неподписанный субъект (нет строки) → скрыт.
- **V3.** `region_id IS NULL` → показан (паритет с ADR-039; неатрибутируемый лот не
  теряется).
- **V4.** `count(filters) == len(search(filters).items)` — счётчик равен ленте (общий
  `_build_where`).
- **V5.** Флаги `apply_subscription_cutoff` и `filter_subscribed_subjects`
  **взаимоисключающи** (guard в `__post_init__`): combination семантически не
  определена, ни один прод-путь не ставит оба.

### Что НЕ изменилось

- `domain/subscription_cutoff.py` `passes_subscription_cutoff` — **email-канал**
  (`SubscribedAtFilteredNotifier`) использует его без изменений.
- `_subscription_cutoff_fragment` + `apply_subscription_cutoff` — **сохранены** как
  протестированный SQL-mirror date-предиката (равенство с Python-предикатом гарантирует
  `test_lot_query_cutoff.py`). После этого ADR в проде их не вызывает ни один путь;
  оставлены намеренно как building-block, а не удалены (минимизация blast radius фикса;
  удаление — отдельная cleanup-задача при явном решении, что SQL date-cutoff ленте не
  нужен никогда).
- `/lots` API (`apply_subscription_cutoff=False`), `BrowserSseNotifier` (по дизайну
  получает всё), схема БД, миграции.

---

## Consequences

- Лента и `#feed-lot-count` показывают лоты подписанных субъектов (V1–V4). Repro:
  feed = 168 Арктика-лотов (все), ДФО скрыты.
- Query-builder **не** получает region-domain знание: подзапрос читает таблицу
  `region_subscriptions`, не хардкодит subject-id (соблюдает
  [[decisions/ADR-062-region-subscription-namespace|ADR-062]]).
- Перф: semi-join против `region_subscriptions` (PK на `region_id`, ≤19 строк) +
  существующий `idx_lots_region_id_active(region_id, is_active)`; новый индекс не нужен.
- ADR-039 §«SQL-level feed cutoff» — амендирован: лента больше не применяет SQL
  date-cutoff; этот механизм остаётся определён, но не подключён к ленте.
- Пагинация «показать еще» / счётчик «Показано N из M» / индикатор загрузки —
  **вне scope** этого ADR (задача gektar-monitor-6jg).

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| (a) Fail-closed по членству + сохранить date-cutoff на ленте (Intent A) | Скрывал бы исторические лоты подписанных субъектов (подписался сегодня → лента пустая до новых поступлений). Пользователь выбрал Intent B (видеть весь активный каталог подписки). |
| (b) Fail-open (текущее) | И есть баг u7p — показывает неподписанные, скрывает подписанные. |
| Переиспользовать `apply_subscription_cutoff`, поменяв его семантику | Сломал бы equivalence-тест и общий с email Python-предикат; конфляция двух concern'ов под одним флагом — нарушение cohesion. Отдельный флаг сохраняет date-предикат нетронутым для email. |
| Bound IN-list подписанных subject-id, передаваемый в запрос | Двигает region-domain знание в call-site → нарушение ADR-062. Подзапрос к таблице самодостаточен. |

---

## References

- `src/fis_monitor/services/lot_query.py` — `LotFilters.filter_subscribed_subjects`, `_build_where`
- `src/fis_monitor/web/feed_context.py` — `_view_filters_to_lot_filters`
- `tests/integration/services/test_lot_query_subscribed_subjects.py` — V1–V5 (Layer 3)
- [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]], [[decisions/ADR-062-region-subscription-namespace|ADR-062]], [[decisions/ADR-064-region-param-noop-global-delta-count|ADR-064]]
- [[glossary#region_subscription]], [[glossary#subscribed_at]]
