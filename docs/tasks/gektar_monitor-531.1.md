---
bd-id: gektar_monitor-531.1
title: Domain — Pydantic DTOs (первая волна)
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/domain/__init__.py
  - src/fis_monitor/domain/errors.py
  - src/fis_monitor/domain/models.py
  - tests/domain/__init__.py
  - tests/domain/conftest.py
  - tests/domain/test_errors.py
  - tests/domain/test_models.py
  - tests/domain/test_sse_schema.py
---

# Domain — Pydantic DTOs (первая волна)

## Что сделано

- Создана иерархия ошибок: `DomainError`, `UpstreamError(category=ErrorCategory)`,
  `ParseBugError` и `ParserVersionMismatch` как сиблинги (разные recovery-пути), `SmtpHostPolicyError`.
- Заведены frozen Pydantic v2 модели с `extra="forbid"`: `Lot`, `FieldChange`, `LotUpsertResult`
  (инвариант: `was_new=True ⇒ changes=[]`), `SmtpCredentials` (пароль — `SecretStr`).
- Разделены `LotPublicDTO` (EventBus) и `LotUserDTO` (UI/REST) — `raw_json` исключён из
  всех SSE-bound моделей через `@model_serializer` (forward-compat multi-user v3).
- `ResolvedSmtpEndpoint` оформлен как `@dataclass(frozen=True, slots=True)` — infra-внутренний DTO,
  не пересекает domain-границу.
- `SsePayloadSchema` с `frozenset`-вайтлистами на тип события; `for_event()` fail-closes к пустому
  множеству при неизвестном типе (defence-in-depth против утечки PII в state).
- 31 unit-тест: иммутабельность, `extra=forbid`, `SecretStr` repr/dump, whitelist exact-sets,
  fail-closed на неизвестный тип, raw_json-exclusion.

## Почему так

- `SecretStr` для пароля — [[decisions-log#ADR-017]]: `__repr__`/`__str__` → `'***'`, plain только
  через `.get_secret_value()`.
- `LotPublicDTO` / `LotUserDTO` — [[architecture]] §3.6.1, ADR-N-minor: одна SSE-трансляция не
  должна утекать user-state между вкладками.
- `SsePayloadSchema` fail-closed — ADR-008 ext (R3-C5): persist critical-event в state без
  stacktrace/recipient. Закрывает PII-вектор через `last_critical_event:*` ключи.
- `ResolvedSmtpEndpoint` — не Pydantic намеренно: infra-dataclass, не сериализуется, закрывает
  TOCTOU connect-by-IP (см. [[decisions-log#ADR-015]] R3-C4).
- `ParseBugError` vs `ParserVersionMismatch` как сиблинги, не иерархия: разные обработчики в
  `run_forever()` (первый — cycle.error, второй — lazy reparse без alert).

## Связи

- Закрывает: `bd #gektar_monitor-531.1`
- Связано: [[decisions-log#ADR-015]], [[decisions-log#ADR-016]], [[decisions-log#ADR-017]],
  [[data-model]]
- Новые термины: [[glossary#LotPublicDTO vs LotUserDTO]], [[glossary#ResolvedSmtpEndpoint]],
  [[glossary#SsePayloadSchema]]

## Follow-up

- `gektar_monitor-bye.3` разблокирован (SmtpHostPolicy использует `ResolvedSmtpEndpoint`).
- `gektar_monitor-c0u` разблокирован (расширение моделей для Protocol-швов).
- Открытые follow-up issues: `ctz` (pickle-hardening SmtpCredentials), `x2x` (Message-ID hash docs),
  `4kh` (errors.py PII docstring).
