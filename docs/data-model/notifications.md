# NotificationRecord, NotifierConfig, NotifyResult, ResolvedSmtpEndpoint

Канон Pydantic-моделей и dataclass'ов для уведомлений и SMTP-инфраструктуры.

## ResolvedSmtpEndpoint (R3-C4, [[decisions/ADR-015-smtp-host-validation|ADR-015]] ext)

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

## NotifierConfig — плагин-архитектура

Базовая абстрактная схема (см. [[notifications]] → плагины). Конкретные реализации:

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

PK — `(lot_id, channel, recipient)` (см. [[decisions/ADR-019-notification-state-machine|ADR-019]]). `sent_at` — audit, не часть ключа. `recipient='local'` для browser/heartbeat.

```python
class NotificationRecord(BaseModel):
    lot_id: int
    channel: Literal["email", "browser", "heartbeat"]
    recipient: str                  # email или 'local'
    sent_at: datetime
```

## NotifyResult

См. [[architecture/03-protocols]] §3.3 — `Notifier.send()` возвращает `NotifyResult(ok, detail, retryable)`. Result-pattern только для Notifier ([[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]]).

См. также: [[notifications]], [[data-model/sse]], [[data-model/errors]].
