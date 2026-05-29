# SSE event payloads, SsePayloadSchema, EventSubscription

См. [[web/api-reference]] → SSE Events и `web/sse.py`. Все payload-ы для `text/event-stream` — это HTML-фрагменты; ниже — структура данных, из которой Jinja их рендерит.

## Per-connection view-filter (ADR-052)

`lot.new` events are filtered **per-connection** based on the subscriber's
`view_filters` cookie, captured once at SSE connection time. The predicate
`make_sse_view_filter(vf: ViewFilters)` is injected into `SseStreamer.stream(event_filter=...)`
by the `GET /events` route handler. Events that do not satisfy the predicate are
silently suppressed — never encoded or yielded. Non-`lot.new` events always pass
through. Cookie changes while connected require an F5 (new EventSource connection).

See [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] for full semantics.

### Backfill lots and `is_backfill`

`BackfillService` publishes `SseLotNew` with `is_backfill=True` for every
historically new lot (first-time upsert) during a catalog catch-up run.

**Client behaviour (dr21):**

- htmx's `sse-swap="lot.new"` inserts the card at the **top** of `#feed`
  (afterbegin, normal path).
- The JS layer (`htmx:sseMessage` handler) immediately checks
  `node.dataset.backfill === '1'`.  If set, the node is synchronously
  relocated to just before `#load-more-trigger` (or appended to
  `section.zone`/`#feed` if absent) — **no flash** because the move is
  in the same synchronous callback.
- **Backfill lots are silent**: no sound, no browser notification, no
  escalation timer (`onLotNew` is not called).
- **Counter incremented**: all `.js-lot-count` elements are incremented
  by +1 regardless of live vs. backfill.

**Why event name stays `"lot.new"` (not `"lot.backfill"`):**

Changing the event name would bypass the per-connection view-filter
(ADR-052): `sse-swap="lot.new"` binds only to that event; a separate
`"lot.backfill"` event would require a second `sse-swap` binding and
duplicate filter logic. The `is_backfill` flag inside the existing event
keeps coupling low. See [[decisions/ADR-060-backfill-sse-insertion-and-true-total-counter|ADR-060]].

---

## SSE event payloads

```python
class SseLotNew(BaseModel):
    event: Literal["lot.new"]
    lot: LotPublicDTO
    fragment_template: Literal["poster"]
    is_backfill: bool = False
    # True when published by BackfillService (historical catch-up).
    # False (default) when published by BrowserSseNotifier (live monitor cycle).


class SSELotStatus(BaseModel):
    event: Literal["lot.status"]
    lot_id: int
    new_status: str
    event_type: Literal["gone", "changed"]


class SSEStatusUpdate(BaseModel):
    event: Literal["status"]
    session: Literal["active", "expiring", "expired"]
    next_cycle_at: datetime | None
    monitor_state: Literal["running", "paused", "dnd"]


class SseSessionExpired(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    event: Literal["expired"] = "expired"
    # ЯВНО БЕЗ: redirect_url, stacktrace, exception_repr — PII/token-leak
    # vectors. `redirect_url` исключён из SsePayloadSchema.SESSION_EXPIRED
    # (URL после expire может нести return-токены / CSRF-нонсы).
```

## SsePayloadSchema (R3-C5, [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] ext)

Whitelist полей для persist'а critical-event в таблицу `state` и для логирования force-unsubscribe. Закрывает утечку PII (stacktrace, email-адреса) через `last_critical_event:*` ключи.

```python
class SsePayloadSchema:
    """Whitelist полей по типу события — для persist + redactor-логов.
    Поля ВНЕ списка вырезаются перед записью в state и перед logger.warning."""
    SESSION_EXPIRED = frozenset({"timestamp", "event"})
    CYCLE_ERROR     = frozenset({"timestamp", "cycle_id", "error_category"})
    SMTP_FAILED     = frozenset({"timestamp", "channel_id", "error_category",
                                 "attempt_no"})
    # Явно НЕ включаем: stacktrace, exception_repr, recipient, smtp_response,
    # cookies, tokens, request/response body.

    @classmethod
    def for_event(cls, event_type: str) -> frozenset[str]: ...


class SseCycleError(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    cycle_id: int
    error_category: ErrorCategory
    # ЯВНО БЕЗ: stacktrace, exception_repr, raw error messages — это PII-vector
    # (stacktrace может содержать request body / cookies / SQL с email).


class SseSmtpFailed(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
    timestamp: datetime
    channel_id: str          # e.g. "email"
    error_category: ErrorCategory
    attempt_no: int
    # ЯВНО БЕЗ: recipient, smtp_response, smtp_code, exception_repr.
```

`EventBus.publish(event)` при сохранении critical-события в state делает `payload = {k: v for k, v in event.dict().items() if k in SsePayloadSchema.for_event(event.event_type)}` и сериализует только это. Аналогично — redactor для `logger.warning` при force-unsubscribe.

См. также: [[data-model/errors]] (ErrorCategory), [[architecture/03-protocols]] §3.5, [[architecture/07-concurrency]] §7.3.
