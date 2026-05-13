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
    ErrorCategory,
    FieldChange,
    Lot,
    LotPublicDTO,
    LotUpsertResult,
    LotUserDTO,
    ResolvedSmtpEndpoint,
    SmtpCredentials,
    SseCycleError,
    SsePayloadSchema,
    SseSmtpFailed,
    TrackedField,
)

__all__ = [
    # errors
    "DomainError",
    "UpstreamError",
    "ParseBugError",
    "ParserVersionMismatch",
    # type aliases
    "TrackedField",
    "ErrorCategory",
    # DTOs
    "Lot",
    "FieldChange",
    "LotUpsertResult",
    "LotPublicDTO",
    "LotUserDTO",
    "SmtpCredentials",
    "SseCycleError",
    "SseSmtpFailed",
    "ResolvedSmtpEndpoint",
    "SsePayloadSchema",
]
