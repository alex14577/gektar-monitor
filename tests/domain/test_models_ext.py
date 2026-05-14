"""Extension DTO tests — Settings / OnboardingState / CycleResult / Notification
state machine / NotifyResult / LoginOutcome / SessionStatus / SSE payloads /
HttpResponse / LockHandle / Subscriptions / SseEvent union.

Canon source: `docs/data-model.md`. Where canon is silent (ParsedListRow /
ParsedDetail / NotifierConfig / Subscriptions), shapes follow
`docs/architecture.md` §3.2-§3.5.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from fis_monitor.domain import (
    BrowserConfig,
    CatchupConfig,
    ConfigSubscription,
    CycleResult,
    DndConfig,
    EmailConfig,
    EventSubscription,
    FiltersConfig,
    HeartbeatConfig,
    HttpResponse,
    LockHandle,
    LoginOutcome,
    LotPublicDTO,
    LotUserState,
    MonitoringConfig,
    NotificationRecord,
    NotificationsConfig,
    NotifierConfig,
    NotifyResult,
    OnboardingState,
    ParsedDetail,
    ParsedListRow,
    SessionStatus,
    Settings,
    SoundEscalationConfig,
    SseCycleError,
    SseEvent,
    SseLotNew,
    SseLotStatus,
    SseSessionExpired,
    SseSmtpFailed,
    UIConfig,
)


# ---------------------------------------------------------------------------
# Settings — full config tree (data-model.md §Settings)
# ---------------------------------------------------------------------------
def test_settings_defaults_canon():
    """Defaults match data-model.md canon (mode=local, MSK, regions=[1,2])."""
    s = Settings()
    assert s.mode == "local"
    assert s.interval_minutes == 15
    assert s.timezone == "Europe/Moscow"
    assert s.regions == [1, 2]
    assert isinstance(s.filters, FiltersConfig)
    assert isinstance(s.ui, UIConfig)
    assert isinstance(s.monitoring, MonitoringConfig)
    assert isinstance(s.notifications, NotificationsConfig)


def test_settings_frozen():
    s = Settings()
    with pytest.raises(ValidationError):
        s.mode = "server"  # type: ignore[misc]


def test_settings_extra_forbid():
    with pytest.raises(ValidationError):
        Settings(unknown_root="boom")  # type: ignore[call-arg]


def test_settings_mode_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        Settings(mode="hosted")  # type: ignore[arg-type]


def test_settings_interval_minutes_bounds():
    Settings(interval_minutes=0)   # 0 = непрерывно (canon)
    Settings(interval_minutes=60)
    with pytest.raises(ValidationError):
        Settings(interval_minutes=-1)
    with pytest.raises(ValidationError):
        Settings(interval_minutes=61)


def test_ui_config_port_bounds_and_literals():
    UIConfig()
    with pytest.raises(ValidationError):
        UIConfig(port=80)            # < 1024
    with pytest.raises(ValidationError):
        UIConfig(font_size_px=20)    # not in {14,16,18}
    with pytest.raises(ValidationError):
        UIConfig(theme="solarized")  # not in literal


def test_email_config_smtp_port_bounds():
    EmailConfig()
    with pytest.raises(ValidationError):
        EmailConfig(smtp_port=0)
    with pytest.raises(ValidationError):
        EmailConfig(smtp_port=70000)


def test_email_config_recipients_emailstr_validated():
    with pytest.raises(ValidationError):
        EmailConfig(recipients=["not-an-email"])


def test_browser_config_default_enabled():
    assert BrowserConfig().enabled is True


def test_heartbeat_config_default_off():
    hb = HeartbeatConfig()
    assert hb.enabled is False
    assert hb.time == "09:00"


def test_sound_escalation_default_seconds():
    se = SoundEscalationConfig()
    assert se.escalate_at_seconds == [60, 120]


def test_dnd_default_off():
    assert DndConfig().until is None


def test_catchup_defaults():
    c = CatchupConfig()
    assert c.enabled is True
    assert c.min_offline_minutes == 60


def test_monitoring_defaults():
    m = MonitoringConfig()
    assert m.full_scan_time == "04:00"
    assert m.full_scan_l2_priority_days == 7


def test_filters_default_empty_rf_subjects():
    assert FiltersConfig().rf_subjects == []


# ---------------------------------------------------------------------------
# LotUserState (data-model.md §LotUserState)
# ---------------------------------------------------------------------------
def test_lot_user_state_defaults():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    s = LotUserState(lot_id=1, updated_at=now)
    assert s.starred is False
    assert s.submitted is False
    assert s.submitted_at is None
    assert s.note is None
    assert s.seen_at is None


def test_lot_user_state_frozen_and_extra_forbid():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    s = LotUserState(lot_id=1, updated_at=now)
    with pytest.raises(ValidationError):
        s.starred = True  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LotUserState(lot_id=1, updated_at=now, unknown=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# OnboardingState — Enum FSM (5 states)
# ---------------------------------------------------------------------------
def test_onboarding_state_values():
    assert OnboardingState.NOT_STARTED.value == "not_started"
    assert OnboardingState.REGIONS_SET.value == "regions_set"
    assert OnboardingState.SMTP_CONFIGURED.value == "smtp_configured"
    assert OnboardingState.RECIPIENTS_SET.value == "recipients_set"
    assert OnboardingState.COMPLETED.value == "completed"
    assert len(list(OnboardingState)) == 5


def test_onboarding_state_is_str_enum():
    """Must inherit `str` so JSON-serializable as plain string (canon)."""
    assert isinstance(OnboardingState.COMPLETED, str)
    assert OnboardingState("completed") is OnboardingState.COMPLETED


# ---------------------------------------------------------------------------
# CycleResult (data-model.md §CycleResult)
# ---------------------------------------------------------------------------
def _cycle_kwargs(**over):
    now = datetime(2026, 5, 13, tzinfo=UTC)
    base = dict(
        id=1,
        region=1,
        started_at=now,
        finished_at=now,
        status="ok",
        lots_fetched=10,
        new_lots=2,
        error=None,
    )
    base.update(over)
    return base


def test_cycle_result_canon_fields():
    cr = CycleResult(**_cycle_kwargs())
    assert cr.id_schema_check == "ok"   # default
    assert cr.error is None


def test_cycle_result_status_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        CycleResult(**_cycle_kwargs(status="success"))   # type: ignore[arg-type]


def test_cycle_result_id_schema_check_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        CycleResult(**_cycle_kwargs(id_schema_check="weird"))   # type: ignore[arg-type]


def test_cycle_result_frozen_and_extra_forbid():
    cr = CycleResult(**_cycle_kwargs())
    with pytest.raises(ValidationError):
        cr.new_lots = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CycleResult(**_cycle_kwargs(unknown="x"))


def test_cycle_result_error_max_length_200():
    """CycleResult.error is log-only; cap at 200 chars to keep `cycles` table
    rows bounded and to defend against accidental stacktrace dumps."""
    # 200 chars accepted
    CycleResult(**_cycle_kwargs(error="x" * 200))
    # 201 rejected
    with pytest.raises(ValidationError):
        CycleResult(**_cycle_kwargs(error="x" * 201))


# ---------------------------------------------------------------------------
# NotificationRecord — ADR-019 state machine extension of canon
# ---------------------------------------------------------------------------
def _notif_kwargs(**over):
    base = dict(
        lot_id=42,
        channel="email",
        recipient="alex@example.com",
        status="pending",
        attempt_no=0,
        last_attempt_at=None,
        sent_at=None,
    )
    base.update(over)
    return base


def test_notification_record_pending_defaults():
    n = NotificationRecord(**_notif_kwargs())
    assert n.status == "pending"
    assert n.sent_at is None
    assert n.last_attempt_at is None


def test_notification_record_status_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        NotificationRecord(**_notif_kwargs(status="failed"))  # type: ignore[arg-type]


def test_notification_record_channel_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        NotificationRecord(**_notif_kwargs(channel="sms"))  # type: ignore[arg-type]


def test_notification_record_attempt_no_strict_int():
    """attempt_no rejects string coercion (StrictInt)."""
    with pytest.raises(ValidationError):
        NotificationRecord(**_notif_kwargs(attempt_no="3"))  # type: ignore[arg-type]


def test_notification_record_frozen_extra_forbid():
    n = NotificationRecord(**_notif_kwargs())
    with pytest.raises(ValidationError):
        n.status = "sent"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        NotificationRecord(**_notif_kwargs(message_id="leak"))   # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# NotifyResult — frozen dataclass (architecture.md §3.3)
# ---------------------------------------------------------------------------
def test_notify_result_frozen():
    r = NotifyResult(ok=True, detail="sent", retryable=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_notify_result_fields():
    f = {f.name for f in dataclasses.fields(NotifyResult)}
    assert f == {"ok", "detail", "retryable"}


def test_notify_result_detail_max_length_500():
    """NotifyResult.detail is log-only; cap at 500 chars.  Defence-in-depth
    so a misbehaving notifier cannot blow up `app.jsonl` rows or smuggle a
    multi-KB MTA response onto the bus by mistake (extra=forbid on
    SseSmtpFailed already forbids the field, but bound the SOURCE too)."""
    # 500 chars accepted
    NotifyResult(ok=False, detail="x" * 500, retryable=True)
    # 501 rejected
    with pytest.raises(ValidationError):
        NotifyResult(ok=False, detail="x" * 501, retryable=True)


# ---------------------------------------------------------------------------
# LoginOutcome — frozen dataclass (architecture.md §3.4)
# ---------------------------------------------------------------------------
def test_login_outcome_frozen_and_fields():
    out = LoginOutcome(success=True, cookies_updated=True, error=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.success = False  # type: ignore[misc]
    assert {f.name for f in dataclasses.fields(LoginOutcome)} == {
        "success", "cookies_updated", "error",
    }


def test_login_outcome_error_literal_closed_set():
    """LoginOutcome.error is tightened to a closed Literal — no free-form
    `playwright:<reason>` strings are allowed (PII vector via raw reason)."""
    # All canon values must be accepted.
    for err in (
        "timeout",
        "cancelled",
        "playwright_disconnect",
        "playwright_timeout",
        "playwright_other",
        None,
    ):
        LoginOutcome(success=False, cookies_updated=False, error=err)  # type: ignore[arg-type]

    # The error type annotation must be the closed Literal | None
    # — guard against accidental widening back to `str | None`.  Resolve
    # the forward-ref string (PEP 563 / `from __future__ import annotations`)
    # via `typing.get_type_hints` so we can inspect the actual union members.
    import typing

    hints = typing.get_type_hints(LoginOutcome)
    error_hint = hints["error"]
    # `error_hint` is `Literal[...] | None` — flatten one level:
    # union args == (Literal[...], NoneType); the Literal members live one
    # level deeper.
    union_args = get_args(error_hint)
    assert type(None) in union_args
    literal_arg = next(a for a in union_args if a is not type(None))
    members = set(get_args(literal_arg))
    assert members == {
        "timeout",
        "cancelled",
        "playwright_disconnect",
        "playwright_timeout",
        "playwright_other",
    }
    # The unresolved-source annotation must also not contain the legacy
    # `playwright:<...>` open-form marker.
    error_field = next(f for f in dataclasses.fields(LoginOutcome) if f.name == "error")
    raw = error_field.type if isinstance(error_field.type, str) else str(error_field.type)
    assert "playwright:<" not in raw
    assert "str | None" not in raw  # not widened back


# ---------------------------------------------------------------------------
# SessionStatus — Enum (str)
# ---------------------------------------------------------------------------
def test_session_status_values():
    assert SessionStatus.ACTIVE.value == "active"
    assert SessionStatus.EXPIRING.value == "expiring"
    assert SessionStatus.EXPIRED.value == "expired"
    assert len(list(SessionStatus)) == 3


# ---------------------------------------------------------------------------
# SSE event DTOs — SessionExpired / LotNew / LotStatus
# ---------------------------------------------------------------------------
def test_sse_session_expired_priority_critical():
    assert SseSessionExpired.priority == "critical"
    now = datetime(2026, 5, 13, tzinfo=UTC)
    evt = SseSessionExpired(timestamp=now, event="session.expired")
    with pytest.raises(ValidationError):
        evt.event = "other"  # type: ignore[misc]


def test_sse_session_expired_event_literal_rejects_unknown():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    with pytest.raises(ValidationError):
        SseSessionExpired(timestamp=now, event="signed_out")  # type: ignore[arg-type]


def test_sse_session_expired_extra_forbid_blocks_pii():
    """`message`, `stacktrace`, `redirect_url` etc. — must be rejected."""
    now = datetime(2026, 5, 13, tzinfo=UTC)
    with pytest.raises(ValidationError):
        SseSessionExpired(timestamp=now, event="session.expired", message="leak")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        # `redirect_url` removed from whitelist + extra=forbid blocks at type level
        SseSessionExpired(timestamp=now, event="session.expired", redirect_url="/login?t=x")  # type: ignore[call-arg]


def test_sse_session_expired_has_timestamp_field():
    """T_SE_2: SseSessionExpired carries `timestamp` (like SseCycleError /
    SseSmtpFailed), so EventBus persist + whitelist can pin event ordering."""
    assert set(SseSessionExpired.model_fields) == {"timestamp", "event"}
    now = datetime(2026, 5, 13, tzinfo=UTC)
    evt = SseSessionExpired(timestamp=now, event="session.expired")
    assert evt.timestamp == now
    assert evt.event == "session.expired"


def test_sse_lot_new_priority_normal_and_carries_public_dto(make_lot):
    assert SseLotNew.priority == "normal"
    base = make_lot()
    public = LotPublicDTO(
        **base.model_dump(),
        age_seconds=10,
        tier="match",
        freshness="hot",
    )
    evt = SseLotNew(event="lot.new", lot=public, fragment_template="poster")
    assert evt.lot is public
    # `raw_json` does NOT cross the bus (inherited from LotPublicDTO serializer)
    dumped = evt.model_dump()
    assert "raw_json" not in dumped["lot"]


def test_sse_lot_new_fragment_template_literal_rejects_unknown(make_lot):
    base = make_lot()
    public = LotPublicDTO(
        **base.model_dump(),
        age_seconds=10,
        tier="match",
        freshness="hot",
    )
    with pytest.raises(ValidationError):
        SseLotNew(event="lot.new", lot=public, fragment_template="card")   # type: ignore[arg-type]


def test_sse_lot_status_canon():
    evt = SseLotStatus(
        event="lot.status",
        lot_id=1,
        new_status="Свободен",
        event_type="changed",
    )
    assert SseLotStatus.priority == "normal"
    with pytest.raises(ValidationError):
        evt.lot_id = 2  # type: ignore[misc]


def test_sse_lot_status_event_type_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        SseLotStatus(
            event="lot.status",
            lot_id=1,
            new_status="X",
            event_type="weird",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# SseEvent — TypeAlias union (covers all 5 events)
# ---------------------------------------------------------------------------
def test_sse_event_union_members():
    # PEP 695 `type X = ...` produces a lazy `TypeAliasType`; unwrap via
    # `__value__` to inspect the underlying union.
    members = set(get_args(SseEvent.__value__))
    assert members == {
        SseCycleError,
        SseSmtpFailed,
        SseSessionExpired,
        SseLotNew,
        SseLotStatus,
    }


# ---------------------------------------------------------------------------
# HttpResponse — frozen dataclass (architecture.md §3.2)
# ---------------------------------------------------------------------------
def test_http_response_frozen():
    r = HttpResponse(status=200, text="<html/>", headers={}, final_url="https://x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = 500  # type: ignore[misc]
    assert {f.name for f in dataclasses.fields(HttpResponse)} == {
        "status", "text", "headers", "final_url",
    }


# ---------------------------------------------------------------------------
# LockHandle — frozen dataclass
# ---------------------------------------------------------------------------
def test_lock_handle_frozen():
    h = LockHandle(fd=42, pid=12345, path="/tmp/fis.lock")
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.pid = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Subscriptions — Protocols with context-manager + unsubscribe
# ---------------------------------------------------------------------------
def test_event_subscription_is_protocol_with_context_manager():
    """Structural protocol: any object with __enter__/__exit__/unsubscribe/iter qualifies."""

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def unsubscribe(self) -> None:
            pass

        def iter(self):
            return iter(())

    f = _Fake()
    # Structural conformance via type annotation (not runtime_checkable)
    _: EventSubscription[object] = f


def test_event_subscription_iter_yields_events():
    """EventSubscription[T] exposes a lazy iterator over received events
    (Protocol invariant: non-blocking generator)."""
    sentinel = ("a", "b", "c")

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def unsubscribe(self) -> None:
            pass

        def iter(self):
            yield from sentinel

    f = _Fake()
    _2: EventSubscription[object] = f
    with f as sub:
        collected = tuple(sub.iter())
    assert collected == sentinel


def test_event_subscription_is_generic_pep695():
    """EventSubscription is parameterised generic — `EventSubscription[SseEvent]`
    must be a valid runtime form (Protocol[T] via PEP 695 syntax)."""
    # Subscripting must not raise; runtime erasure still yields the protocol.
    parameterised = EventSubscription[SseEvent]
    assert parameterised is not None


def test_config_subscription_is_protocol_with_context_manager():
    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def unsubscribe(self) -> None:
            pass

    # Structural conformance via type annotation (not runtime_checkable)
    _: ConfigSubscription = _Fake()


# ---------------------------------------------------------------------------
# NotifierConfig — base BaseModel (frozen + extra=forbid)
# ---------------------------------------------------------------------------
def test_notifier_config_base_frozen_extra_forbid():
    cfg = NotifierConfig()
    with pytest.raises(ValidationError):
        NotifierConfig(unknown=True)   # type: ignore[call-arg]
    # base has no fields — model_dump returns empty dict
    assert cfg.model_dump() == {}


# ---------------------------------------------------------------------------
# ParsedListRow / ParsedDetail — minimal canon-aligned shapes
# ---------------------------------------------------------------------------
def test_parsed_list_row_frozen_extra_forbid():
    """Minimal list-row shape — parser invariant: None, never ''."""
    row = ParsedListRow(
        id=12345,
        cadastral_no="27:23:0040000:1234",
        area_sqm=None,
        region="Хабаровский край",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="Свободен",
        date_create=datetime(2026, 5, 13, tzinfo=UTC),
        date_update=None,
    )
    with pytest.raises(ValidationError):
        row.status = "X"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ParsedListRow(  # type: ignore[call-arg]
            id=1,
            cadastral_no="x",
            area_sqm=None,
            region="r",
            municipality=None,
            land_category=None,
            permitted_use=None,
            ogv=None,
            status="s",
            date_create=datetime(2026, 5, 13, tzinfo=UTC),
            date_update=None,
            extra="boom",
        )


def test_parsed_detail_frozen_extra_forbid():
    """Detail-card extra fields land in raw_json — no free-form fields here."""
    d = ParsedDetail(
        lat=48.48,
        lon=135.08,
        has_boundaries=True,
        date_update=datetime(2026, 5, 13, tzinfo=UTC),
        raw_json={"k": "v"},
        parser_version=1,
    )
    with pytest.raises(ValidationError):
        d.lat = 0.0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ParsedDetail(  # type: ignore[call-arg]
            lat=None,
            lon=None,
            has_boundaries=None,
            date_update=None,
            raw_json={},
            parser_version=1,
            stray="x",
        )


# ---------------------------------------------------------------------------
# Cross-cutting: SseEvent union all participants are frozen + critical/normal
# ---------------------------------------------------------------------------
def test_priority_classvar_partition():
    """Critical: cycle/smtp/session.  Normal: lot.new / lot.status.
    Guards against accidental priority change during refactor.
    """
    assert SseCycleError.priority == "critical"
    assert SseSmtpFailed.priority == "critical"
    assert SseSessionExpired.priority == "critical"
    assert SseLotNew.priority == "normal"
    assert SseLotStatus.priority == "normal"
