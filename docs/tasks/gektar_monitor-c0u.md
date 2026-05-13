---
bd-id: gektar_monitor-c0u
title: Domain — расширение DTO для Protocol-швов
status: closed
closed: 2026-05-13
files:
  - docs/data-model.md
  - pyproject.toml
  - src/fis_monitor/domain/__init__.py
  - src/fis_monitor/domain/models.py
  - tests/domain/test_models_ext.py
  - tests/domain/test_sse_schema.py
---

# Domain — расширение DTO для Protocol-швов

## Что сделано

- Добавлено ~18 DTO, которые нужны типизации Protocol-интерфейсов (bd 531.2):
  полное дерево `Settings` (10 вложенных конфиг-классов), `LotUserState`, `CycleResult`,
  `NotificationRecord` (ADR-019 state machine), `NotifierConfig`, `ParsedListRow`,
  `ParsedDetail`, `HttpResponse`, `LockHandle`.
- `OnboardingState` реализован как `StrEnum` (5 состояний FSM: `not_started` →
  `regions_set` → `smtp_configured` → `recipients_set` → `completed`).
- `LoginOutcome.error` ограничен `Literal["timeout","cancelled","playwright_disconnect",
  "playwright_timeout","playwright_other"]` — закрывает free-form text вектор.
- `NotifyResult` — `pydantic_dataclass` с `detail: str | None`, `max_length=500`;
  docstring контракт: log-only, NEVER публикуется в SSE-шину (см. [[decisions-log#ADR-003]]).
- `SsePayloadSchema.SESSION_EXPIRED` обновлён: убран `redirect_url`
  (SSO-токен/return-параметры = PII-вектор).
- `SseEvent` — `type` alias (PEP 695 union) всех SSE-payload типов;
  `EventSubscription[T]` — generic Protocol с `__iter__()` по канону.
- `pyproject.toml`: добавлена зависимость `pydantic[email]` для `EmailStr` в Settings.
- 85 тестов всего (54 новых в `test_models_ext.py` + 3 обновлённых `test_sse_schema.py`).

## Почему так

- `OnboardingState` как `StrEnum` — [[decisions-log#ADR-018]]: server-side FSM,
  строковое значение хранится в state-таблице под ключом `onboarding_state`.
- `NotificationRecord` с `status`/`attempt_no`/`last_attempt_at` — [[decisions-log#ADR-019]]:
  durable state machine, recovery поднимает `status='pending'` при рестарте.
- `NotifyResult` log-only контракт — [[decisions-log#ADR-003]]: Result только для Notifier,
  detail не должен нести PII (recipient, smtp_response) в SSE-трансляцию.
- `LoginOutcome.error` = Literal — предотвращает попадание exception `__class__.__name__`
  или stack trace в структурированный лог; Playwright-специфичные коды явно перечислены.
- `CycleResult.error` ограничен `max_length=200` с PII-docstring — аналогичная защита
  для записей в таблицу `cycles`.

## Связи

- Закрывает: `bd #gektar_monitor-c0u`
- Связано: [[gektar_monitor-531.1]], [[decisions-log#ADR-018]], [[decisions-log#ADR-019]],
  [[decisions-log#ADR-003]], [[data-model]], [[onboarding]]
- Новые термины: [[glossary#OnboardingState]], [[glossary#NotifierConfig]]

## Follow-up

- Разблокирован: `gektar_monitor-531.2` (Protocol-интерфейсы).
- Follow-up issues: `z9d` (перенести `EventSubscription`/`ConfigSubscription` в `interfaces.py`),
  `0u7` (разбить `models.py`), `0t8` (diagnostics exclude-list), `arl` (тест на утечку
  `NotifyResult.detail` в Dispatcher), `vn5` (ADR для pydantic[email]), `7pi` (max_length для
  `LotUserState.note`).
