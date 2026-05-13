---
bd-id: gektar_monitor-0t8
title: DiagnosticsExcludePolicy — PII exclude-list для diagnostic.zip
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/services/diagnostics/__init__.py
  - src/fis_monitor/services/diagnostics/exclude_policy.py
  - tests/unit/services/diagnostics/test_exclude_policy.py
---

# DiagnosticsExcludePolicy — PII exclude-list для diagnostic.zip

## Что сделано

Реализован модуль `src/fis_monitor/services/diagnostics/exclude_policy.py` с классом
`DiagnosticsExcludePolicy`. Создан пакет `services/diagnostics/` (готов принять
`DiagnosticsService` в a4t.7).

Защищаемые поля:

| Тип защиты | Поле |
|------------ |------|
| exclude | `notifications.email.recipients` (Settings tree) |
| exclude | `notifications.email.from_address` (Settings tree) |
| exclude | `lot_user_state.note` (DB row) |
| redact | `cycles.error` — URL / email / Unix-path / Windows-path заменяются `[REDACTED]` |

## Почему так

**SRP**: policy только *определяет* что прятать, не собирает zip — это работа
`DiagnosticsService` (a4t.7). Разделение упрощает тестирование и независимую эволюцию.

**Pure functions**: все три публичных метода (`filter_settings`, `filter_row`,
`redact_error`) возвращают новые dict/str, не мутируют входные данные.

**SSOT через frozensets**: `EXCLUDED_SETTINGS_PATHS`, `EXCLUDED_DB_FIELDS`,
`REDACTED_DB_FIELDS` — единственные места, куда нужно добавлять новые PII-поля.

**Порядок redact-паттернов** (`_REDACT_PATTERNS`): URL → email → Unix-path → Win-path.
URL идут первыми, потому что содержат `/`, которые иначе дважды матчились бы path-RE.
Паттерны компилируются один раз при загрузке модуля.

Соответствует [[decisions-log#ADR-012]] (explicit allow-list + redactor) и
[[decisions-log#ADR-017]] (SecretStr + crash-dump exclusion).

## Связи

- Закрывает: `bd #0t8`
- Разблокирует: `DiagnosticsService` (a4t.7) — готов потреблять policy
- Архитектура: [[architecture]] §10.7 «Diagnostic.zip — explicit allow-list + redactor»
- ADR: [[decisions-log#ADR-012]], [[decisions-log#ADR-017]]
- Данные: [[data-model]] (LotUserDTO.note, CycleResult.error, EmailNotifierConfig.recipients)
- Новые термины: [[glossary#DiagnosticsExcludePolicy]]

## Follow-up

- a4t.7: реализовать `DiagnosticsService`, использующий `DiagnosticsExcludePolicy`
- CI-тест schema-snapshot (`test_diagnostics_schema_no_drift.py`) — упомянут в архитектуре (R4-Minor)
