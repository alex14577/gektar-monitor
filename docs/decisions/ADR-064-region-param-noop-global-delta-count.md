# ADR-064 — Донорский `region=` — no-op; delta-trigger считает глобально

**Status**: Accepted
**Date**: 2026-06-01
**Deciders**: Backend
**Tags**: delta-trigger, backfill, monitor-cycle, count_active, use_filter_pocket, region, donor-semantics
**Fixes**: bug gektar-monitor-rzj (бесконечный backfill — delta не сходится)
**Amends**: [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I1 (fetch-scope), [[decisions/ADR-062-region-subscription-namespace|ADR-062]] (delta-trigger часть)
**See also**: [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]] (delta-trigger contract)

---

## Context

`MonitorCycleService` делает head-poll, читает `parsed_page.total_count` («Найдено
записей: N» из DOM) и сравнивает с числом активных лотов в БД (`db_count`).
Если `delta = total_count − db_count` превышает порог — триггерит backfill
(ADR-036, ADR-028).

До этого ADR `db_count` считался **по подмножеству субъектов**:

```python
db_count = self._lot_repo.count_active(region_ids=subjects_for_macros([region]))
```

где `region` — наш macro-id (1=ДФО, 2=Арктика), а `subjects_for_macros([region])`
— site-id'ы из `SUBJECTS_BY_MACRO[region]` (ADR-062 завёл этот per-subject
IN-список для delta-trigger).

**Симптом (bug rzj):** индикатор «загрузка каталога» висел постоянно, backfill
перезапускался каждый цикл, донор долбился по rate-limit без конца.

**Воспроизведение (fake-torgi, до 560 лотов):** `total_upstream` рос со всем
каталогом (40→560), а `count_active(Арктика)` — только на арктическую долю
(12→168); `delta` (28→392) никогда не падал ≤ порога(~23) → `decision=trigger`
каждый цикл.

**Эмпирическая проверка живого донора (надальнийвосток.рф, 2026-06-01, реальная
ЕСИА-сессия оператора):** три запроса вернули **одинаковый** `total`:

| URL | «Найдено записей» |
|---|---|
| `/cabinet/free-lot?region=1&use_filter_pocket=1&sort=-DATE_CREATE&per-page=50` | 346 из 346 |
| `/cabinet/free-lot?region=2&use_filter_pocket=1&sort=-DATE_CREATE&per-page=50` | 346 из 346 |
| `/cabinet/free-lot` (без `region`, без `use_filter_pocket`) | 346 из 346 |

**Вывод:** донорский query-параметр `region=` **фактически ничего не фильтрует**
— сервер отдаёт один и тот же набор (операторский «карман» `use_filter_pocket`,
который монитор всегда шлёт; `_LIST_QUERY` в `url_builder.py`) независимо от
`region=`. Подтверждено `docs/ops/server-performance-v3.md`: `filter_pocket`
сужает выдачу и не управляется параметром `region`.

Следствие: `total_count` (полный pocket-набор) и `db_count`
(подмножество `SUBJECTS_BY_MACRO[macro]`) меряют **разные популяции** — delta
структурно несходим. Это и есть корень rzj.

Предпосылка ADR-035 §I1 — «`region=` тянет весь макрорегион, fetch-scope =
macro» — **эмпирически неверна**: `region=` — no-op, fetch-scope = операторский
pocket, единый для всех регионов.

---

## Decision

Delta-trigger сравнивает `total_count` с **глобальным** числом активных лотов:

```python
db_count = self._lot_repo.count_active()   # region_ids=() — без субъектного фильтра
```

Поскольку донор возвращает один pocket-набор независимо от `region=`, корректная
локальная мера «сколько из этого набора у нас есть» — это **все** активные лоты в
БД, а не подмножество по macro. После того как backfill догрузил каталог,
`count_active() == total_count` → `delta = 0` → backfill завершается, индикатор
гаснет.

Сигнатура `LotRepository.count_active(region_ids: tuple[int, ...] = ())` **не
меняется** — пустой кортеж уже означает глобальный счёт. Меняется только вызов в
`monitor_cycle.py`. Импорт `subjects_for_macros` из `monitor_cycle.py` удалён
(больше не используется) — снижает связность сервиса с `domain.regions`.

`url_builder` (параметр `region=`), `BackfillService`, схема БД — **не трогаются**.

---

## Invariants

**D1. Одна популяция.** `total_count` (донорский pocket) и `db_count`
(`count_active()`) меряют один и тот же набор — весь активный каталог. Delta
сходится к нулю после полной загрузки.

**D2. Симметрия по cutoff.** Ни `total_count`, ни `count_active()` не применяют
`subscribed_at`-cutoff (ADR-039). Сравнение валидно: обе стороны без cutoff.

**D3. Cold-start.** При пустой БД `count_active()=0`, `delta=total_count` →
backfill триггерится (как и требуется).

**D4. Деактивация.** После `FullScanService` mass-deactivation `count_active()` =
текущий активный набор; `delta` может стать `<0` → `skip_negative` (без ложного
триггера). Это корректно.

---

## Assumptions / Known Limitations

**A1. Допущение «один pocket-набор на всех регионах».** D1 опирается на
эмпирический факт: донор возвращает один и тот же набор при любом `region=`.
Боевой конфиг — `regions=[2]` (один макрорегион), где допущение тривиально
выполнено. При `regions=[1,2]` сходимость сохраняется **пока** донор игнорирует
`region=`: backfill по region=1 и region=2 пишет одни и те же лот-ID (idempotent
upsert) → `count_active()` = размер pocket, не сумма регионов.

**A2. Будущая хрупкость.** Если донор когда-нибудь сделает `region=` реальным
фильтром И будет сконфигурирован мультирегион, глобальный `count_active()` начнёт
суммировать непересекающиеся наборы регионов → `delta<0` → `skip_negative`
заблокирует backfill. Тогда delta-trigger придётся вернуть к per-region мере (или
снимать per-region snapshot). Сейчас — вне scope (донор `region=` no-op, прод
монорегионален). Зафиксировано как technical debt.

---

## Consequences

- `MonitorCycleService` больше не зависит от `subjects_for_macros` /
  `SUBJECTS_BY_MACRO` (cohesion ↑, coupling ↓).
- ADR-062 в части «delta-trigger использует `count_active(region_ids)` IN-список»
  пересмотрен: для delta-trigger теперь глобальный счёт. Сам метод
  `count_active(region_ids=...)` и его использование в cutoff/suppression
  (ADR-039/062) **остаются** — меняется только вызов в delta-check.
- ADR-035 §I1: «fetch-scope = macro-region via `region=`» помечается как
  эмпирически неточный — фактический fetch-scope = операторский pocket, единый
  для всех регионов. Полный пересмотр модели fetch-scope (нужен ли `region=`
  вообще, как pocket соотносится с подпиской на регионы) — отдельная задача.
- Перф: глобальный `COUNT(*) WHERE is_active=1` не покрывается индексом
  `idx_lots_region_id_active(region_id, is_active)` (нет ведущего `is_active`),
  но при объёме каталога (сотни–тысячи лотов; прод pocket=346) и частоте «раз в
  цикл на регион» это микросекунды — отдельный индекс `ON lots(is_active)` **не
  добавляется** (преждевременная оптимизация).
- `tools/fake_torgi` (игнорирует `region=`, отдаёт `total=len(all_lots)`)
  **случайно корректно** моделирует прод-донора — repro на стенде валиден.

---

## Дополнение (2026-06-01): схлопывание fetch-петли до одного запроса/цикл

Поскольку `region=` — no-op (донор отдаёт один pocket-набор на любой регион),
итерация `for region in settings.regions` в fetch-коде слала **дубль-запросы за
тем же набором**. Устранено:

- `MonitorCycleService.run_forever` — вместо петли по `settings.regions` делает
  **один** head-poll за проход: `fetch_region = settings.regions[0]`,
  `run_cycle(fetch_region)`. Guard на пустой список и backfill-skip применяются к
  `fetch_region`. Сигнатуры `run_cycle`/`_run_cycle_inner`/`CycleResult.region` не
  меняются.
- `FullScanService.run_once` — **один** walk вместо N: `fetch_region =
  settings.regions[0]`; `all_regions_completed` = `pagination_completed` этого
  единственного walk (упал → mass-deactivation подавляется). Гард «`seen_ids`
  пуст → abort» сохранён.
- `BackfillService` — **не трогается**: per-region семантика остаётся;
  `maybe_start(region_id)` получает `fetch_region`; login-path `_run(regions=None)`
  по-прежнему читает `settings.regions` для начального seeding.
- Новая константа НЕ вводится — используется `settings.regions[0]` (донор всё
  равно игнорирует значение; именованная `FETCH_REGION` создала бы иллюзию
  значимости параметра).

**Не затронуто** (`settings.regions` остаётся SSOT для подписок/notify/view):
`config_source._do_reload` (seeding `region_subscriptions`, ADR-039/062),
онбординг, `rf_subjects`-notify, `view_filters`, UI.

**Амендирует:** ADR-036 §H4 (cost per pass: было `len(settings.regions) × 1`,
стало **1** HTTP request/cycle — единый fetch-region); ADR-035 §I1 (fetch НЕ
итерирует все `settings.regions` — один head-poll с `regions[0]`).

**Known limitation:** при `len(settings.regions) > 1` И если донор когда-нибудь
сделает `region=` реальным фильтром — один fetch по `regions[0]` будет недобирать
остальные регионы. Сейчас невозможно (region= no-op, прод монорегионален). Тот же
tech debt, что A2 выше.

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| (A) Исправить `SUBJECTS_BY_MACRO` под фактическую группировку донора | `region=` ничего не группирует (no-op) — нечего выравнивать. Требовало бы live-donor выверки и было бы хрупко к реклассификации. |
| (C2) Колонка `lots.fetch_macro_id` (под каким `region=` вытянут лот), счёт по ней | При region-независимом доноре одни и те же лоты приходят под обоими `region=` → нельзя посчитать под двумя macro (first-writer-wins) → multi-region зацикливается. Хуже C1 + миграция схемы + правка ingest. Нарушает «минимальное решение». |
| Relative-growth (сравнивать `total_count` с предыдущим значением) | Слепо к неполному/частично-упавшему backfill: `total` не растёт, `db<total`, но триггера нет → каталог молча недокачан. Correctness-регрессия + большая смена контракта ADR-036. |
| Снять `use_filter_pocket=1`, чтобы `region=` заработал | Pocket — осознанный операторский фильтр (сужает нагрузку, server-performance-v3). Снятие меняет, КАКИЕ лоты видит монитор — вне scope багфикса. |

---

## References

- `src/fis_monitor/services/monitor_cycle.py` — delta-check: `count_active()` (глобально)
- `src/fis_monitor/services/backfill.py::BackfillService.maybe_start` — delta-trigger gate (без изменений)
- `src/fis_monitor/infra/http/url_builder.py` — `_LIST_QUERY` содержит `use_filter_pocket=1`
- `src/fis_monitor/infra/sqlite/repositories/lots.py::count_active` — `SELECT COUNT(*) WHERE is_active=1 [AND region_id IN (...)]`
- `tests/unit/services/test_monitor_cycle_delta.py::test_count_active_called_globally_not_per_subject`
- `docs/ops/server-performance-v3.md` — `filter_pocket` сужает выдачу
- [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]], [[decisions/ADR-035-three-scope-filter-model|ADR-035]], [[decisions/ADR-062-region-subscription-namespace|ADR-062]]
- [[glossary#use_filter_pocket]], [[glossary#Fetch scope]]
