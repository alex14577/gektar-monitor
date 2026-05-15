"""Domain DTOs — Pydantic v2 models (frozen, no I/O).

All Pydantic models:
* `frozen=True`     → immutability invariant (safe to share across threads/EventBus).
* `extra="forbid"`  → typos are validation errors, never silent ignores.
* `strict=True` is applied **point-wise** via `Annotated[int, Field(strict=True)]`
  on identifier / timestamp-ish fields where coercion `"123" → 123` is a bug
  signal. We do NOT enable strict-mode globally (datetime parsing from ISO
  strings depends on coercion).

ResolvedSmtpEndpoint is a stdlib `dataclass(frozen=True, slots=True)` —
infra-internal DTO that never crosses the EventBus / SSE / DB boundary.

SsePayloadSchema is a plain class with class-level `frozenset` whitelists
used by `EventBus` to scrub PII before persisting critical events.
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

# ---------------------------------------------------------------------------
# Type aliases (closed Literal-enums)
# ---------------------------------------------------------------------------

#: Whitelist of fields that may appear in `lots_history` diffs.
#: Being a `Literal` means SQL-identifier injection is impossible at type level.
TrackedField = Literal[
    "status",
    "area_sqm",
    "date_update",
    "auction",
    "is_active",
    "list_presence",
]

#: Closed enum for error taxonomy in SSE / log events.
#: Raw `exception.__class__.__name__` is **never** allowed (PII vector).
ErrorCategory = Literal[
    "network",
    "http_5xx",
    "http_4xx",
    "redirect_login",
    "timeout",
    "parse_bug",
    "schema_anomaly",
    "internal_error",  # M1 fix: для unexpected exceptions (bugs)
]


# Reusable `ConfigDict` — DRY. All domain models share the same policy.
_DOMAIN_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    # Parser is responsible for whitespace normalisation; domain layer must
    # NOT silently mutate inputs.
    str_strip_whitespace=False,
)


# ---------------------------------------------------------------------------
# Lot — mirror of the `lots` table (see db/schema.sql)
# ---------------------------------------------------------------------------
class Lot(BaseModel):
    """The canonical lot DTO. Mirrors the `lots` table.

    Field order matches docs/data-model/lot.md §Lot.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    # --- Identification ----------------------------------------------------
    id: StrictInt
    cadastral_no: str

    # --- List / detail-card columns ---------------------------------------
    area_sqm: int | None
    region: str
    municipality: str | None
    land_category: str | None
    permitted_use: str | None
    ogv: str | None
    status: str
    date_create: datetime
    date_update: datetime | None

    # --- Coordinates ------------------------------------------------------
    lat: float | None
    lon: float | None
    has_boundaries: bool | None

    # --- Extensibility ----------------------------------------------------
    raw_json: dict[str, Any]
    parser_version: StrictInt = 1

    # --- Lifecycle --------------------------------------------------------
    first_seen: datetime
    last_seen: datetime
    detail_fetched_at: datetime | None
    enrichment_status: Literal["pending", "done", "failed", "permanent_fail"] | None

    # --- Removal-tracking -------------------------------------------------
    last_seen_at: datetime | None
    is_active: bool = True
    inactive_reason: Literal["status_changed", "hard_removed", "list_absent"] | None = None
    inactive_since: datetime | None = None
    inactive_confirmed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Diff protocol — FieldChange / LotUpsertResult
# ---------------------------------------------------------------------------
class FieldChange(BaseModel):
    """A single field delta to be written to `lots_history`."""

    model_config = _DOMAIN_MODEL_CONFIG

    field: TrackedField
    old_value: Any | None
    new_value: Any | None


class LotUpsertResult(BaseModel):
    """Return value of `LotRepository.upsert`.

    Invariant: a brand-new INSERT (`was_new=True`) MUST have `changes == []`
    because `lots_history` is not written for inserts (see ADR-016 / R3-C2).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    was_new: bool
    changes: list[FieldChange]

    @model_validator(mode="after")
    def _check_new_implies_no_changes(self) -> LotUpsertResult:
        if self.was_new and self.changes:
            raise ValueError(
                "LotUpsertResult: was_new=True implies changes==[] "
                "(lots_history is not written for INSERTs)"
            )
        return self


# ---------------------------------------------------------------------------
# Public / user DTOs for EventBus and UI fan-out
# ---------------------------------------------------------------------------
class LotPublicDTO(Lot):
    """Lot enriched with presentation hints, WITHOUT any user-state.

    Safe to publish via EventBus (multi-user forward-compat).

    `raw_json` is excluded from `model_dump` / `model_dump_json` — it is a
    heavy, parser-internal blob that has no place in fan-out payloads. It
    remains on the inherited `Lot` shape (so the repo / cache layer keeps
    full fidelity), but never crosses the SSE / EventBus boundary.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    age_seconds: StrictInt
    tier: Literal["match", "silent", "gone"]
    freshness: Literal["hot", "warm", "cool", "cold"]

    @model_serializer(mode="wrap")
    def _exclude_raw_json(self, handler):  # type: ignore[no-untyped-def]
        data = handler(self)
        # `model_dump_json()` also funnels through the same serializer.
        data.pop("raw_json", None)
        return data


class LotUserDTO(LotPublicDTO):
    """`LotPublicDTO` + per-user state. Returned via server-rendered HTML
    or `GET /api/lots/{id}/user-state` — never via fan-out SSE.

    Inherits the `raw_json` exclusion from `LotPublicDTO`.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    starred: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None


# ---------------------------------------------------------------------------
# SMTP credentials (state.db) — secret handling per ADR-017
# ---------------------------------------------------------------------------
class SmtpCredentials(BaseModel):
    """SMTP login + password. Canon shape per docs/data-model/settings.md §SmtpCredentials.

    `smtp_password` is `SecretStr` — never leaks via `__repr__` / `__str__` /
    `model_dump` (ADR-017).

    `from_name` — optional RFC 5322 display name for the ``From:`` header.
    When set, the notifier produces ``"Display Name" <user@host>``.
    When ``None``, the bare email address is used.

    Security invariants (gektar_monitor-ctz):
    - Pickle is HARD-BLOCKED: `__reduce__` / `__getstate__` / `__setstate__`
      each raise `TypeError`. Pydantic's `SecretStr.__reduce__` would otherwise
      preserve the plaintext password in the pickle stream for round-trip.
    - `copy.deepcopy` is HARD-BLOCKED for the same reason: `deepcopy` falls
      back to `__reduce__` when no `__deepcopy__` hook is present, so the ban
      applies to both paths.
    - `multiprocessing` (Queue / Pipe / shared-memory) relies on pickle
      internally — blocked transitively by the pickle ban.
    - `faulthandler.dump_traceback`: cannot be blocked at the Python level;
      OS-level process isolation (seccomp / AppArmor / SELinux) is required
      if crash-dump confidentiality is a hard requirement. The ADR-017
      diagnostic-zip exclude-list (*.dmp, core.*) is the current mitigation.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    smtp_user: str
    smtp_password: SecretStr
    smtp_host: str
    smtp_port: Annotated[StrictInt, Field(ge=1, le=65535)] = 587
    use_default: bool = True
    from_name: str | None = None

    # ------------------------------------------------------------------
    # Pickle / deepcopy hard-block (ADR-017, gektar_monitor-ctz)
    # ------------------------------------------------------------------

    _PICKLE_MSG: ClassVar[str] = (
        "SmtpCredentials cannot be pickled — security policy (ADR-017, "
        "gektar_monitor-ctz). Use SmtpCredentialsRepository to persist and "
        "reload credentials via the DB layer."
    )

    def __reduce__(self) -> object:  # type: ignore[override]
        raise TypeError(self._PICKLE_MSG)

    def __getstate__(self) -> object:
        raise TypeError(self._PICKLE_MSG)

    def __setstate__(self, state: object) -> None:
        raise TypeError(self._PICKLE_MSG)

    def __deepcopy__(self, memo: dict | None = None) -> object:  # type: ignore[override]
        raise TypeError(self._PICKLE_MSG)


# ---------------------------------------------------------------------------
# SSE / EventBus payloads — PII-fail-closed
# ---------------------------------------------------------------------------
class SseCycleError(BaseModel):
    """Critical event: monitor cycle failed.

    Canon shape per docs/data-model/lot.md §CycleResult. Free-form error `message` goes to
    `app.jsonl` (structured log), NOT to SSE — typed `error_category` is the
    only error signal allowed in fan-out (PII fail-closed).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["critical"]] = "critical"

    event: Literal["cycle.error"] = "cycle.error"
    timestamp: datetime
    cycle_id: StrictInt
    error_category: ErrorCategory
    # NB: `message`, stacktrace, exception_repr are intentionally NOT
    # modelled — PII vectors. Free-form context goes to app.jsonl.


class SseSmtpFailed(BaseModel):
    """Critical event: SMTP delivery failed.

    Canon shape per docs/data-model/notifications.md §NotificationRecord.
    `channel_id` is an FK into the channel-table; the plaintext recipient
    address is NEVER sent on the bus.
    `recipient_hash` and `message_id` live in the `notifications` table for
    dedup — they are NOT part of the SSE payload (PII / MTA-leak vectors).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["critical"]] = "critical"

    event: Literal["smtp.failed"] = "smtp.failed"
    timestamp: datetime
    channel_id: str
    attempt_no: StrictInt
    error_category: ErrorCategory
    # NB: recipient (plaintext) / recipient_hash / message_id / smtp_response /
    # smtp_code are intentionally NOT modelled — PII / MTA-leak vectors.


# ---------------------------------------------------------------------------
# ResolvedSmtpEndpoint — infra-internal frozen dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResolvedSmtpEndpoint:
    """Pinned (DNS-resolve + policy-check) result for a single SMTP connect.

    `ip` is used for the actual `socket.connect()` (closes TOCTOU between
    resolve and connect). `original_host` is retained for SNI / TLS-cert
    verification. See R3-C4 / ADR-021.
    """

    ip: str
    family: socket.AddressFamily
    port: int
    original_host: str


# ---------------------------------------------------------------------------
# SsePayloadSchema — whitelist for PII-redaction
# ---------------------------------------------------------------------------
class SsePayloadSchema:
    """Whitelist of fields per critical-event type.

    Used by `EventBus.publish()` when persisting `last_critical_event:*` rows
    and by the `force-unsubscribe` log redactor. Anything outside the
    whitelist (stacktraces, recipient addresses, tokens, cookies) is dropped
    BEFORE serialization.

    Unknown event types resolve to an empty `frozenset()` — **fail-closed**:
    rather than risk leaking PII on a typo, persist nothing.
    """

    # `redirect_url` is intentionally NOT whitelisted — the URL the site
    # redirects to after session expiry can embed return-tokens, CSRF
    # nonces, or originating cabinet paths (PII / token-leak vector). The
    # SSE consumer needs only the literal event discriminator + the
    # bus-stamped timestamp to drive the "re-login" UX.
    SESSION_EXPIRED: ClassVar[frozenset[str]] = frozenset({"timestamp", "event"})
    CYCLE_ERROR: ClassVar[frozenset[str]] = frozenset({"timestamp", "cycle_id", "error_category"})
    SMTP_FAILED: ClassVar[frozenset[str]] = frozenset(
        {"timestamp", "channel_id", "error_category", "attempt_no"}
    )

    _BY_EVENT: ClassVar[dict[str, frozenset[str]]] = {
        "session.expired": SESSION_EXPIRED,
        "cycle.error": CYCLE_ERROR,
        "smtp.failed": SMTP_FAILED,
    }

    @classmethod
    def for_event(cls, event_type: str) -> frozenset[str]:
        """Return the whitelist for `event_type`, or `frozenset()` if unknown.

        Fail-closed: typos do NOT silently leak fields.
        """
        return cls._BY_EVENT.get(event_type, frozenset())


# ---------------------------------------------------------------------------
# Settings — `config.json` full tree (docs/data-model/settings.md §Settings)
# ---------------------------------------------------------------------------
#
# All sub-models share the domain policy (frozen + extra=forbid + no auto-strip).
# `smtp_password` / `smtp_user` are intentionally absent — they live in
# `state.db` (see `SmtpCredentials` above + ADR-020).
# ---------------------------------------------------------------------------
class FiltersConfig(BaseModel):
    """Notify-time RF subject filter; empty list = all from selected macro-regions."""

    model_config = _DOMAIN_MODEL_CONFIG

    rf_subjects: list[int] = Field(default_factory=list)


class UIConfig(BaseModel):
    """Local web UI binding & cosmetics."""

    model_config = _DOMAIN_MODEL_CONFIG

    bind_host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1024, le=65535)] = 8080
    auto_open_browser: bool = True
    font_size_px: Literal[14, 16, 18] = 16
    theme: Literal["auto", "light", "dark"] = "auto"


class EmailConfig(BaseModel):
    """Email-channel config WITHOUT credentials (those live in state.db)."""

    model_config = _DOMAIN_MODEL_CONFIG

    enabled: bool = True
    use_default_smtp: bool = True
    smtp_host: str | None = None
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    from_address: str | None = None
    recipients: list[EmailStr] = Field(default_factory=list)


class BrowserConfig(BaseModel):
    model_config = _DOMAIN_MODEL_CONFIG

    enabled: bool = True


class HeartbeatConfig(BaseModel):
    """Optional daily-`all-quiet` digest. Off by default."""

    model_config = _DOMAIN_MODEL_CONFIG

    enabled: bool = False
    time: str = "09:00"  # HH:MM, local TZ


class SoundEscalationConfig(BaseModel):
    """Browser-side escalation steps in seconds (UI consumes the list)."""

    model_config = _DOMAIN_MODEL_CONFIG

    enabled: bool = True
    escalate_at_seconds: list[int] = Field(default_factory=lambda: [60, 120])


class DndConfig(BaseModel):
    """Do-not-disturb until ISO timestamp; `None` = disabled."""

    model_config = _DOMAIN_MODEL_CONFIG

    until: str | None = None


class CatchupConfig(BaseModel):
    """Replay missed events after long offline window."""

    model_config = _DOMAIN_MODEL_CONFIG

    enabled: bool = True
    min_offline_minutes: int = 60


class NotificationsConfig(BaseModel):
    model_config = _DOMAIN_MODEL_CONFIG

    email: EmailConfig = Field(default_factory=EmailConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    sound_escalation: SoundEscalationConfig = Field(default_factory=SoundEscalationConfig)
    dnd: DndConfig = Field(default_factory=DndConfig)
    catchup: CatchupConfig = Field(default_factory=CatchupConfig)


class MonitoringConfig(BaseModel):
    model_config = _DOMAIN_MODEL_CONFIG

    full_scan_time: str = "04:00"
    full_scan_l2_priority_days: int = 7


class TargetConfig(BaseModel):
    """Целевой сайт (Программа «Дальневосточный гектар», Punycode-домен для requests
    совместимости). Endpoint paths — доменные константы внутри TorgiUrlBuilder."""

    model_config = _DOMAIN_MODEL_CONFIG

    base_url: str = "https://xn--80aaggvgieoeoa2bo7l.xn--p1ai"
    request_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 90  # study: 80-150s/page
    user_agent: str = "fis-monitor/0.1"

    @field_validator("base_url", mode="after")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")


class Settings(BaseModel):
    """Root `config.json` Pydantic model. Canon shape per docs/data-model/settings.md.

    `interval_minutes=0` means continuous (no idle between cycles).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    @model_validator(mode="before")
    @classmethod
    def _migrate_subject_site_ids(cls, data: Any) -> Any:
        # Preserves on-disk user intent during ADR-035 transition.
        if not isinstance(data, dict):
            return data
        data = dict(data)  # defensive copy — never mutate caller's dict
        legacy = data.pop("subject_site_ids", None)
        if legacy is not None:
            filters = data.get("filters") or {}
            # Coerce FiltersConfig instance so .get() is always safe below.
            if isinstance(filters, FiltersConfig):
                filters = filters.model_dump()
            elif not isinstance(filters, dict):
                filters = {}
            if not filters.get("rf_subjects"):
                filters["rf_subjects"] = legacy
                data["filters"] = filters
        return data

    mode: Literal["local", "server"] = "local"
    # Default 1 min: отзывчивый out-of-box. Per docs/ops/server-performance-v3.md
    # типичный цикл ~71s, 60s между full-pass — рабочая нижняя планка.
    interval_minutes: Annotated[int, Field(ge=0, le=60)] = 1
    timezone: str = "Europe/Moscow"
    regions: list[int] = Field(default_factory=lambda: [1, 2])
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)


# ---------------------------------------------------------------------------
# LotUserState — per-lot user data (docs/data-model/settings.md §LotUserState)
# ---------------------------------------------------------------------------
class LotUserState(BaseModel):
    """Per-lot user-state. Survives parser-reparse (separate `lot_user_state`
    table — see db/schema.sql).

    Forward-compat: in multi-user v3 the PK becomes `(user_id, lot_id)`; for
    MVP single-user `lot_id` alone is the natural key. Callers MUST NOT rely
    on `lot_id` being unique post-migration.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    lot_id: StrictInt
    starred: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    note: str | None = None
    seen_at: datetime | None = None
    updated_at: datetime


# ---------------------------------------------------------------------------
# OnboardingState — server-side FSM (ADR-018, docs/onboarding.md)
# ---------------------------------------------------------------------------
class OnboardingState(StrEnum):
    """Onboarding FSM. Server is the SSOT — UI mirrors what the server allows.

    Transitions (see docs/onboarding.md):

        not_started → regions_set → smtp_configured → recipients_set → completed
    """

    NOT_STARTED = "not_started"
    REGIONS_SET = "regions_set"
    SMTP_CONFIGURED = "smtp_configured"
    RECIPIENTS_SET = "recipients_set"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# CycleResult — one monitor-cycle row (docs/data-model/lot.md §CycleResult)
# ---------------------------------------------------------------------------
class CycleResult(BaseModel):
    """A single completed monitor cycle. Mirrors the `cycles` table.

    `error` is a free-form, log-only diagnostic — NEVER published on the
    bus (PII vector). Typed `error_category` belongs on `SseCycleError`,
    not here.

    Contract for callers writing `CycleResult.error`:
      * MUST NOT contain stacktraces, raw exception reprs, URLs with
        tokens, recipient addresses, cookies, or any PII.
      * Use the closed `ErrorCategory` enum on `SseCycleError` for the
        typed fan-out signal — `error` here is human-readable only.
      * Hard-capped at 200 chars (`max_length=200`) to keep `cycles`
        rows bounded and to discourage accidental stacktrace dumps.
      * Logged to `cycles` table; included in `diagnostic.zip` ONLY
        after PII-redaction (see bd `0t8`).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    id: StrictInt
    region: int
    started_at: datetime
    finished_at: datetime
    status: Literal["ok", "error", "aborted"]
    lots_fetched: int
    new_lots: int
    error: Annotated[str, Field(max_length=200)] | None = None
    id_schema_check: Literal["ok", "anomaly", "confirmed"] = "ok"


# ---------------------------------------------------------------------------
# NotificationRecord — notifications table (ADR-019, notifications.md)
# ---------------------------------------------------------------------------
class NotificationRecord(BaseModel):
    """A row in the `notifications` table. PK `(lot_id, channel, recipient)`.

    State machine (ADR-019):

        pending → sent              (terminal-success)
        pending → permanent_fail    (terminal-failure, no further retries)

    `sent_at` and `last_attempt_at` are nullable: an `INSERT OR IGNORE`
    reserve creates the row with `status='pending'`, both timestamps NULL.
    Only `reserve` / `mark_attempt` / `mark_sent` / `mark_permanent_fail` on
    `NotificationsRepository` may mutate this row, each inside its own short
    `BEGIN IMMEDIATE` transaction.

    `recipient='local'` is used for `browser` / `heartbeat` channels which
    have no addressable target.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    lot_id: StrictInt
    channel: Literal["email", "browser", "heartbeat"]
    recipient: str
    status: Literal["pending", "sent", "permanent_fail"]
    attempt_no: StrictInt
    last_attempt_at: datetime | None
    sent_at: datetime | None

    def __repr__(self) -> str:
        """PII-safe representation: recipient is hashed via sha256[:8].

        NB: sha256[:8] is a short FINGERPRINT for log correlation, NOT a
        cryptographic identity — 32 bits → collision probability ~1 in 65k
        via birthday paradox. Sufficient for distinguishing recipients in
        logs without exposing plaintext PII.
        """
        recipient_hash = hashlib.sha256(self.recipient.encode("utf-8")).hexdigest()[:8]
        return (
            f"NotificationRecord(lot_id={self.lot_id}, channel={self.channel!r}, "
            f"recipient=<sha256:{recipient_hash}>, status={self.status!r}, "
            f"attempt_no={self.attempt_no})"
        )


# ---------------------------------------------------------------------------
# NotifierConfig — plugin base (docs/data-model/notifications.md §NotifierConfig)
# ---------------------------------------------------------------------------
class NotifierConfig(BaseModel):
    """Base class for channel-plugin configs.

    Concrete subclasses (EmailNotifierConfig, BrowserNotifierConfig, ...)
    live with their notifier implementations in `infra/notifiers/*`. The
    base class only fixes the policy (frozen, extra=forbid) so plugins
    cannot loosen it.
    """

    model_config = _DOMAIN_MODEL_CONFIG


# ---------------------------------------------------------------------------
# Parser outputs (docs/architecture/03-protocols.md §3.2)
# ---------------------------------------------------------------------------
#
# Parser invariant (R3-minor): absent / empty fields → `None`, NEVER `""`.
# `compute_changes` normalises `""` → `None` defence-in-depth, but parsers
# MUST emit `None` to avoid FTS-trigger churn on every upsert.
# ---------------------------------------------------------------------------
class ParsedListRow(BaseModel):
    """One row of the lot-list page. Mirrors list-table columns only.

    Detail-card fields (lat/lon, raw_json, parser_version) live on
    `ParsedDetail`, NOT here — list parser cannot see them.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    id: StrictInt
    cadastral_no: str
    area_sqm: int | None
    region: str
    municipality: str | None
    land_category: str | None
    permitted_use: str | None
    ogv: str | None
    status: str
    date_create: datetime
    date_update: datetime | None


class ParsedDetail(BaseModel):
    """One detail-card payload (`cabinet-free-lot-view`).

    Everything that is NOT a typed field goes into `raw_json` — that keeps
    the parser forward-compatible with new site columns without a schema
    migration.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    lat: float | None
    lon: float | None
    has_boundaries: bool | None
    date_update: datetime | None
    raw_json: dict[str, Any]
    parser_version: StrictInt = 1


# ---------------------------------------------------------------------------
# NotifyResult — Notifier Result-pattern
# (docs/architecture/03-protocols.md §3.3, docs/notifications.md)
# ---------------------------------------------------------------------------
@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class NotifyResult:
    """Return of `Notifier.send()` / `Notifier.test()`.

    Result-pattern is used ONLY for notifiers
    (docs/architecture/00-open-questions-resolved.md Q2). The rest of the
    codebase raises `UpstreamError(category=...)` / `DomainError`.

    Log-only contract for `detail`:
      * Written to `app.jsonl` and the `notifications.detail` column by
        the Dispatcher.
      * NEVER published verbatim on the SSE bus — the Dispatcher MUST
        map it to a closed `ErrorCategory` value (see bd `arl`).
      * Type-level guarantee: `SseSmtpFailed` has `extra="forbid"`, so a
        sloppy `event_dict["detail"] = result.detail` will raise at
        validation time.
      * Hard-capped at 500 chars to bound `app.jsonl` line size and to
        defend against a misbehaving notifier smuggling multi-KB MTA
        responses through the log pipeline.

    Implementation note: this is a `pydantic.dataclasses.dataclass` (NOT
    a stdlib `@dataclass`) so that `Annotated[..., Field(max_length=500)]`
    is enforced at construction time. `dataclasses.fields(...)`,
    `FrozenInstanceError`, and tuple-style positional construction still
    work identically.

    Fields:
        ok        — terminal success.
        detail    — human-readable diagnostic (see contract above).
        retryable — whether the Dispatcher should retry (network / 5xx)
                    vs treat as terminal (auth / 4xx).
    """

    ok: bool
    detail: Annotated[str, Field(max_length=500)]
    retryable: bool


# ---------------------------------------------------------------------------
# LoginOutcome / SessionStatus — auth seams (docs/architecture/03-protocols.md §3.4)
# ---------------------------------------------------------------------------
#: Closed set of `LoginOutcome.error` hint values.
#:
#: Free-form `playwright:<reason>` open form is intentionally REMOVED —
#: the raw `<reason>` substring is a PII / internal-detail leak vector
#: (it can carry browser-internal paths, target URLs, etc.). Mapper in
#: the LoginSession use case translates a Playwright exception into one
#: of the closed members below; raw diagnostics go to `app.jsonl`.
LoginErrorHint = Literal[
    "timeout",
    "cancelled",
    "playwright_disconnect",
    "playwright_timeout",
    "playwright_missing_binary",
    "playwright_missing_deps",
    "playwright_other",
    "needs_manual_login",
]


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """Return of `LoginSession.open_headed_login()`.

    `error` is a closed-set Literal hint, NOT a free-form message:
    one of `LoginErrorHint` members, or `None` on success. Free-form
    diagnostics belong in `app.jsonl`.
    """

    success: bool
    cookies_updated: bool
    error: LoginErrorHint | None


class SessionStatus(StrEnum):
    """Result of `SessionProbe.check()` — cookie-validity health-check.

    EXPIRING == less than 10 minutes until session expiry (heuristic; the
    probe sees a 200 but with a refresh hint in the body).
    """

    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# HttpResponse / LockHandle — infra-internal frozen dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Return of `HttpClient.get()`. Infra-internal — never crosses the bus.

    `final_url` is required for the `redirect_login` detection (302 on
    `/login` after session expiry — see `ErrorCategory`).
    """

    status: int
    text: str
    headers: Mapping[str, str]
    final_url: str


@dataclass(frozen=True, slots=True)
class LockHandle:
    """Opaque handle returned by `Locker.acquire()` and consumed by
    `Locker.release(handle)`.

    `fd` is the open file descriptor of the lock-file; used by `release()`
    to unlock and close.

    `pid` is the writer-PID stamped into the lock-file for human inspection
    (`who holds the lock?`). It is NOT used for arbitration — OS-level
    lock (`fcntl.flock` / `msvcrt.locking`) is the SSOT.

    `path` is the lock-file path; used by `release()` to unlink.
    """

    fd: int
    pid: int
    path: str


# ---------------------------------------------------------------------------
# SSE event payloads — SessionExpired / LotNew / LotStatus
# ---------------------------------------------------------------------------
#
# `priority` is a `ClassVar` Literal (OCP — adding a new event type does NOT
# require touching `EventBus.publish`). See docs/architecture/03-protocols.md §3.5.
# ---------------------------------------------------------------------------
class SseSessionExpired(BaseModel):
    """Critical event: stored cookies are no longer valid.

    `extra="forbid"` blocks every PII vector at the type level (stacktraces,
    `redirect_url` with return-tokens, etc. — only `timestamp` and the
    literal `event` discriminator are permitted).

    `timestamp` matches `SseCycleError` / `SseSmtpFailed` shape so the
    EventBus persist-slot (`last_critical_event:session`) preserves event
    ordering and TTL accounting consistently across all critical event
    types (see `SsePayloadSchema.SESSION_EXPIRED`).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["critical"]] = "critical"

    timestamp: datetime
    event: Literal["session.expired"] = "session.expired"


class SseLotNew(BaseModel):
    """Normal event: a brand-new lot appeared.

    Carries a `LotPublicDTO` (NOT `LotUserDTO`) — fan-out across multiple
    SSE subscribers MUST NOT leak one tab's user-state into another
    (multi-user forward-compat, docs/architecture/03-protocols.md §3.6.1).
    `LotPublicDTO`'s `model_serializer` strips `raw_json` automatically.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["normal"]] = "normal"

    event: Literal["lot.new"] = "lot.new"
    lot: LotPublicDTO
    fragment_template: Literal["poster"]


class SseLotStatus(BaseModel):
    """Normal event: a tracked lot transitioned (gone / status changed).

    Only the lot-id + the new status are broadcast — the full lot row is
    served via a separate REST call to avoid bloating the bus when many
    lots churn at once.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["normal"]] = "normal"

    event: Literal["lot.status"] = "lot.status"
    lot_id: StrictInt
    new_status: str
    event_type: Literal["gone", "changed"]


# ---------------------------------------------------------------------------
# Conversion helpers — public domain functions
# ---------------------------------------------------------------------------


def parsed_row_to_lot(row: ParsedListRow, now: datetime) -> Lot:
    """Construct a minimal ``Lot`` from a ``ParsedListRow``.

    Detail fields (lat, lon, raw_json, enrichment_status, …) are set to
    sensible defaults — ``EnrichmentService`` will fill them in.

    Moved from ``services/monitor_cycle.py`` (was private ``_parsed_row_to_lot``)
    because both ``MonitorCycleService`` and ``BackfillService`` need it.
    Domain conversion belongs in the domain layer alongside the types involved.
    """
    return Lot(
        id=row.id,
        cadastral_no=row.cadastral_no,
        area_sqm=row.area_sqm,
        region=row.region,
        municipality=row.municipality,
        land_category=row.land_category,
        permitted_use=row.permitted_use,
        ogv=row.ogv,
        status=row.status,
        date_create=row.date_create,
        date_update=row.date_update,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=now,
        last_seen=now,
        detail_fetched_at=None,
        enrichment_status="pending",
        last_seen_at=now,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
    )


#: Fields written to lots_history on every upsert — minimal MVP set.
#: Moved from services/monitor_cycle.py so both MonitorCycleService and
#: BackfillService can import without cross-service dependency.
DEFAULT_TRACKED_FIELDS: tuple[TrackedField, ...] = (
    "status",
    "area_sqm",
    "date_update",
    "is_active",
    # "list_presence" removed: forward-compat, not yet implemented
    # (see diff._UNIMPLEMENTED_FIELDS). Regression: gektar_monitor-cr4.
)


def lot_to_public_dto(lot: Lot) -> LotPublicDTO:
    """Construct a ``LotPublicDTO`` from a ``Lot`` with default presentation hints.

    ``age_seconds``, ``tier``, and ``freshness`` are presentation hints that
    are computed properly by the web layer.  For EventBus fan-out and retry
    paths we use safe defaults — downstream consumers that need accurate tiers
    should recompute from the lot's timestamps.

    Moved from ``services/monitor_cycle.py`` (was private ``_lot_to_public_dto``)
    because it is a pure domain conversion used by multiple services
    (MonitorCycleService, NotifierDispatcher retry path).  High cohesion:
    belongs in the domain layer alongside the types it converts.
    """
    return LotPublicDTO(
        **lot.model_dump(),
        age_seconds=0,
        tier="match",
        freshness="hot",
    )


# ---------------------------------------------------------------------------
# ProviderSuggestion — SMTP provider auto-fill DTO (ADR-038)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProviderSuggestion:
    """Pre-filled SMTP endpoint for a known email provider.

    Returned by ``SmtpProviderCatalog.lookup()`` when the email domain is
    in the static catalog. DTO without behavior — infra-internal, never
    crosses the EventBus / SSE / DB boundary.

    Fields:
        smtp_host: FQDN of the SMTP submission endpoint (e.g. ``"smtp.yandex.ru"``).
        smtp_port: Port number — 465 for implicit TLS, 587 for STARTTLS.
        use_starttls: True for port-587 STARTTLS, False for port-465 implicit TLS.
        app_password_url: URL to provider's app-password setup docs, or ``None``
            if a regular account password is sufficient.
        provider_label: Human-readable provider name for UI display (e.g. ``"Yandex"``).
    """

    smtp_host: str
    smtp_port: int
    use_starttls: bool
    app_password_url: str | None
    provider_label: str


# ---------------------------------------------------------------------------
# SseEvent — closed union over all bus event types
# ---------------------------------------------------------------------------
#: All five concrete SSE event DTOs. `EventBus.publish(event: SseEvent)` and
#: `EventBus.subscribe() -> EventSubscription[SseEvent]` use this alias.
#: Adding a new event type = extend this union AND register its priority
#: ClassVar; nothing else changes (OCP).
type SseEvent = SseCycleError | SseSmtpFailed | SseSessionExpired | SseLotNew | SseLotStatus
