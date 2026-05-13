# SSE event payloads, SsePayloadSchema, EventSubscription

См. [[web/api-reference]] → SSE Events и `web/sse.py`. Все payload-ы для `text/event-stream` — это HTML-фрагменты; ниже — структура данных, из которой Jinja их рендерит.

## SSE event payloads

```python
class SSELotNew(BaseModel):
    event: Literal["lot.new"]
    lot: LotDTO
    fragment_template: Literal["poster", "list"]


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
