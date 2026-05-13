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

import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    model_serializer,
    model_validator,
)

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

    Field order matches data-model.md §Lot.
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
    """SMTP login + password. Canon shape per docs/data-model.md:101-108.

    `smtp_password` is `SecretStr` — never leaks via `__repr__` / `__str__` /
    `model_dump` (ADR-017).

    WARNING: do NOT pickle / `multiprocessing.Queue` / `faulthandler`-dump
    instances of this model. Pydantic `SecretStr.__reduce__` preserves the
    plaintext password for round-trip — this is by design. See bd issue
    `gektar_monitor-ctz` for `__reduce__` hardening follow-up.
    """

    model_config = _DOMAIN_MODEL_CONFIG

    smtp_user: str
    smtp_password: SecretStr
    smtp_host: str
    smtp_port: Annotated[StrictInt, Field(ge=1, le=65535)] = 587
    use_default: bool = True


# ---------------------------------------------------------------------------
# SSE / EventBus payloads — PII-fail-closed
# ---------------------------------------------------------------------------
class SseCycleError(BaseModel):
    """Critical event: monitor cycle failed.

    Canon shape per docs/data-model.md. Free-form error `message` goes to
    `app.jsonl` (structured log), NOT to SSE — typed `error_category` is the
    only error signal allowed in fan-out (PII fail-closed).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["critical"]] = "critical"

    timestamp: datetime
    cycle_id: StrictInt
    error_category: ErrorCategory
    # NB: `message`, stacktrace, exception_repr are intentionally NOT
    # modelled — PII vectors. Free-form context goes to app.jsonl.


class SseSmtpFailed(BaseModel):
    """Critical event: SMTP delivery failed.

    Canon shape per docs/data-model.md. `channel_id` is an FK into the
    channel-table; the plaintext recipient address is NEVER sent on the bus.
    `recipient_hash` and `message_id` live in the `notifications` table for
    dedup — they are NOT part of the SSE payload (PII / MTA-leak vectors).
    """

    model_config = _DOMAIN_MODEL_CONFIG

    priority: ClassVar[Literal["critical"]] = "critical"

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

    SESSION_EXPIRED: ClassVar[frozenset[str]] = frozenset({"timestamp", "redirect_url"})
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
