# ADR-035 — Three-Scope Filter Model: Fetch / Notify / View

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: filters, fetch-scope, notify-scope, view-scope, subject-site-ids, rf-subjects
**Supersedes**: [[decisions/ADR-031-region-ssot-site-id|ADR-031]] §Q3 и Addendum (поле `subject_site_ids` как fetch-narrowing и UX-инвариант «≥1 субъект»).

---

## Context

В коде существуют три разных «слоя фильтрации» субъектов, но они нигде не формализованы явно:

1. **Fetch-scope**: какие макрорегионы (macro-id) тянуть с сайта. SSOT — `Settings.regions`. Реализован через параметр `region=` в URL (`monitor_cycle.py:392` — `self._url_builder.lot_list_url(region=region)` без какого-либо субъектного сужения).
2. **Notify-scope**: по каким субъектам (site-id) рассылать уведомления post-fetch. SSOT — `Settings.filters.rf_subjects`. Реализован в `services/filter_matcher.py:67–68` (пустой список = notify-all).
3. **View-scope**: какие субъекты показывать в браузерной ленте. SSOT — cookie `view_filters` + каталог `SUBJECT_TITLE_BY_ID`. Реализован в `web/routes/filters.py`.

ADR-031 §Q3 вводил **четвёртое** поле `Settings.subject_site_ids` как fetch-time субъектный скоуп (site-id → `FreeLotSearch[rfSubjectId][]`). Однако ни один production-вызов это поле не передаёт: `MonitorCycleService._run_cycle_inner` (`monitor_cycle.py:392`) вызывает `lot_list_url(region=region)` без `subject_site_ids`. `PaginatedListFetcher.iterate` принимает параметр `subject_site_ids: tuple[int, ...] = ()` (`paginated_list_fetcher.py:77`), но его вызывает только `BackfillService`, и даже там он не транслируется из `Settings`. Поле остаётся мёртвым.

Addendum ADR-031 вводил UX-инвариант «≥1 subject required» для `subject_site_ids`, однако семантика notify-all (пустой `rf_subjects`) уже проверена и подтверждена пользователем 2026-05-15. Мёртвый fetch-field и конфликтующий UX-инвариант создают долг.

---

## Decision

Формализовать **три скоупа** с явными SSOT и потребителями:

| Scope | SSOT | Consumer |
|---|---|---|
| **Fetch** | `Settings.regions` (macro-region ids; 1=ДФО, 2=Арктика) | `infra/http/url_builder.TorgiUrlBuilder.lot_list_url(region=...)` — только макропараметр `region=`, без субъектного сужения в URL |
| **Notify** | `Settings.filters.rf_subjects` (site-id ints из полного каталога `SUBJECT_TITLE_BY_ID`) | `services/filter_matcher.RfSubjectFilterMatcher` в `monitor_cycle._run_cycle_inner` — gate перед `NotifierDispatcher.dispatch` |
| **View** | cookie `view_filters.subjects` + каталог `SUBJECT_TITLE_BY_ID` | `web/routes/filters.get_subjects` + `services/lot_query.LotQueryService` |

Поле `Settings.subject_site_ids` (ADR-031 §Q3) признаётся **мёртвым** и удаляется (bd `gektar_monitor-6f6`).

---

## Invariants

**I1. Fetch ⊇ Notify** на уровне доступности данных: уведомление может прийти только по лоту, который fetch уже вытянул. Macro-region URL — единственный knob fetch-скоупа. Субъектного сужения на уровне HTTP-запроса нет (`monitor_cycle.py:392`).

**I2. Notify ⊆ SUBJECT_TITLE_BY_ID**: множество notify-субъектов является подмножеством полного каталога поддерживаемых субъектов (`domain/regions.py::SUBJECT_TITLE_BY_ID`). Значения вне каталога не матчатся (`filter_matcher.py:70–73`: `lot_region_id = self._NAME_TO_ID.get(lot.region)` + fail-open).

**I3. View НЕЗАВИСИМ от Notify**: пользователь может фильтровать ленту по субъектам, на которые он не подписан, и наоборот. Скоупы разделены: notify-фильтр в `config.json` (`Settings.filters.rf_subjects`), view-фильтр в cookie (`view_filters`).

**I4. Пустой `filters.rf_subjects` ⇒ notify-all**: поведение унаследовано от `filter_matcher.py:67–68` (`if not filters.rf_subjects: return True`) и подтверждено пользователем 2026-05-15. Это явный инвариант, а не дефект.

---

## Consequences

- **Удаление `Settings.subject_site_ids`** (bd `gektar_monitor-6f6`): поле удаляется из `domain/models.py`. Добавляется `model_validator(mode="before")` — миграционный shim: если в raw dict присутствует `subject_site_ids` с непустым значением, а `filters.rf_subjects` пуст, значения копируются в `filters.rf_subjects`, сохраняя пользовательский intent из старых `config.json`. Ключ `subject_site_ids` pop-ается до валидации Pydantic.
- **Удаление параметра `subject_site_ids`** из `infra/http/url_builder.lot_list_url` и `services/paginated_list_fetcher.iterate` (bd `gektar_monitor-6f6`): production-callers не передают его; dead-code чище удалить, чем оставлять как extension point.
- **Переименование в UI** (bd `gektar_monitor-4fn`): `/settings` — поле переименовывается «Регионы (субъекты РФ)» → «Субъекты уведомлений»; поле привязывается к `filters.rf_subjects`; пустой выбор разрешён с поясняющим текстом («все субъекты выбранных макрорегионов»).
- **`/filters/subjects` (sidebar)** рендерит полный каталог `SUBJECT_TITLE_BY_ID` независимо от региона (bd `gektar_monitor-4fn`), а не только субъекты из `subject_site_ids`.

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| (a) Переименовать `subject_site_ids` → notify-поле | Семантическая ловушка: имя говорит «site-ids», прошлый ADR-031 давал ему fetch-семантику; cross-references сломались бы |
| (b) Сохранить оба поля (`subject_site_ids` + `rf_subjects`) | Мёртвое поле остаётся навсегда; два поля с похожими именами путают будущих разработчиков |
| (c) Удалить migration shim, требовать ручной рекконфигурации | Потеря пользовательского intent в `config.json`; неприемлемо для production-конфигураций |

---

## Migration

One-shot `model_validator(mode="before")` в `Settings`:

```python
@model_validator(mode="before")
@classmethod
def _migrate_subject_site_ids(cls, data: dict) -> dict:
    """Migrate legacy subject_site_ids → filters.rf_subjects (ADR-035)."""
    legacy = data.pop("subject_site_ids", None)
    if legacy:
        filters = data.get("filters") or {}
        if not filters.get("rf_subjects"):
            filters["rf_subjects"] = legacy
            data["filters"] = filters
    return data
```

После того как `gektar_monitor-6f6` смержен, старые `config.json` загружаются прозрачно. Поле `subject_site_ids` игнорируется парсером (pop до Pydantic-валидации).

---

## References

- `src/fis_monitor/domain/models.py:350–355` — `FiltersConfig.rf_subjects` definition
- `src/fis_monitor/domain/models.py:460–483` — `Settings.regions`, `Settings.subject_site_ids`, `Settings.filters`
- `src/fis_monitor/services/filter_matcher.py:58–75` — `RfSubjectFilterMatcher.matches` (I4 source)
- `src/fis_monitor/services/monitor_cycle.py:392` — `lot_list_url(region=region)` без subject_site_ids
- `src/fis_monitor/services/paginated_list_fetcher.py:77–120` — dead `subject_site_ids` parameter
- `src/fis_monitor/web/routes/filters.py:118–142` — view-scope implementation
- [[decisions/ADR-031-region-ssot-site-id|ADR-031]] — superseded §Q3 + Addendum
- [[decisions/ADR-024-target-config-and-url-builder|ADR-024]] — url-builder design
- [[glossary#Fetch scope]], [[glossary#Notify scope]], [[glossary#View scope]]
