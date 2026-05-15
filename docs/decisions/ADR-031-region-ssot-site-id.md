# ADR-031 — Region SSOT: site-id mapping субъектов по макрорегионам

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: regions, url-builder, site-id, ssot, fetch-scope

---

## Context

`domain/regions.py` знает только 2 макрорегиона (dfo=1, arctic=2).
`filter_matcher.py` хардкодит OKTMO-коды субъектов (1–89) в `_RF_SUBJECT_NAMES`.
`url_builder.py` шлёт `?FreeLotSearch[rfSubjectId][]=1` (macro-id), но сайт принимает в
этом поле только свои внутренние site-id (72–96). Анализ фикстуры
`tests/fixtures/list_region1_perpage50.html` подтверждает:

- `<select name="FreeLotSearch[rfSubjectId][]">` содержит **18 опций** со значениями
  72, 85, 87, 88, 89, 90, 91, 93, 94, 95, 96 (ДФО) и 27, 28, 29, 30, 34, 68, 69, 76
  (Арктика); 87 (Якутия) и 96 (Чукотка) присутствуют в обоих.
- Никаких значений 1 или 2 в этом списке нет: передача `rfSubjectId=1` игнорируется
  сервером (он возвращает весь список без фильтра).
- Пагинация использует `?region=1` — **отдельный** параметр макрорегиона.
  Он работает сам по себе и фильтрует по макрорегиону на уровне сервера.

---

## Decision

### Q1 — URL contract: Вариант B (макропараметр `region=`)

Использовать `?region={macro_id}` как основной параметр фетча, **не** итерировать
по отдельным site-id в `rfSubjectId`. Обоснование:

- Фикстура `list_region1_perpage50.html` получена именно с `?region=1` и содержит
  лоты из Амурской, Приморского, Якутии, Магаданской одновременно — макропараметр
  работает и покрывает все субъекты ДФО одним запросом.
- Вариант A (итерация по site-id) дал бы ~11 HTTP-запросов вместо 1 на макрорегион,
  что несовместимо с rate-limit политикой (2 с между страницами × N субъектов × M страниц).
- Вариант C (гибрид) не нужен: цель fetch — полный каталог макрорегиона; отфильтровать
  по субъекту можно post-fetch через `FiltersConfig.rf_subjects`.

**Контракт `TorgiUrlBuilder.lot_list_url`**: параметр переименовывается с `region: int`
(без изменения семантики) — это по-прежнему macro_id (1=ДФО, 2=Арктика).
`_LIST_QUERY` строка обновляется с `FreeLotSearch[rfSubjectId][]=` на `region=`.

### Q3 — Новое поле `Settings.subject_site_ids`

`FiltersConfig.rf_subjects` (list[int], OKTMO) — notify-time фильтр — сохраняется
без изменений для backward compat.

Добавить **новое поле** `Settings.subject_site_ids: list[int] = []` в корень `Settings`
(не в FiltersConfig). Семантика: **fetch-scope** (какие субъекты тянуть с сайта).
Пустой список = тянуть всё из выбранных макрорегионов (поведение по умолчанию).
Непустой список = `rfSubjectId[]=<id>` передаётся вместе с `region=` (уточняющий
фильтр для уменьшения объёма fetch).

Разделение обосновано решением decisions-log §«Семантика фильтров»:
> `regions` — fetch-time (ограничено URL сайта). `rf_subjects` — только к уведомлениям.

`subject_site_ids` — третий уровень, fetch-time субъектный скоуп.

**Миграция `var/config.json`**: проект без релиза → breaking change приемлем.
Старые `config.json` без поля `subject_site_ids` десериализуются корректно (Pydantic
использует default `[]`). Поле `FiltersConfig.rf_subjects` с OKTMO-кодами теряет
практический смысл (OKTMO != site-id), но не ломает загрузку — числа просто не
совпадут ни с одним `SUBJECT_TITLE_BY_ID`. Warning-лог при load если `rf_subjects`
непустой и хотя бы один ID вне диапазона известных site-id (detect: `id not in
SUBJECT_TITLE_BY_ID`).

### Схема изменений `domain/regions.py`

Добавить в файл (все константы `MappingProxyType`, module-level):

```python
SUBJECTS_BY_MACRO: Mapping[int, tuple[int, ...]] = MappingProxyType({
    1: (72, 85, 87, 88, 89, 90, 91, 93, 94, 95, 96),   # ДФО
    2: (27, 28, 29, 30, 34, 68, 69, 76, 87, 96),         # Арктика
})

SUBJECT_TITLE_BY_ID: Mapping[int, str] = MappingProxyType({
    27: "Республика Карелия",
    28: "Республика Коми",
    29: "Архангельская область",
    30: "Ненецкий автономный округ",
    34: "Мурманская область",
    68: "Ханты-Мансийский автономный округ",
    69: "Ямало-Ненецкий автономный округ",
    72: "Республика Бурятия",
    76: "Красноярский край",
    85: "Забайкальский край",
    87: "Республика Саха (Якутия)",
    88: "Приморский край",
    89: "Хабаровский край",
    90: "Амурская область",
    91: "Камчатский край",
    93: "Магаданская область",
    94: "Сахалинская область",
    95: "Еврейская автономная область",
    96: "Чукотский автономный округ",
})

def subjects_for_macros(macro_ids: Sequence[int]) -> tuple[int, ...]:
    """Возвращает дедуплицированный union site-id субъектов по macro_ids."""
    seen: dict[int, None] = {}
    for mid in macro_ids:
        for sid in SUBJECTS_BY_MACRO.get(mid, ()):
            seen[sid] = None
    return tuple(seen)
```

Убрать `_RF_SUBJECT_NAMES` из `filter_matcher.py`; `RfSubjectFilterMatcher`
использует `SUBJECT_TITLE_BY_ID` из `domain/regions.py` как SSOT.

---

## Alternatives Considered

| Option | Reason Rejected |
|---|---|
| Вариант A: per-subject iteration (`rfSubjectId`) | ~11–20 HTTP/макрорегион; несовместимо с 2s rate-limit |
| Оставить `rf_subjects` как fetch-scope | Смешивает notify-time и fetch-time семантику; OKTMO != site-id |
| Миграция OKTMO→site-id в `rf_subjects` | Mapping неоднозначен (OKTMO 14=Якутия → site-id 87, но OKTMO 14 используется иначе в других контекстах) |

---

## Consequences

- `PaginatedListFetcher.iterate(region=macro_id)` — контракт не меняется (всё ещё int).
- `BackfillService._run` итерирует по `settings.regions` (macro-ids) — без изменений.
- `MonitorCycleService` итерирует по `settings.regions` — без изменений.
- `filter_matcher.py` использует `SUBJECT_TITLE_BY_ID` → `_RF_SUBJECT_NAMES` удаляется.
- Новое поле `Settings.subject_site_ids` передаётся в `url_builder` только если непустое.
- `/settings/subjects` endpoint (ekb) получает данные из `subjects_for_macros(settings.regions)`.

---

## References

- `src/fis_monitor/domain/regions.py` — расширение
- `src/fis_monitor/infra/http/url_builder.py` — `_LIST_QUERY` fix
- `src/fis_monitor/services/filter_matcher.py` — убрать `_RF_SUBJECT_NAMES`
- `tests/fixtures/list_region1_perpage50.html` — эмпирическое подтверждение
- [[decisions/ADR-024-target-config-and-url-builder|ADR-024]] — url-builder design
- [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]] — backfill trigger

---

## Addendum 2026-05-15 — UI invariant: `subject_site_ids` must be non-empty

Семантика домена: пустой `subject_site_ids` остаётся валидной (= все субъекты
выбранных макро). Однако веб-форма `POST /settings/subjects` теперь требует
≥1 субъект (422 при пустой submission). Аналогично `POST /settings/regions`
требует ≥1 макрорегион и автоматически усекает `subject_site_ids` до
`subjects_for_macros(new_regions)`. Это UX-инвариант защищающий от состояния
«пустой scope → пустая лента» без явного выбора пользователя; для CLI-edit
`var/config.json` поведение не меняется.
