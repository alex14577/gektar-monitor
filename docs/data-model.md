# Модели данных

Канон Pydantic-моделей и dataclass'ов проекта. Источник правды для DTO,
API-контрактов, валидации `config.json` и schema'ы SSE-событий.

Соответствует [[decisions-log]] (стек: Pydantic v2, sqlite3 sync, SMTP-пароль в
state.db, tier решает сервер) и [[db/schema|db/schema.sql]] (mirror +
user-state, removal-tracking).

## Settings — `config.json`

Полная Pydantic v2 модель `config.json`. Раскладку по разделам и валидацию
см. в [[config-reference]]. `smtp_password` здесь **отсутствует** — хранится
в `state.db` (см. ниже `SmtpCredentials`).

```python
from typing import Literal
from pydantic import BaseModel, Field, EmailStr


class FiltersConfig(BaseModel):
    rf_subjects: list[int] = Field(default_factory=list)
    # notify-time фильтр субъектов РФ; пусто = все из выбранных макрорегионов


class UIConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    port: int = Field(8080, ge=1024, le=65535)
    auto_open_browser: bool = True
    font_size_px: Literal[14, 16, 18] = 16
    theme: Literal["auto", "light", "dark"] = "auto"


class EmailConfig(BaseModel):
    enabled: bool = True
    use_default_smtp: bool = True
    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = Field(587, ge=1, le=65535)
    from_address: str | None = None
    recipients: list[EmailStr] = Field(default_factory=list)
    # smtp_user / smtp_password — НЕ здесь, см. SmtpCredentials


class BrowserConfig(BaseModel):
    enabled: bool = True


class HeartbeatConfig(BaseModel):
    enabled: bool = False
    time: str = "09:00"  # HH:MM, локальная TZ


class SoundEscalationConfig(BaseModel):
    enabled: bool = True
    escalate_at_seconds: list[int] = Field(default_factory=lambda: [60, 120])


class DndConfig(BaseModel):
    until: str | None = None  # ISO timestamp, null = выключено


class CatchupConfig(BaseModel):
    enabled: bool = True
    min_offline_minutes: int = 60


class NotificationsConfig(BaseModel):
    email: EmailConfig = Field(default_factory=EmailConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    sound_escalation: SoundEscalationConfig = Field(default_factory=SoundEscalationConfig)
    dnd: DndConfig = Field(default_factory=DndConfig)
    catchup: CatchupConfig = Field(default_factory=CatchupConfig)


class MonitoringConfig(BaseModel):
    full_scan_time: str = "04:00"          # HH:MM локального TZ
    full_scan_l2_priority_days: int = 7    # L2-verification для лотов младше N дней


class Settings(BaseModel):
    mode: Literal["local", "server"] = "local"
    interval_minutes: int = Field(15, ge=0, le=60)  # 0 = непрерывно
    timezone: str = "Europe/Moscow"
    regions: list[int] = Field(default_factory=lambda: [1, 2])  # 1=ДФО, 2=Арктика
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
```

## SmtpCredentials — state.db

SMTP-логин и пароль хранятся в `state.db` (одна строка с id=1, см.
[[decisions-log]] → «SMTP-пароль хранится в state.db»). В `config.json`
их нет.

```python
from pydantic import SecretStr

class SmtpCredentials(BaseModel):
    smtp_user: str
    smtp_password: SecretStr    # ADR-017: __repr__/__str__ → '***'. plain — .get_secret_value()
    smtp_host: str              # формат-валидатор (см. ADR-015); policy — infra/smtp/host_policy.py
    smtp_port: int = Field(587, ge=1, le=65535)   # R4-C1, ADR-020: SSOT = state.db
    use_default: bool = True    # True = бот-ящик, зашитый в сборку

    model_config = ConfigDict(frozen=True)
```

`SecretStr` гарантирует что пароль не утечёт в crash-логи через `__repr__`. Получить plain-value — только явным `.get_secret_value()` в `SmtpEmailNotifier.send()`. Хранение plain в `state.db` сохраняется (ACL `%LOCALAPPDATA%` достаточен — см. decisions-log).

### ResolvedSmtpEndpoint (R3-C4, ADR-015 ext)

Pin'нутый результат DNS-resolve + policy-check для одного connect-цикла. `SmtpHostPolicy.resolve_and_check()` возвращает этот объект, `SmtpEmailNotifier.send()` использует `ip` для connect (закрывает TOCTOU), `original_host` для SNI / TLS-cert validation.

```python
from dataclasses import dataclass
import socket

@dataclass(frozen=True)
class ResolvedSmtpEndpoint:
    ip: str                          # resolved IPv4 dotted или IPv6 literal
    family: socket.AddressFamily     # AF_INET | AF_INET6
    port: int
    original_host: str               # для SNI и hostname-verification
```

Не Pydantic-модель — это infra-внутренний DTO, не пересекает domain-границу и не сериализуется.

### SsePayloadSchema (R3-C5, ADR-008 ext)

Whitelist полей для persist'а critical-event в таблицу `state` и для логирования force-unsubscribe. Закрывает утечку PII (stacktrace, email-адреса) через `last_critical_event:*` ключи.

```python
class SsePayloadSchema:
    """Whitelist полей по типу события — для persist + redactor-логов.
    Поля ВНЕ списка вырезаются перед записью в state и перед logger.warning."""
    SESSION_EXPIRED = frozenset({"timestamp", "event"})
    CYCLE_ERROR     = frozenset({"timestamp", "cycle_id", "error_category"})
    SMTP_FAILED     = frozenset({"timestamp", "channel_id", "error_category",
                                 "attempt_no"})
    # Явно НЕ включаем: stacktrace, exception_repr, recipient, smtp_response,
    # cookies, tokens, request/response body.

    @classmethod
    def for_event(cls, event_type: str) -> frozenset[str]: ...


# R4-M5: error_category — закрытый Literal-enum.
# Произвольная строка (exception.__class__.__name__, например) — НЕ допускается.
# Mapper в use case переводит низкоуровневое исключение в одну из категорий.
ErrorCategory = Literal[
    "network", "http_5xx", "http_4xx", "redirect_login",
    "timeout", "parse_bug", "schema_anomaly",
]


class SseCycleError(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    cycle_id: int
    error_category: ErrorCategory
    # ЯВНО БЕЗ: stacktrace, exception_repr, raw error messages — это PII-vector
    # (stacktrace может содержать request body / cookies / SQL с email).


class SseSmtpFailed(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    channel_id: str          # e.g. "email"
    error_category: ErrorCategory
    attempt_no: int
    # ЯВНО БЕЗ: recipient, smtp_response, smtp_code, exception_repr.
```

`EventBus.publish(event)` при сохранении critical-события в state делает `payload = {k: v for k, v in event.dict().items() if k in SsePayloadSchema.for_event(event.event_type)}` и сериализует только это. Аналогично — redactor для `logger.warning` при force-unsubscribe.

## Lot — основная модель лота

Соответствует таблице `lots` (см. [[db/schema|db/schema.sql]]). Покрывает данные из таблицы списка и
детальной карточки `cabinet-free-lot-view` (см. [[cabinet-free-lot]]).

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

    # Removal-tracking (см. decisions-log → Removal-detection)
    last_seen_at: datetime | None
    is_active: bool = True
    inactive_reason: Literal["status_changed", "hard_removed", "list_absent"] | None = None
    inactive_since: datetime | None = None
    inactive_confirmed_at: datetime | None = None
```

## LotUserState

Не теряется при reparse mirror (отдельная таблица, см.
[[db/schema|db/schema.sql]] → таблица `lot_user_state`).

```python
class LotUserState(BaseModel):
    lot_id: int
    starred: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None
    updated_at: datetime
```

## LotPublicDTO / LotUserDTO — разделение публичной и user-state части

Разделение принято для forward-compat с multi-user v3 (хостинг): SSE-fan-out не должен утечь user-state одной вкладки в другие. См. architecture.md §3.6.1 (N-minor).

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

Контракт `LotRepository.upsert(lot, *, tracked)` — см. architecture.md §3.1, ADR-016 (R3-C2).
Caller передаёт **только** список tracked-полей; `compute_changes()` зовётся repo
внутри BEGIN IMMEDIATE tx (закрывает TOCTOU между SELECT old и UPDATE). `LotUpsertResult.changes`
содержит фактически записанные FieldChange.

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

## NotifierConfig — плагин-архитектура

Базовая абстрактная схема (см. [[notifications]] → плагины). Конкретные
реализации:

```python
class NotifierConfig(BaseModel):
    """Базовый класс для конфигов плагин-каналов."""


class EmailNotifierConfig(NotifierConfig):
    enabled: bool
    use_default_smtp: bool
    smtp_host: str
    smtp_port: int = Field(587, ge=1, le=65535)
    from_address: str | None = None
    recipients: list[EmailStr]


class BrowserNotifierConfig(NotifierConfig):
    enabled: bool


class HeartbeatNotifierConfig(NotifierConfig):
    enabled: bool = False
    time: str                       # HH:MM
```

## NotificationRecord — запись в `notifications`

PK — `(lot_id, channel, recipient)` (см. [[decisions-log]] → «Idempotency
notifier»). `sent_at` — audit, не часть ключа. `recipient='local'` для
browser/heartbeat.

```python
class NotificationRecord(BaseModel):
    lot_id: int
    channel: Literal["email", "browser", "heartbeat"]
    recipient: str                  # email или 'local'
    sent_at: datetime
```

## SSE event payloads

См. [[api-reference]] → SSE Events и `web/sse.py`. Все payload-ы для
`text/event-stream` — это HTML-фрагменты; ниже — структура данных,
из которой Jinja их рендерит.

```python
class SSELotNew(BaseModel):
    event: Literal["lot.new"]
    lot: LotDTO
    fragment_template: Literal["poster", "list"]


class SSELotStatus(BaseModel):
    event: Literal["lot.status"]
    lot_id: int
    new_status: str
    event_type: Literal["gone", "changed"]


class SSEStatusUpdate(BaseModel):
    event: Literal["status"]
    session: Literal["active", "expiring", "expired"]
    next_cycle_at: datetime | None
    monitor_state: Literal["running", "paused", "dnd"]


class SseSessionExpired(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    event: Literal["expired"] = "expired"
    # ЯВНО БЕЗ: redirect_url, stacktrace, exception_repr — PII/token-leak
    # vectors. `redirect_url` исключён из SsePayloadSchema.SESSION_EXPIRED
    # (URL после expire может нести return-токены / CSRF-нонсы).
```

## OnboardingState — FSM

Server-side FSM (см. [[onboarding]], ADR-018). Замена номерных «шагов» на explicit states.

```python
from enum import Enum

class OnboardingState(str, Enum):
    NOT_STARTED      = "not_started"
    REGIONS_SET      = "regions_set"
    SMTP_CONFIGURED  = "smtp_configured"
    RECIPIENTS_SET   = "recipients_set"
    COMPLETED        = "completed"
```

Текущее состояние читается через `OnboardingService.current() → OnboardingState`. Хранится в таблице `state` под ключом `onboarding_state` (`COMPLETED` ↔ legacy `onboarded=true`). Дополнительные ключи: `email_skipped`, `smtp_test_last_result_ok`, `onboarding_test_email_ok`, `onboarding_completed_at`.

## См. также

- [[decisions-log]] — все зафиксированные решения по моделям
- [[db/schema|db/schema.sql]] — каноническая SQL-схема
- [[api-reference]] — REST/SSE-эндпоинты, использующие эти модели
- [[config-reference]] — таблица ключей `config.json`
- [[notifications]] — плагин-архитектура каналов
- [[onboarding]] — FSM для OnboardingState (ADR-018)
