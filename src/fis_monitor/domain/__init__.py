"""Domain layer — pure data types, Protocols, and chaste functions.

This package MUST NOT import from `infra`, `services`, `web`, or
`repositories`. Domain knows nothing about SQLite, HTTP, SMTP, or
Playwright. Higher layers depend on `domain`, never vice-versa (DIP).
"""

from fis_monitor.domain.errors import (
    DomainError,
    ParseBugError,
    ParserVersionMismatch,
    UpstreamError,
)
from fis_monitor.domain.models import (
    BrowserConfig,
    CatchupConfig,
    ConfigSubscription,
    CycleResult,
    DndConfig,
    EmailConfig,
    ErrorCategory,
    EventSubscription,
    FieldChange,
    FiltersConfig,
    HeartbeatConfig,
    HttpResponse,
    LockHandle,
    LoginOutcome,
    Lot,
    LotPublicDTO,
    LotUpsertResult,
    LotUserDTO,
    LotUserState,
    MonitoringConfig,
    NotificationRecord,
    NotificationsConfig,
    NotifierConfig,
    NotifyResult,
    OnboardingState,
    ParsedDetail,
    ParsedListRow,
    ResolvedSmtpEndpoint,
    SessionStatus,
    Settings,
    SmtpCredentials,
    SoundEscalationConfig,
    SseCycleError,
    SseEvent,
    SseLotNew,
    SseLotStatus,
    SsePayloadSchema,
    SseSessionExpired,
    SseSmtpFailed,
    TrackedField,
    UIConfig,
)

__all__ = [  # noqa: RUF022 — grouped by responsibility, NOT alphabetical
    # errors
    "DomainError",
    "UpstreamError",
    "ParseBugError",
    "ParserVersionMismatch",
    # type aliases / enums
    "TrackedField",
    "ErrorCategory",
    "SseEvent",
    # core lot DTOs
    "Lot",
    "FieldChange",
    "LotUpsertResult",
    "LotPublicDTO",
    "LotUserDTO",
    "LotUserState",
    # SMTP / SSE schema
    "SmtpCredentials",
    "ResolvedSmtpEndpoint",
    "SsePayloadSchema",
    # SSE event DTOs
    "SseCycleError",
    "SseSmtpFailed",
    "SseSessionExpired",
    "SseLotNew",
    "SseLotStatus",
    # Settings tree
    "Settings",
    "FiltersConfig",
    "UIConfig",
    "EmailConfig",
    "BrowserConfig",
    "HeartbeatConfig",
    "SoundEscalationConfig",
    "DndConfig",
    "CatchupConfig",
    "NotificationsConfig",
    "MonitoringConfig",
    # FSM / cycle / notification
    "OnboardingState",
    "CycleResult",
    "NotificationRecord",
    "NotifierConfig",
    # Parser outputs
    "ParsedListRow",
    "ParsedDetail",
    # Notifier / auth seam DTOs
    "NotifyResult",
    "LoginOutcome",
    "SessionStatus",
    # Infra-internal frozen dataclasses
    "HttpResponse",
    "LockHandle",
    # Subscription handles
    "EventSubscription",
    "ConfigSubscription",
]
