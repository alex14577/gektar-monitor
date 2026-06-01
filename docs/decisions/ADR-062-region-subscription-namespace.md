# ADR-062: region_subscriptions namespace — macro-id → subject site-id

**Status.** Accepted (bd gektar-monitor-v1t).

**Context.** Колонка `region_id` несёт ДВА несовместимых namespace:

- `region_subscriptions.region_id` заполняется **макро-кодом** (`dfo=1`, `arctic=2`) — пишется из `settings.regions` через `WatchdogConfigSource`.
- `lots.region_id` после миграции v7→v8 хранит **site-id субъекта РФ** (27–96, ключи `SUBJECT_TITLE_BY_ID`).

Воспроизведено на боевой БД: `region_subscriptions=(region_id=2)`, `DISTINCT lots.region_id={29,30,34,72,…,96}`, JOIN `lots.region_id = rs.region_id` → **0 совпадений**.

Сломанные потребители (карта из brainstorm-фазы, 3 агента):

| # | Место | Эффект |
|---|---|---|
| B1 | `infra/config_source.py` `_apply_region_diff` / `_bootstrap_subscriptions` | пишет macro-id в подписки |
| B3 | `services/lot_query.py` `_subscription_cutoff_fragment` | cutoff-JOIN мёртв → лента не режется по дате подписки (255→255) |
| B4 | `services/notifier_dispatcher.py` `SubscribedAtFilteredNotifier.should_suppress` | `get_subscribed_at(lot.region_id)` всегда `None` → suppression мёртв |
| B5 | `services/monitor_cycle.py` delta-trigger | `count_active(region_id=macro)` всегда 0 → delta-trigger срабатывает некорректно |

НЕ затронут: `full_scan.py` deactivation (`list_active()` без фильтра по region_id, работает по `lot.id`); парсинг/инжест (`subject_id_by_title(row.region)` уже даёт корректный subject-id); `RfSubjectFilterMatcher`; `url_builder` (macro-id в URL — так и надо, сайт ожидает macro).

**Decision.** `region_subscriptions.region_id` переводится в namespace **site-id субъектов** (совпадает с `lots.region_id` post-v8). Решение data-model, не query-time.

1. **Write-path** (`WatchdogConfigSource`): на каждый macro-id из `settings.regions` записывать по строке на каждый subject-id из `subjects_for_macros([macro])`. `subscribed_at` для субъекта = момент подписки (не перезаписывается при повторной развёртке). При снятии macro-региона удалять только те субъекты, что НЕ входят ни в один оставшийся подписанный macro (защита субъектов-пересечений 87=Якутия, 96=Чукотка).
2. **Migration v9→v10** (новая): развернуть существующие macro-строки в subject-строки. Для субъектов-пересечений брать `MIN(subscribed_at)` (ранний cutoff = не скрываем лоты). Идемпотентно (`INSERT OR IGNORE` + удаление исходных macro-строк после вставки). Bump `user_version` 9→10.
3. **count_active** (`LotRepository`): сигнатура `region_id: int` → `region_ids: tuple[int, ...]`, SQL `WHERE region_id IN (...)`. `monitor_cycle` передаёт `subjects_for_macros([region])`.
4. **JOIN в `_subscription_cutoff_fragment` и `should_suppress` НЕ меняются** — становятся корректны после миграции данных.
5. Обновить комментарий `docs/db/schema.sql` к `region_subscriptions`/`lots.region_id` (устарел: говорит «macro-region FK», фактически subject site-id).

**Alternatives considered.**

- **Query-time translation** (разворачивать macro→subjects в SQL cutoff-фрагменте): SQLite не выражает `LEFT JOIN … ON lots.region_id IN (…)` чисто; требует CTE/subquery → `_subscription_cutoff_fragment` (чистый read-query-builder) получает domain-знание о регионах → нарушение high cohesion / SRP. Не решает `count_active` без отдельной правки. **Отклонено.**
- **Отдельная колонка `macro_id`**: два namespace продолжают сосуществовать, схема усложняется, JOIN-логика растёт. **Отклонено.**

**Consequences.**

- JOIN-конструкции остаются реляционно корректными без изменения кода запроса; правится только namespace данных + write-path + `count_active`.
- Одноразовая миграция `user_version` 9→10; боевые `subscribed_at` сохраняются.
- `region_subscriptions` после миграции хранит ≤19 уникальных subject-строк (11 ДФО + 10 Арктика − 2 пересечения) вместо ≤2 macro-строк; индекс/PK по `region_id` покрывает.
- Расширяемость: новый макро-регион добавляется только в `SUBJECTS_BY_MACRO` — write-path и миграция используют его автоматически.

См. также: [[decisions-log]], [[decisions/ADR-031-region-ssot-site-id|ADR-031]], [[data-model]], [[glossary#region_id]].
