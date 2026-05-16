# ADR-019: Notification state machine — reserve → attempt → sent | permanent_fail

**Context.** `notifications.md` декларировал `Dispatcher.mark_attempt(...)` (write-ahead перед `send`), но в `schema.sql` таблица `notifications` имела PK `(lot_id, channel, recipient)` + `sent_at NOT NULL DEFAULT CURRENT_TIMESTAMP`. Не было колонки `attempt_no`, не было pending-состояния. Контракт `NotificationsRepository.mark_attempt(..., attempt_no: int)` без поля в БД = silent no-op либо runtime-ошибка. На рестарте Dispatcher не знал, продолжать ли retry или начинать с нуля.

**Decision.** Notifications — state machine с тремя состояниями (`pending`, `sent`, `permanent_fail`) и одним PK `(lot_id, channel, recipient)` (одна запись на адресата — idempotency сохраняется).

Изменения схемы (`schema.sql`):
- `status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (...))` — состояние.
- `attempt_no INTEGER NOT NULL DEFAULT 0` — счётчик попыток, durable.
- `last_attempt_at TIMESTAMP` — nullable до первой попытки.
- `sent_at TIMESTAMP` — стал nullable (NULL пока status != 'sent').
- Partial-индекс `idx_notifications_pending` на `last_attempt_at WHERE status='pending'` — для recovery после рестарта.

Контракт `NotificationsRepository` (см. [[notifications]]):
- `reserve(lot_id, channel, recipient) -> bool` — INSERT OR IGNORE + status='pending', attempt_no=0.
- `mark_attempt(lot_id, channel, recipient, at) -> int` — UPDATE attempt_no=attempt_no+1, last_attempt_at=at WHERE status='pending', RETURNING attempt_no.
- `mark_sent(lot_id, channel, recipient, at)` — UPDATE status='sent', sent_at=at WHERE status='pending'.
- `mark_permanent_fail(lot_id, channel, recipient)` — UPDATE status='permanent_fail' WHERE status='pending'.
- `status_of(...)` — для skip уже отправленных.
- `list_pending_older_than(age)` — recovery на старте consumer-loop.

Каждый метод — короткая отдельная tx (BEGIN IMMEDIATE). Сетевой `send()` идёт **между** `mark_attempt` и `mark_sent` — открытую writer-tx на десятки секунд держать недопустимо. Цена: между mark_attempt и mark_sent возможен рестарт; recovery подхватит status='pending' и повторит — attempt_no уже инкрементирован, idempotency на адресата сохраняется (PK + проверка status_of).

**Consequences.** Полный durable state machine. Рестарт во время retry — продолжается с того же attempt_no. Идемпотентность на адресата гарантируется PK + проверкой `status_of` перед reserve. Backoff корректно работает поверх рестартов. Цена: 3 новых колонки в `notifications` (~ничего по размеру). `already_sent()` deprecated в пользу `status_of() == 'sent'`.

**Расширение R4-C5 (at-least-once семантика + Message-ID дедупликация).** PK + `status_of()` защищают **только запись в БД** (idempotency на уровне нашей таблицы). Они НЕ защищают от дубликата на стороне MTA: при крэше процесса между «SMTP 250 OK от сервера» и `mark_sent` COMMIT запись остаётся `status='pending'`, recovery (`list_pending_older_than`) повторит → второе письмо уйдёт. Окно дубля — секунды (crash-window между ACK и COMMIT). Утверждать «exactly-once» некорректно — это **at-least-once на адресата**.

Митигация: детерминированный `Message-ID: <{lot_id}.{channel}.{sha256(recipient)[:16]}@fis-monitor.local>` (RFC 5322 §3.6.4). Major MTA (Gmail, Yandex, Mail.ru, Outlook) дедуплицируют по Message-ID на стороне получателя — повторное письмо с тем же ID отбрасывается. `recipient` хешируется чтобы не светить email в логах MTA-цепочки (Received-headers могут публично попасть в bounces).

Не блокер для MVP single-user. Известно и документировано в runbook (см. сценарий 11 / при жалобе на дубль).

**Расширение R4-C3 (recovery zombie-резерватов с `last_attempt_at IS NULL`).** `list_pending_older_than(age)` ВКЛЮЧАЕТ записи где `last_attempt_at IS NULL` (zombie — created `reserve()` но процесс крэшнулся до первого `mark_attempt`). SQL:
```sql
SELECT ... FROM notifications
 WHERE status='pending'
   AND (last_attempt_at IS NULL OR last_attempt_at < :cutoff);
```
Без `OR ... IS NULL` zombie-резерват вечно болтался бы pending, не виден recovery. Индекс `idx_notifications_pending` хранит NULL last_attempt_at (partial WHERE status='pending') — обе ветки SQL'я индексны.

**Расширение R4-C4 (`mark_attempt -> int | None` race).** `mark_attempt(lot_id, channel, recipient)` возвращает `int | None`. None — если запись уже `sent` либо `permanent_fail` (race с конкурентным consumer / recovery / cap_reached в R4-M6). Caller (`_send_one`) обязан пропустить отправку:
```python
attempt_no = self.notif_repo.mark_attempt(lot_id, ch, recipient)
if attempt_no is None:
    return  # race — уже в финальном статусе
```
Без этого race на reserve → mark_attempt → permanent_fail (например, конкурентный cap_reached) бы приводил к UnboundLocalError либо raise. Race — легитимный путь, не баг.

**Расширение R4-M6 (hard-cap на общее число попыток).** `MAX_TOTAL_ATTEMPTS = 10`. После N рестартов с recovery (`attempt_no > MAX_TOTAL_ATTEMPTS`) — `mark_permanent_fail`, лог `notification.cap_reached`. Защита от бесконечного retry при перманентной невозможности отправки (e.g. provider sustained outage 24h+).

**Расширение R4-M8 (migration v1→v2 для notifications + smtp_credentials).** `PRAGMA user_version` bumped 1→2. MigrationRunner v1→v2 — следующий SQL (запускается в одной BEGIN IMMEDIATE tx):

> **FIXME (R5 review)**: SQL ниже физически невыполним в SQLite — `ALTER TABLE` не может ослабить `NOT NULL` constraint на `sent_at` без rebuild table. Greenfield MVP создаёт БД сразу с `user_version=2`, поэтому migration v1→v2 в проде не выполнится — runtime impact нулевой. При реальной миграции (v2→v3 в будущем) переписать через 12-step rebuild pattern из SQLite docs: CREATE TABLE _new + INSERT SELECT + DROP old + RENAME.

```sql
BEGIN IMMEDIATE;

-- notifications: state machine (ADR-019)
ALTER TABLE notifications ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'
    CHECK (status IN ('pending','sent','permanent_fail'));
ALTER TABLE notifications ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1;
ALTER TABLE notifications ADD COLUMN last_attempt_at TIMESTAMP;
-- Существующие строки v1 имели sent_at NOT NULL — это всё «успешные» отправки,
-- DEFAULT 'sent' их так и помечает. last_attempt_at нет в v1 → ставим sent_at:
UPDATE notifications SET last_attempt_at = sent_at WHERE status='sent';
-- sent_at в v2 nullable (для future pending records) — старые данные не меняются.

-- smtp_credentials: SSOT host/port (ADR-020)
ALTER TABLE smtp_credentials ADD COLUMN smtp_host TEXT NOT NULL DEFAULT 'smtp.yandex.ru';
ALTER TABLE smtp_credentials ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587
    CHECK (smtp_port BETWEEN 1 AND 65535);
-- Defaults — литералы бот-ящика. После migration MigrationRunner может опционально
-- ALTER COLUMN убрать DEFAULT (SQLite не поддерживает DROP DEFAULT на ALTER —
-- придётся через rebuild table; в greenfield MVP это no-op).

-- Indexes (R4-M9 + R4-C3)
DROP INDEX IF EXISTS idx_notifications_sent_at;   -- старая версия без partial
CREATE INDEX IF NOT EXISTS idx_notifications_sent_at
    ON notifications(sent_at DESC) WHERE status='sent';
CREATE INDEX IF NOT EXISTS idx_notifications_pending
    ON notifications(last_attempt_at) WHERE status='pending';

PRAGMA user_version = 2;
COMMIT;
```

Greenfield MVP не имеет prod-баз с v1 — реальные пользователи получат сразу v2 при первом запуске installer'а через `schema.sql`. MigrationRunner есть для совместимости с unit-тестами и dev-данными, и для будущих v2→v3.

**Расширение 2026-05-15: Intentional dispatch suppression (ADR-039).** at-least-once SLO определён над notifications, которые система **решает** отправить — не над universe of lots в регионе. Фильтрация по `subscribed_at` (ADR-039) применяется per-channel через `SubscribedAtFilteredNotifier` (decorator над email notifier): если `lot.date_create < region.subscribed_at`, email `send()` возвращает suppressed result без вызова `reserve()` / `mark_attempt()`. Browser channel (`BrowserSseNotifier`) не оборачивается фильтром — он получает все лоты. Это намеренная per-channel suppression, не нарушение SLO. Idempotency-гарантии PK + `status_of()` остаются неизменными для всего, что прошло в dispatch.

См. также: [[decisions-log]], [[notifications]], [[data-model/notifications]], [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]].
