# ADR-012: Diagnostic.zip — explicit allow-list + redactor

**Context.** Пользователь шлёт diagnostic.zip разработчику. Не должно протечь ничего секретного.

**Decision.**
- **Allow-list таблиц для экспорта**: `lots`, `cycles`, `notifications(lot_id, channel, sent_at)` (БЕЗ recipient). Таблица `smtp_credentials` физически не открывается (DB cursor не касается).
- **Redactor для логов** на этапе сборки zip (regex на Cookie/Authorization/`?code=`/`?state=`, СНИЛС/паспорт/ИНН/email).
- **MANIFEST.txt** в zip — список включённого + app-version.

**Consequences.** Безопасный экспорт. Цена — отдельный `DiagnosticsService` (~150 строк) + конфигурируемые redactor-regex.

**Расширение второго раунда (audit.jsonl isolation).** Полные значения config-diff (включая `smtp.host`, `recipients[]`, `interval_minutes`) пишутся ТОЛЬКО в append-only `audit.jsonl` в `data_dir/`. Этот файл **физически исключён** из DiagnosticsService allow-list (наряду с `smtp_credentials`). В `app.jsonl` идут только счётчики и булы (см. [[architecture/10-7-diagnostic-zip]] и [[architecture/09-test-strategy]] раздел 7.6, N-M5). Так PII не утекает в диагностический архив, отправляемый разработчику.

См. также: [[decisions-log]], [[architecture/10-7-diagnostic-zip]].
