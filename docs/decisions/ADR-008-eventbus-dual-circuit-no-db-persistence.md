# ADR-008: EventBus — двухконтурный (normal/critical), без persistence в БД

**Context.** SSE-события могут теряться при медленном подписчике. Какие можно дропать, какие — нет?

**Decision.** Два метода: `publish_normal` (drop-from-tail при maxsize=100, для `lot.new` и UI-уведомлений) и `publish_critical` (blocking `put(timeout=2.0)`, для `session.expired`, `cycle.error`, `smtp.failed`). При timeout критичного — force-unsubscribe slow consumer. **Persistence событий в БД — НЕТ** (БД содержит lot/notification как source of truth, F5 восстановит).

**Consequences.** Простая memory-only модель. UX-события могут быть пропущены вкладкой — это OK. Критичные гарантированно доставлены либо подписчик отвалится (что и хотим).

**Расширение R3-C5 (per-type slots + payload whitelist).** Persist last-critical event делится на **per-type ключи** в таблице `state`: `last_critical_event:session`, `last_critical_event:cycle`, `last_critical_event:smtp` (TTL 1ч каждый). Single-slot терял пачку (session.expired в 10:00, cycle.error в 10:30 — пользователь при reconnect видел только cycle.error). Persist'имые поля — фильтр через `SsePayloadSchema` (whitelist по типу): для `cycle.error` — `{timestamp, cycle_id, error_category}` БЕЗ stacktrace/exception_repr; для `smtp.failed` — `{timestamp, channel_id, error_category, attempt_no}` БЕЗ recipient/smtp_response. `logger.warning` при force-unsubscribe тоже редактируется по тому же whitelist. Закрывает утечку PII через `last_critical_event:*` (stacktrace в state — это слой PII при экспорте/диагностике, хотя `audit.jsonl` уже исключён — defence-in-depth).

См. также: [[decisions-log]], [[architecture/03-protocols]] §3.5, [[data-model/sse]].
