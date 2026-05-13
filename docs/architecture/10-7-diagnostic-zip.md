# 10.7 Diagnostic.zip — explicit allow-list + redactor

`DiagnosticsService` (`services/diagnostics.py`) собирает диагностический архив. Threat model: пользователь шлёт zip разработчику для разбора инцидента — не должно протечь ничего секретного.

**Allow-list таблиц для экспорта** (всё остальное физически не открывается):
- `lots` — публичные данные.
- `cycles` — техническая телеметрия.
- `notifications(lot_id, channel, sent_at)` — **БЕЗ `recipient`** (это PII).
- `state` — фильтр: только ключи `monitor_paused`, `last_full_scan_at`, `onboarded`, `onboarding_step`; явно исключить любые ключи с подстроками `password|secret|token`.

**`smtp_credentials` физически не открывать** (даже для маскирования) — DB cursor вообще не касается этой таблицы.

**Redactor для логов** (на этапе сборки zip, не runtime):
- regex на: `Cookie:.*`, `Authorization:.*`, `?code=...`, `?state=...` (OAuth-параметры), СНИЛС (`\d{3}-\d{3}-\d{3} \d{2}`), паспорт (`\d{4} \d{6}`), ИНН (`\d{10,12}`), email-адреса в логах.
- Заменять на `<redacted:cookie>`, `<redacted:snils>` и т.д.

**MANIFEST.txt** в zip — список включённых файлов + версия app + commit-hash.

**Schema-snapshot fail-closed (R3-M5).** В коде `DiagnosticsService` живёт константа:
```python
DIAGNOSTIC_SCHEMA_V1 = {
    "lots": frozenset({"id", "cadastral_no", "area_sqm", "region", "municipality",
                       "land_category", "permitted_use", "ogv", "status",
                       "date_create", "date_update", "lat", "lon", "has_boundaries",
                       "parser_version", "first_seen", "last_seen",
                       "detail_fetched_at", "enrichment_status", "enrichment_retries",
                       "last_seen_at", "last_status", "last_status_at",
                       "is_active", "inactive_reason", "inactive_since",
                       "inactive_confirmed_at"}),
    "cycles": frozenset({"id", "region", "started_at", "finished_at", "status",
                         "lots_fetched", "new_lots", "error", "id_schema_check"}),
    "notifications": frozenset({"lot_id", "channel", "sent_at"}),
    # ВНИМАНИЕ: status/attempt_no/last_attempt_at — НЕ в whitelist (могут содержать
    # PII через side-channels). recipient — НЕ в whitelist (это PII по определению).
    "state": frozenset({"key", "value", "updated_at"}),  # фильтрация по ключам в коде
}
```
Перед сборкой zip Diagnostics сравнивает фактическую схему через `PRAGMA table_info(<table>)` со snapshot'ом. Если в реальной таблице **больше** колонок, чем в whitelist (например, после миграции добавили `last_login_ip`), bundle **НЕ собирается** — Diagnostics поднимает `SchemaDriftError("table=<...>, new=<set>, update DIAGNOSTIC_SCHEMA_V1")`. Fail-closed: лучше падающий diagnostic, чем тихая утечка новой колонки. При добавлении колонки разработчик ОБЯЗАН явно обновить DIAGNOSTIC_SCHEMA_V1, оценив PII-риски новой колонки.

**R4-M10 — generic UI message при SchemaDriftError.** Детали (имя таблицы, имя новой колонки) идут только в `app.jsonl`:
```python
try:
    self._validate_schema(DIAGNOSTIC_SCHEMA_V1)
except SchemaDriftError as e:
    logger.error("diagnostic.schema_drift", details=str(e))   # ПОЛНОЕ — в лог
    raise DiagnosticUnavailable(                              # ОБЩЕЕ — в UI
        "Diagnostic export disabled, contact support"
    )
```
Причина: имя колонки в UI может намекать на чувствительные поля (например `last_login_ip`, `payment_token`) — раскрывать через generic UI публично нежелательно. Разработчик получает диагноз через `app.jsonl` (это не PII — это техническая телеметрия).

**R4-Minor — CI-тест schema-snapshot.** Дополнительно к runtime fail (DiagnosticUnavailable) — `tests/integration/test_diagnostics_schema_no_drift.py`: pytest сверяет `DIAGNOSTIC_SCHEMA_V1` с реальной свежесозданной БД (после `schema.sql` + миграций). Drift → CI red. Это ловит drift на этапе review, а не у пользователя в проде.

См. [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]], [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]].
