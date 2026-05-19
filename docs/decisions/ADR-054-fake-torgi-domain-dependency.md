# ADR-054 — Разрешённая зависимость tools/fake_torgi → fis_monitor.domain.regions

**Status**: Accepted
**Date**: 2026-05-19
**Deciders**: Backend Architect
**Tags**: tools, fake_torgi, domain, dependency, e2e, staging

---

## Context

`tools/fake_torgi/server.py` — staging-сервер, имитирующий `torgi.gov.ru` для ручного
тестирования и e2e-интеграций. Он должен хранить seed-лоты с канонически корректными
именами регионов (теми же, что использует продуктовый парсер), иначе region-матчинг
в `RfSubjectFilterMatcher` будет давать ложные fail-open для e2e-тестов.

Для проверки корректности регионов при запуске (`_validate_lots_on_startup`) и при
добавлении лота через Admin UI (`admin_add_lot`) `server.py` импортирует:

```python
from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID
```

Это нарушает формальное правило «tools/ не зависят от src/»: инструментальный код
тянет доменный модуль. Альтернатива — копировать snapshot регионов в
`tools/fake_torgi/_regions_snapshot.py` — порождает drift-риск: два источника истины
вместо одного.

---

## Decision

Разрешить `tools/fake_torgi/server.py` зависеть от `fis_monitor.domain.regions`
как от **read-only catalog** (только константа `SUBJECT_TITLE_BY_ID`).

Граница разрешённой зависимости — строго `domain.regions`:

| Разрешено | Запрещено |
|---|---|
| `from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID` | Любые импорты из `fis_monitor.services.*` |
| — | Любые импорты из `fis_monitor.infra.*` |
| — | Любые импорты из `fis_monitor.web.*` |
| — | Любые импорты из `fis_monitor.domain.*` кроме `regions` |

Основание: `domain.regions` — чистый иммутабельный каталог без side-effects,
без зависимостей от других модулей проекта. Использование его в tools/
эквивалентно чтению константы из общего конфига.

---

## Rationale

- **E2E fidelity**: seed-данные staging-сервера должны проходить ту же валидацию, что
  и production-парсер. Единственный SSOT — `SUBJECT_TITLE_BY_ID`.
- **No-drift**: snapshot в tools/ неизбежно устаревает при добавлении новых регионов
  в каталог; импорт живого каталога устаревание исключает.
- **Fail-fast startup**: `_validate_lots_on_startup` выбрасывает `ValueError` при старте
  если `lots.json` содержит не-каноничное имя — баг находится сразу, не в runtime.

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| Snapshot `tools/fake_torgi/_regions_snapshot.py` | Дублирование SSOT, drift-риск |
| Без валидации (любая строка в region) | Буг-репорты из e2e при несоответствии имён |
| Отдельный конфиг-файл регионов | Усложнение без пользы; каталог уже есть в domain |

---

## Consequences

- `import-linter` должен допускать `tools.fake_torgi -> fis_monitor.domain.regions`
  (если в проекте настроен `.importlinter`). Правило добавляется туда.
- При добавлении новых RF-субъектов в `SUBJECT_TITLE_BY_ID` — `tools/fake_torgi/lots.json`
  автоматически принимает новые имена без изменения кода сервера.
- Запрет на импорты из services/infra/web остаётся строгим; нарушение — blocker в code review.

---

## References

- `tools/fake_torgi/server.py` — `_CANONICAL_REGIONS`, `_validate_lots_on_startup`, `admin_add_lot`
- `src/fis_monitor/domain/regions.py` — `SUBJECT_TITLE_BY_ID`
- [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I2 — region_id SSOT
- [[decisions/ADR-006-import-linter-ci|ADR-006]] — import-linter в CI
