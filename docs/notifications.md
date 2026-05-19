# Уведомления — расширяемая система каналов

> Решения зафиксированы в [[decisions-log]]. Эта заметка — текущая архитектура.

## Браузерные уведомления — активация

**Требуется user gesture.** Browsers enforce Permissions Policy: `Notification.requestPermission()` may only be called from a user-initiated event. Auto-requesting on page load is silently denied.

**Реализация (app.js):** постоянная кнопка-колокольчик `#notif-perm-btn` в шапке (`base.html.jinja`). `initNotifPermButton()` при загрузке читает `Notification.permission` и выставляет `data-notif-perm` + `aria-label`. По клику:
- `default` → вызывает `requestPermission()` (требует user gesture), после ответа обновляет состояние кнопки и показывает toast.
- `granted` → запускает тестовое уведомление «Уведомления работают.»
- `denied` → показывает toast «Разрешите в настройках браузера» (диалог браузера не откроется).

Если `Notification` API недоступен — кнопка скрывается (`btn.hidden = true`).

**Browser Notification API — show-policy contract:** `maybeBrowserNotify()` вызывает `new Notification()` ТОЛЬКО когда `document.hidden === true` (вкладка свёрнута / фоне). На активной вкладке Chrome's show-policy всё равно подавляет фоновые вызовы без user gesture; вместо этого используется звук + in-page jump-pill. Исключение: test-нотификация по клику колокольчика — user gesture, браузер показывает вне зависимости от `document.hidden`.

Root cause: Chrome show-policy — фоновые `Notification()` без user gesture suppress-ятся на активной вкладке (см. [MDN: Notifications API](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API), [Notifications spec](https://notifications.spec.whatwg.org/)).

CSS-состояния:
- `[data-notif-perm="granted"]` — акцентный цвет + `box-shadow: inset 0 -2px 0 var(--accent)` (non-color indicator, WCAG 1.4.1).
- `[aria-disabled="true"]` — muted + `cursor: not-allowed`, но `pointer-events: auto` — кнопка остаётся в tab-order и генерирует click (toast «разрешите в настройках» доступен с клавиатуры).
- Кнопка никогда не выставляет `disabled` — используется `aria-disabled` только для `denied`; для прочих состояний атрибут снимается через `removeAttribute('aria-disabled')` (избегаем ложного «dimmed» в NVDA).

**Toast и screen reader:** контейнер `.toaster` создаётся `ensureToaster()` с `role="status"` и `aria-live="polite"`. Все toast-сообщения (включая «разрешите в настройках браузера») автоматически озвучиваются скринридером без дополнительного `announce()` (WCAG 4.1.3).

**Wiring SSE → уведомление:** htmx-sse extension (ADR-029) публикует `htmx:sseMessage` на `document.body` после каждого sse-swap. Обработчик `lot.new` читает `feed.firstElementChild` (только что вставленный `<article>`) и берёт `data-title` / `data-area` для тела уведомления. Существующая htmx-вставка в `#feed` при этом не затрагивается.

**Контракт шаблона:** `_lot_poster.html.jinja` обязан присутствовать на корневом `<article>`:
- `data-title="<region>[, <district>]"`
- `data-area="<area_ha>"`

Изменения в этих атрибутах требуют синхронного обновления `app.js`. См. [[decisions/ADR-049-browser-notification-activation|ADR-049]].

## Что в MVP

| Канал | Статус | Параметры |
|---|---|---|
| **Браузер** | всегда вкл | — |
| **Email** | в MVP | бот-ящик из дефолта **+** опциональный override SMTP клиентом через панель |
| Telegram | **v2** (после feedback) | через плагин: токен бота + список chat_id |
| ntfy.sh, Discord, Slack, ВК, SMS, Webhook | **v3+** по запросу | плагин-интерфейс готов |

## Email — детальная схема

### Дефолтный путь («ничего не настраиваю»)
- SMTP host/port/user/password — **все четыре** хранятся в `state.db` (таблица `smtp_credentials`, SSOT). Дефолтные значения бот-ящика (`smtp.yandex.ru:587` + login + app-password) записываются при первом запуске установщика / онбординге, **не в `config.json`** (R4-C1, ADR-020).
- В `config.json` остаётся только флаг `use_default_smtp=true` (формальный признак «использовать дефолтный бот-ящик»). Литералы `smtp.yandex.ru:587` хранятся в коде (`infra/smtp/defaults.py`) — fallback на случай пустой таблицы при первом запуске.
- Пользователь в панели вводит **только список получателей** (свои email-адреса)
- Email уходит «From: fis-monitor.alex@yandex.ru», «To: alex@gmail.com»

### Расширенный путь («хочу свой ящик»)
Пользователь в форме разворачивает «Расширенные настройки SMTP», вводит:
- SMTP host
- SMTP port
- SMTP user
- SMTP password (app-password)
- Адрес «От» (опционально)

С этого момента email идёт через его SMTP. Это нужно если пользователь:
- Не доверяет нашему бот-ящику
- Хочет, чтобы письма приходили «от себя» (корпоративная почта)
- Хочет переезд без участия разработчика

## Heartbeat-сводка

Опциональная фича в MVP. В настройках → Уведомления → отдельный чек-бокс:
```
[ ] Присылать ежедневную сводку «всё спокойно»
    Раз в сутки в [09:00] (МСК)
```
По умолчанию **выключено**.

## Архитектура плагинов

### Базовый интерфейс — Protocol, не ABC

**Изменено относительно первой версии**: `Notifier` — `typing.Protocol`, не `abc.ABC`. Общее поведение (retry, logging) — отдельные функции-декораторы (композиция, не наследование). См. [[architecture]] §0.1 и ADR в §11.

```python
from typing import Protocol, ClassVar, Sequence

class Notifier(Protocol):
    channel_id: ClassVar[str]                  # "email", "telegram", ...
    display_name: ClassVar[str]
    description: ClassVar[str]
    config_schema: ClassVar[type[NotifierConfig]]
    recipient_label: ClassVar[str]
    recipient_placeholder: ClassVar[str]

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult: ...
    def test(self, recipient: str) -> NotifyResult: ...

@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    detail: str
    retryable: bool

# Retry — decorator-функция, structurally-compatible с Protocol Notifier.
# В production-graph НЕ используется (см. ниже «Retry в Dispatcher»);
# остаётся как утилита для unit-тестов одиночного notifier-а.
def with_retry(n: Notifier, *, attempts: int, backoff: Sequence[float]) -> Notifier:
    cls = type(n)
    class _Retry:
        channel_id      = cls.channel_id        # ClassVar-forwarding обязателен
        display_name    = cls.display_name
        config_schema   = cls.config_schema
        recipient_label = cls.recipient_label
        def send(self, lot, recipient): ...
        def test(self, recipient): return n.test(recipient)
    return _Retry()
```

**`send_to_all` снят с интерфейса**. Цикл «по получателям + idempotency через `notifications` таблицу» живёт в `NotifierDispatcher` (services/notifier_dispatcher.py). Это правильно: проход по получателям — оркестрация (зависит от Repository), а не ответственность канала.

### Retry-policy — в Dispatcher, не в decorator (N-M4 + ADR-019)

**Решение**: retry в `NotifierDispatcher`, не через `with_retry`-decorator.

Причина: decorator-retry — чисто in-memory, теряет состояние при рестарте процесса. Dispatcher же видит `NotificationsRepository` → может пометить attempt в БД ДО send и проверить journal ПОСЛЕ рестарта → не пошлёт дубль и не сбросит счётчик попыток.

#### State machine отправки (ADR-019)

Состояния `notifications.status`:
- `pending` — слот зарезервирован, попытки идут;
- `sent` — финальный успех (idempotent — повторный `dispatch(lot)` увидит `sent` и пропустит);
- `permanent_fail` — терминальная ошибка (5xx auth, invalid recipient) — больше не пробуем.

Переходы реализуются ТОЛЬКО через `NotificationsRepository` (все внутри `BEGIN IMMEDIATE`):

```sql
-- reserve(lot_id, channel, recipient) → bool (True если создан новый slot)
INSERT OR IGNORE INTO notifications (lot_id, channel, recipient,
                                     status, attempt_no, last_attempt_at, sent_at)
  VALUES (?, ?, ?, 'pending', 0, NULL, NULL);
-- bool := changes() == 1

-- mark_attempt(lot_id, channel, recipient, at) → int (новый attempt_no)
UPDATE notifications
   SET attempt_no = attempt_no + 1, last_attempt_at = ?
 WHERE lot_id = ? AND channel = ? AND recipient = ?
   AND status = 'pending'
RETURNING attempt_no;

-- mark_sent(lot_id, channel, recipient, at)
UPDATE notifications
   SET status = 'sent', sent_at = ?
 WHERE lot_id = ? AND channel = ? AND recipient = ?
   AND status = 'pending';

-- mark_permanent_fail(lot_id, channel, recipient)
UPDATE notifications
   SET status = 'permanent_fail'
 WHERE lot_id = ? AND channel = ? AND recipient = ?
   AND status = 'pending';
```

Все update'ы гайдят `status = 'pending'` — концевые статусы immutable, повторный вызов = no-op.

#### Контракт `NotificationsRepository`

```python
class NotificationsRepository(Protocol):
    """Idempotency + state-machine. PK (lot_id, channel, recipient).
    Все методы — атомарные, внутри BEGIN IMMEDIATE."""
    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool: ...
    # True если новая запись создана, False если уже была (любого status).

    def status_of(self, lot_id: int, channel: str, recipient: str
                  ) -> Literal['pending', 'sent', 'permanent_fail'] | None: ...
    # None — записи нет (reserve ещё не звали).

    def mark_attempt(self, lot_id: int, channel: str, recipient: str,
                     at: datetime) -> int | None: ...
    # R4-C4: возврат `int | None`, не int.
    # Атомарно (BEGIN IMMEDIATE):
    #   UPDATE notifications
    #      SET attempt_no = attempt_no + 1, last_attempt_at = :at
    #    WHERE lot_id=? AND channel=? AND recipient=? AND status='pending'
    #   RETURNING attempt_no;
    # Если запись уже в 'sent' или 'permanent_fail' (race с конкурентным
    # consumer / recovery / cap_reached) — возвращает None, caller ОБЯЗАН
    # пропустить _send_one. НЕ raise: race — это легитимный путь, не баг.
    # (Старая семантика «raise при status != pending» удалена — она ловила
    # бы race каждый раз, что заставляло бы caller всё равно делать try/except.)

    def mark_sent(self, lot_id: int, channel: str, recipient: str,
                  at: datetime) -> None: ...
    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None: ...

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]: ...
    # Recovery после рестарта: «вернуть pending где last_attempt_at < now - age».

    def list_recent(self, limit: int) -> list[NotificationRecord]: ...
```

`already_sent()` deprecated в пользу `status_of(...) == 'sent'`.

#### Consumer-loop с retry и stop_event-aware sleep

```python
class NotifierDispatcher:
    def __init__(self, *, registry, notif_repo, clock, event_bus, stop_event,
                 retry_attempts: int = 3,
                 retry_backoff: Sequence[float] = (2.0, 4.0, 8.0), ...): ...

    def dispatch(self, lot: LotPublicDTO) -> None:
        for notifier in self.registry.enabled():
            for recipient in self._recipients_of(notifier):
                self._send_one(lot, notifier, recipient)

    def _send_one(self, lot, notifier, recipient) -> None:
        ch = notifier.channel_id
        # 1) reserve (idempotent): если status уже 'sent'/'permanent_fail' — выходим.
        status = self.notif_repo.status_of(lot.id, ch, recipient)
        if status in ('sent', 'permanent_fail'):
            return
        if status is None:
            self.notif_repo.reserve(lot.id, ch, recipient)

        # 2) retry-loop. На рестарте: attempt_no сохранён в БД, продолжаем оттуда.
        for _ in range(self.retry_attempts):
            attempt_no = self.notif_repo.mark_attempt(lot.id, ch, recipient,
                                                      at=self.clock.now())
            r = notifier.send(lot, recipient)
            if r.ok:
                self.notif_repo.mark_sent(lot.id, ch, recipient, at=self.clock.now())
                return
            if not r.retryable:
                self.notif_repo.mark_permanent_fail(lot.id, ch, recipient)
                self._publish_smtp_failed(lot, notifier, attempt_no, r)
                return
            # N-M2: stop_event-aware sleep — НЕ time.sleep.
            idx = min(attempt_no - 1, len(self.retry_backoff) - 1)
            delay = self.retry_backoff[idx] + random.uniform(0, 0.5)
            if self.stop_event.wait(delay):
                return  # shutdown — оставляем status='pending', recovery на след. старте
        # Все попытки исчерпаны — НЕ помечаем permanent_fail, оставляем pending
        # для следующего цикла consumer-loop'а либо recovery после рестарта.
        self._publish_smtp_failed(lot, notifier, self.retry_attempts, r)
```

> **Idempotency-guarantor** (R5 review): между tx-ями `status_of()` и `reserve()` существует race-window. Functional impact — нулевой: `INSERT OR IGNORE` в reserve() идемпотентен, `WHERE status='pending'` guard в mark_*() — race-safe (см. R4-C4 `mark_attempt -> int | None`). `status_of()` — fast-path optimization для уже sent/permanent_fail, не критичный путь.

**Recovery после рестарта**: на старте consumer-loop делает
`list_pending_older_than(timedelta(minutes=1))` и переотправляет — `attempt_no` сохранён,
backoff продолжается с правильной позиции.

> **TODO (R5 review — Backend)**: при наличии &gt;10 zombie-pending после крэша consumer_loop последовательно обрабатывает все за O(N × send_timeout) = до 25 минут при N=50. В это время `queue.get()` не вызывается → новые лоты задержаны. **Митигация для post-MVP**: `list_pending_older_than(age, *, limit=10)` + интерлив с `queue.get(timeout=0.1)` между батчами. Для MVP single-user typical N=0-5 — приемлемо.

#### Деление транзакций

`reserve` / `mark_attempt` / `mark_sent` / `mark_permanent_fail` — **каждый в своей короткой tx**.
Между `mark_attempt` и `mark_sent` идёт сетевой `send()` (десятки секунд) — держать tx открытой
всё это время недопустимо (writer-lock блокирует всех). Цена: между `mark_attempt` и `mark_sent`
может случиться рестарт; тогда status остаётся `pending`, attempt_no уже инкрементирован —
recovery подхватит и сделает повторную попытку (idempotency на уровне адресата всё равно
сохраняется через PK + проверку `status_of`).

### Регистрация — explicit registry

```python
# В composition root (production-graph, БЕЗ with_retry — retry в Dispatcher):
registry = ExplicitNotifierRegistry()
registry.register(SmtpEmailNotifier(..., host_policy=smtp_host_policy))
registry.register(BrowserSseNotifier(event_bus=event_bus))
registry.register(HeartbeatNotifier(...))
```

Не используем `@decorator`-регистрацию — она вводит side-effect при импорте модуля и плохо контролируется в Nuitka onefile (см. ADR в [[architecture]] §11).

UI авто-генерируется из метаданных класса (config_schema через Pydantic → JSONSchema → форма).

### Idempotency

Реализована через state-machine `notifications.status` (см. выше «State machine отправки»).
Запись с PK `(lot_id, channel, recipient)` создаётся `reserve()` один раз, дальше переводится
в `sent` либо `permanent_fail`. Повторный `dispatch(lot)` пропускает адресатов со status
`sent`/`permanent_fail` — INSERT OR IGNORE гарантирует, что слот не пересоздаётся.

Это закрывает дубликаты при ретраях, рестартах, race-условиях между producer-event'ами.

### Очередь и приоритеты

Цикл мониторинга **не блокируется** на отправке. Реализация — **sync** (всё приложение sync, см. ADR-005), не asyncio. Уведомления летят в in-memory `queue.Queue` и обрабатываются выделенным supervised thread `NotifierDispatcher.consumer_loop`:

```python
# Producer (MonitorCycleService после upsert нового лота):
#   self.notifier_dispatcher.dispatch(lot)   # = self._queue.put_nowait(lot), fire-forget

# Consumer (выделенный supervised thread, см. architecture.md §4.3.bis):
def consumer_loop(self, stop_event: threading.Event) -> None:
    """Drain in-memory queue + recovery pending из БД (после рестарта)."""
    while not stop_event.is_set():
        # 1) Drain queue новых лотов
        try:
            lot = self._queue.get(timeout=1.0)
        except queue.Empty:
            lot = None
        if lot is not None:
            self.dispatch_all_channels(lot)   # = _send_one для каждого notifier × recipient

        # 2) Recovery: pending старше 1 минуты с момента последней попытки
        #    (R4-C3: ВКЛЮЧАЕТ last_attempt_at IS NULL — zombie-резерваты)
        for pending in self.notif_repo.list_pending_older_than(timedelta(minutes=1)):
            if stop_event.is_set():
                return
            self._retry_one(pending)
```

Контракт `dispatch(lot)` — async-fire-forget: producer не ждёт результата отправки. Если SMTP отвечает 30 секунд — мониторинг это не задерживает (consumer thread обрабатывает отдельно).

### Семантика доставки (R4-C5)

SMTP-доставка — **at-least-once на адресата**, не exactly-once. При крэше процесса между «SMTP 250 OK от сервера» и `mark_sent` COMMIT запись остаётся `status='pending'`, recovery (`list_pending_older_than`) повторит → ВТОРОЕ письмо уйдёт. Окно дубля — секунды (crash-window между ACK MTA и COMMIT БД).

Утверждение «idempotency через PK + status_of» в ADR-019 защищает **только запись в БД** (одна строка `notifications` на адресата). Дубль на стороне MTA получателя — отдельная проблема.

**Митигация — детерминированный Message-ID.** В `SmtpEmailNotifier.build_message()` инвариант:
```
Message-ID: <{lot_id}.{channel_id}.{sha256(recipient)[:16]}@fis-monitor.local>
```
RFC 5322 §3.6.4 + RFC 5321: major MTA (Gmail, Yandex, Mail.ru, Outlook) дедуплицируют по Message-ID — повторное письмо с тем же ID отбрасывается на стороне получателя. `recipient` хешируется (sha256, 16 hex chars), чтобы не светить email в логах MTA-цепочки (Received: headers).

Не блокер для MVP single-user. Документировано в [[ops/runbook]] (сценарий «жалоба на дубль письма»).

### Hard-cap на общее число попыток (R4-M6)

```python
MAX_TOTAL_ATTEMPTS = 10   # После N рестартов с recovery — permanent_fail
```

В `_send_one`:
```python
attempt_no = self.notif_repo.mark_attempt(lot_id, ch, recipient)
if attempt_no is None:
    return   # R4-C4 race — уже в финальном статусе
if attempt_no > MAX_TOTAL_ATTEMPTS:
    self.notif_repo.mark_permanent_fail(lot_id, ch, recipient)
    logger.warning("notification.cap_reached",
                   lot_id=lot_id, channel=ch, attempt_no=attempt_no)
    return
# ... обычная отправка
```

Защита от бесконечного retry при перманентной невозможности отправки (sustained provider outage > 24h, deactivated bot-account). Без cap'а `attempt_no` рос бы без границ при каждом рестарте, забивая лог.

## Пустой rf_subjects vs subscribed_at

**Семантика пустого `filters.rf_subjects` (ADR-035 I4):** пустой список означает «без региональных
ограничений» — `RfSubjectFilterMatcher.matches` возвращает `True` для любого лота. Это явный
инвариант «notify-all», а не дефект.

**Subscribed_at остаётся активным при пустом rf_subjects (ADR-039, by design).**
`SubscribedAtFilteredNotifier` — отдельный decorator-слой в цепочке
`RfSubjectFilteredEmailNotifier → SubscribedAtFilteredNotifier → SmtpEmailNotifier`.
Когда RF-декоратор пропускает лот (в том числе при пустом rf_subjects), управление передаётся
inner-декоратору `SubscribedAtFilteredNotifier`, который применяет cutoff по `subscribed_at`.

Пример цепочки при пустом rf_subjects:

```
Лот (region=Хабаровский край, date_create=2026-01-10)
   ↓ RfSubjectFilteredEmailNotifier: rf_subjects=[] → pass-through (I4)
   ↓ SubscribedAtFilteredNotifier: subscribed_at=2026-02-01 → SUPPRESS (date_create < subscribed_at)
   ✗ SmtpEmailNotifier: не вызван
```

```
Лот (region=Хабаровский край, date_create=2026-02-15)
   ↓ RfSubjectFilteredEmailNotifier: rf_subjects=[] → pass-through (I4)
   ↓ SubscribedAtFilteredNotifier: subscribed_at=2026-02-01 → pass (date_create >= subscribed_at)
   ✓ SmtpEmailNotifier: письмо отправлено
```

Это предотвращает спам при онбординге нового региона: даже без региональных ограничений
исторические лоты (до момента подписки) не рассылаются.

Покрыто тестами `test_rf_empty_with_subscribed_at_passes_new_lots` и
`test_rf_empty_with_subscribed_at_drops_old_lots` в
`tests/unit/services/test_notifier_dispatcher.py`.

См. [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I4 и
[[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]].

## API-эндпоинты

Каналами управляют:

- `GET /api/notifiers` — список каналов с metadata и (маскированным) конфигом
- `PUT /api/notifiers/{channel}` — обновить конфиг канала
- `POST /api/notifiers/{channel}/test` — отправить тестовое сообщение
- `POST /api/notifiers/{channel}/discover` — v2, авто-обнаружение получателей (Telegram chat_id через `getUpdates`)

Детали request/response — в [[web/api-reference]].

## Хранение секретов
- **SMTP логин и пароль хранятся в `state.db`** (таблица user-state `smtp_credentials`), **не в `config.json`** (см. [[decisions-log]])
- Pydantic-схема `config.json` не содержит полей `smtp_user` / `smtp_password`
- ПК пользователя — доверенная среда: файловый ACL на `{data_dir}` (Windows `%LOCALAPPDATA%\fis-monitor\`, Linux `~/.local/share/fis-monitor/`) достаточен для нашей threat model ([[decisions-log]] → Security & operations)
- В API-ответах все пароли/токены маскируются `***`
- В UI пустое поле = «не менять текущее значение»

См. также: [[web/ui-architecture]], [[decisions-log]], [[product/mvp-scope]], [[architecture]].
