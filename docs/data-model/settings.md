# Settings, SmtpCredentials, OnboardingState

Pydantic v2 модели для `config.json` + state.db user-state.

## Settings — `config.json`

Полная Pydantic v2 модель `config.json`. Раскладку по разделам и валидацию см. в [[config-reference]]. `smtp_password` здесь **отсутствует** — хранится в `state.db` (см. ниже `SmtpCredentials`).

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
    smtp_host: str | None = None  # ADR-024: host в state.db; None = дефолтный из infra/smtp/constants.py
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
    interval_minutes: int = Field(1, ge=0, le=60)  # 0 = непрерывно (без пауз между циклами), 1 = по умолчанию (1 минута между циклами)
    timezone: str = "Europe/Moscow"
    regions: list[int] = Field(default_factory=lambda: [1, 2])  # 1=ДФО, 2=Арктика (macro-ids)
    subject_site_ids: list[int] = Field(default_factory=list)
    # fetch-scope: site-id субъектов (27–96, domain/regions.py::SUBJECT_TITLE_BY_ID).
    # Пустой список = тянуть всё из выбранных макрорегионов (поведение по умолчанию).
    # Непустой список = rfSubjectId[] добавляется к URL запроса (уточняющий фильтр).
    # ADR-031: отличается от FiltersConfig.rf_subjects (notify-time, OKTMO-коды).
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
```

## SmtpCredentials — state.db

SMTP-параметры (host, port, user, password) хранятся в `state.db` (одна строка с id=1), **не в `config.json`**. 

См. [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]] (SMTP SSOT = state.db) и [[decisions/ADR-024-target-config-and-url-builder|ADR-024]] (хост и порт не должны быть в domain models).

```python
from pydantic import SecretStr

class SmtpCredentials(BaseModel):
    smtp_user: str
    smtp_password: SecretStr    # ADR-017: __repr__/__str__ → '***'. plain — .get_secret_value()
    smtp_host: str              # формат-валидатор (см. ADR-015); policy — infra/smtp/host_policy.py
    smtp_port: int = Field(587, ge=1, le=65535)   # R4-C1, ADR-020: SSOT = state.db
    use_default: bool = True    # True = использовать дефолтный бот-ящик (`smtp.yandex.ru:587` из infra/smtp/constants.py)
    from_name: str | None = None  # RFC 5322 display name → From: "Имя" <user@host>; None = bare email (bd ljp)

    model_config = ConfigDict(frozen=True)
```

Поле `from_name` хранится в `state.db` как `smtp_from_name TEXT` (nullable). Добавлено миграцией v2→v3 (`infra/sqlite/migrations_v2_to_v3.py`). Если `None` — `SmtpEmailNotifier` использует bare `smtp_user` в заголовке `From:`; если задано — `"Display Name" <smtp_user>` (RFC 5322 format, кодирование через `email.headerregistry.Address`).

Дефолтные значения (`DEFAULT_SMTP_HOST = "smtp.yandex.ru"`, `DEFAULT_SMTP_PORT = 587`) живут в коде — fallback на случай пустой таблицы при первой установке.

**UI prefill через `SmtpProviderCatalog` (ADR-038).** При вводе email-логина wizard / `/settings`-форма дёргают `GET /settings/smtp/suggest?email=...`. Если домен в каталоге провайдеров (`yandex.ru`, `mail.ru`, `gmail.com`, `outlook.com`, `icloud.com`, `rambler.ru`, ...) — UI подставляет `smtp_host`, `smtp_port`, `use_starttls` (и опционально показывает app-password hint). **БД-схема `smtp_credentials` от этого не меняется** — host/port пишутся в те же существующие колонки. Suggestion — UX-помощник: на сабмите формы `DefaultSmtpHostPolicy.resolve_and_check()` валидирует host/port независимо ([[decisions/ADR-015-smtp-host-validation|ADR-015]] fail-closed pipeline сохранён). Неизвестный домен → suggestion=null → UI разворачивает advanced-секцию с manual-вводом. См. [[decisions/ADR-038-smtp-provider-catalog|ADR-038]].

`SecretStr` гарантирует что пароль не утечёт в crash-логи через `__repr__`. Получить plain-value — только явным `.get_secret_value()` в `SmtpEmailNotifier.send()`. Хранение plain в `state.db` сохраняется (ACL `%LOCALAPPDATA%` достаточен — см. [[decisions-log]]).

## OnboardingState — FSM

Server-side FSM (см. [[onboarding]], [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]]). Замена номерных «шагов» на explicit states.

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

## LotUserState

Не теряется при reparse mirror (отдельная таблица, см. `db/schema.sql` → таблица `lot_user_state`).

```python
class LotUserState(BaseModel):
    lot_id: int
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None
    updated_at: datetime
```

См. также: [[data-model/lot]], [[config-reference]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]], [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]], [[decisions/ADR-038-smtp-provider-catalog|ADR-038]], [[decisions/ADR-053-remove-favorites-feature|ADR-053]].
