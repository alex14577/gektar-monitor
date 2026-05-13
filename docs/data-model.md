# Модели данных (MOC)

Stub-MOC. Атомарные ноты — в `docs/data-model/`. Источник правды для DTO, API-контрактов, валидации `config.json` и schema'ы SSE-событий.

Соответствует [[decisions-log]] (стек: Pydantic v2, sqlite3 sync, SMTP-пароль в state.db, tier решает сервер) и `db/schema.sql` (mirror + user-state, removal-tracking).

## Атомарные ноты

- [[data-model/lot]] — Lot, LotPublicDTO, LotUserDTO, CycleResult, FieldChange, LotUpsertResult, TrackedField
- [[data-model/notifications]] — NotificationRecord, NotifierConfig, NotifyResult, ResolvedSmtpEndpoint
- [[data-model/settings]] — Settings (config.json), SmtpCredentials, OnboardingState, LotUserState
- [[data-model/sse]] — SSE event payloads, SsePayloadSchema, EventSubscription
- [[data-model/errors]] — DomainError hierarchy, UpstreamError, ErrorCategory

## См. также

- [[decisions-log]] — все зафиксированные решения по моделям
- `db/schema.sql` — каноническая SQL-схема
- [[web/api-reference]] — REST/SSE-эндпоинты, использующие эти модели
- [[config-reference]] — таблица ключей `config.json`
- [[notifications]] — плагин-архитектура каналов
- [[onboarding]] — FSM для OnboardingState ([[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]])
