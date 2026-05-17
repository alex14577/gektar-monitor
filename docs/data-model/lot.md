# Lot, LotPublicDTO, LotUserDTO, CycleResult, FieldChange

Доменные модели лотов, DTO для UI/EventBus, diff-протокол репозитория.

## Lot — основная модель лота

Соответствует таблице `lots` (см. `db/schema.sql`). Покрывает данные из таблицы списка и детальной карточки `cabinet-free-lot-view` (см. [[parser/cabinet-free-lot]]).

```python
from datetime import datetime


class Lot(BaseModel):
    # Идентификация
    id: int                          # data-key сайта (== rowid)
    cadastral_no: str                # INDEX, не UNIQUE

    # Колонки списка / карточки
    area_sqm: int | None
    region: str                      # макрорегион/название
    municipality: str | None
    land_category: str | None
    permitted_use: str | None        # ВРИ
    ogv: str | None
    status: str                      # «Свободен», «Зарезервирован», ...
    date_create: datetime            # DATE_CREATE из списка /cabinet/free-lot (ФИС DB, НЕ ЕГРН)
    date_update: datetime | None     # «Дата изменения сведений в ЕГРН» с detail-страницы
    date_registry: datetime | None   # «Дата постановки на учет» с detail-страницы (ЕГРН). NULL до обогащения. ADR-040

    # Координаты (для R-tree)
    lat: float | None
    lon: float | None
    has_boundaries: bool | None

    # Расширяемость
    raw_json: dict                   # все прочие поля карточки
    parser_version: int = 1

    # Жизненный цикл
    first_seen: datetime
    last_seen: datetime
    detail_fetched_at: datetime | None
    enrichment_status: Literal["pending", "done", "failed", "permanent_fail"] | None

    # Removal-tracking (см. [[decisions-log]] → Removal-detection)
    last_seen_at: datetime | None
    is_active: bool = True
    inactive_reason: Literal["status_changed", "hard_removed", "list_absent"] | None = None
    inactive_since: datetime | None = None
    inactive_confirmed_at: datetime | None = None
```

## LotPublicDTO / LotUserDTO — разделение публичной и user-state части

Разделение принято для forward-compat с multi-user v3 (хостинг): SSE-fan-out не должен утечь user-state одной вкладки в другие. См. [[architecture/03-protocols]] §3.6.1 (N-minor).

```python
class LotPublicDTO(Lot):
    """Лот БЕЗ user-state. Безопасно публиковать через EventBus."""
    age_seconds: int                                       # для тикера в браузере
    tier: Literal["match", "silent", "gone"]                # для звука/стиля
    freshness: Literal["hot", "warm", "cool", "cold"]       # для цвета бордера

    model_config = ConfigDict(frozen=True)


class LotUserDTO(LotPublicDTO):
    """LotPublicDTO + LotUserState. Возвращается в server-rendered HTML
    или через отдельный GET /api/lots/{id}/user-state."""
    starred: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None

    model_config = ConfigDict(frozen=True)


# Deprecated alias для обратной совместимости с существующими ссылками в коде —
# будет удалён после миграции всех use cases.
LotDTO = LotUserDTO
```

EventBus публикует **только** `LotPublicDTO`. UI на главной странице получает `LotUserDTO` через server-rendered HTML (one-shot, не SSE).

## CycleResult — запись в `cycles`

```python
class CycleResult(BaseModel):
    id: int
    region: int
    started_at: datetime
    finished_at: datetime
    status: Literal["ok", "error", "aborted"]
    lots_fetched: int
    new_lots: int
    error: str | None = None
    id_schema_check: Literal["ok", "anomaly", "confirmed"] = "ok"
```

## FieldChange / LotUpsertResult — diff-протокол репозитория

Контракт `LotRepository.upsert(lot, *, tracked)` — см. [[architecture/03-protocols]] §3.1, [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]] (R3-C2). Caller передаёт **только** список tracked-полей; `compute_changes()` зовётся repo внутри BEGIN IMMEDIATE tx (закрывает TOCTOU между SELECT old и UPDATE). `LotUpsertResult.changes` содержит фактически записанные FieldChange.

```python
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict

# Whitelist допустимых полей для tracking в lots_history.
# Поле — Literal, инъекции в SQL-identifier невозможны на уровне типа.
TrackedField = Literal[
    "status", "area_sqm", "date_update", "auction", "is_active", "list_presence",
]


class FieldChange(BaseModel):
    field: TrackedField
    old_value: Any | None              # сериализуется json.dumps в БД (см. schema.sql)
    new_value: Any | None              # сериализуется json.dumps в БД

    model_config = ConfigDict(frozen=True)


class LotUpsertResult(BaseModel):
    was_new: bool                       # True — это INSERT, history НЕ пишется
    changes: list[FieldChange]          # фактически записанные в lots_history

    model_config = ConfigDict(frozen=True)
```

`compute_changes(old: Lot | None, new: Lot, tracked: Sequence[TrackedField]) -> list[FieldChange]` живёт в `domain/diff.py`. Чистая функция.

## LotFilters — критерии фильтрации для LotQueryService

`@dataclass(frozen=True)` в `services/lot_query.py`. Все поля опциональны / по умолчанию пусты; комбинируются конъюнктивно в `_build_query`.

```python
@dataclass(frozen=True)
class LotFilters:
    regions: tuple[int, ...]                  # numeric macro-region codes (legacy API path, /lots endpoint)
    subject_display_names: tuple[str, ...]    # display-name strings из SUBJECT_TITLE_BY_ID
                                              # (web filter path, конвертируется из cookie view_filters)
                                              # SQL: WHERE region IN (?, ?, ...) со строками
    area_sqm_min: Decimal | None = None
    area_sqm_max: Decimal | None = None
    status: str | None = None                 # валидируется против {"Свободен", "Зарезервирован"}
    fts_query: str | None = None              # FTS5 — deferred (NotImplementedError)
```

**Пути использования:**
- `/lots` API endpoint — передаёт `regions` (numeric), `subject_display_names` пуст.
- Web filter path (`POST /filters/view`, `GET /`) — передаёт `subject_display_names` из cookie `view_filters` через `build_feed_context` (в `web/feed_context.py`), `regions` пуст.

**Guard mutual-exclusion:** `LotFilters.__post_init__` выбрасывает `ValueError` если одновременно непусты `regions` и `subject_display_names` — оба поля взаимоисключающие.

**Преобразование из View-scope:** `build_feed_context` (в `web/feed_context.py`) читает `view_filters.subjects` (list[str] display-names) → `LotFilters(subject_display_names=tuple(subjects))`. Mapping site-id → display name через `SUBJECT_TITLE_BY_ID` в `domain/regions.py`.

См. [[glossary#LotFilters]], [[decisions/ADR-035-three-scope-filter-model|ADR-035]] (View scope).

## См. также

[[data-model/settings]] (LotUserState), [[data-model/notifications]], [[data-model/sse]].
