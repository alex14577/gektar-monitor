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
    date_create: datetime
    date_update: datetime | None

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

См. также: [[data-model/settings]] (LotUserState), [[data-model/notifications]], [[data-model/sse]].
