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

SMTP-логин и пароль хранятся в `state.db` (одна строка с id=1, см. [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]]). В `config.json` их нет.

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
    starred: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None
    updated_at: datetime
```

См. также: [[data-model/lot]], [[config-reference]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]], [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]].
