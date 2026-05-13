"""Tests for `fis_monitor.domain.models`."""

from __future__ import annotations

import dataclasses
import socket
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fis_monitor.domain import (
    FieldChange,
    Lot,
    LotPublicDTO,
    LotUpsertResult,
    LotUserDTO,
    ResolvedSmtpEndpoint,
    SmtpCredentials,
    SseCycleError,
    SseSmtpFailed,
)


# ---------------------------------------------------------------------------
# T1 — Lot immutability
# ---------------------------------------------------------------------------
def test_lot_is_frozen(make_lot):
    lot = make_lot()
    with pytest.raises(ValidationError):
        lot.status = "Зарезервирован"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T2 — Lot extra="forbid"
# ---------------------------------------------------------------------------
def test_lot_extra_forbid(make_lot):
    with pytest.raises(ValidationError):
        make_lot(unknown_field="boom")


# ---------------------------------------------------------------------------
# T3 — Lot JSON round-trip
# ---------------------------------------------------------------------------
def test_lot_roundtrip_json(make_lot):
    original = make_lot()
    payload = original.model_dump_json()
    restored = Lot.model_validate_json(payload)
    assert restored == original


# ---------------------------------------------------------------------------
# T4 — SmtpCredentials never leaks plaintext in repr / dump
# ---------------------------------------------------------------------------
def test_smtp_credentials_repr_no_plaintext():
    creds = SmtpCredentials(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bot@example.com",
        smtp_password="hunter2",  # type: ignore[arg-type]
    )
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds.model_dump())
    assert "hunter2" not in creds.model_dump_json()


# ---------------------------------------------------------------------------
# T5 — SecretStr get_secret_value() works
# ---------------------------------------------------------------------------
def test_smtp_credentials_get_secret_value():
    creds = SmtpCredentials(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bot@example.com",
        smtp_password="hunter2",  # type: ignore[arg-type]
    )
    assert creds.smtp_password.get_secret_value() == "hunter2"


# ---------------------------------------------------------------------------
# T5b — SmtpCredentials canon shape: no from_addr / use_starttls
# ---------------------------------------------------------------------------
def test_smtp_credentials_canon_fields():
    """Canon shape per docs/data-model.md:101-108. No from_addr, no use_starttls."""
    fields = SmtpCredentials.model_fields
    assert set(fields.keys()) == {
        "smtp_user",
        "smtp_password",
        "smtp_host",
        "smtp_port",
        "use_default",
    }


def test_smtp_credentials_use_default_default_true():
    creds = SmtpCredentials(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bot@example.com",
        smtp_password="hunter2",  # type: ignore[arg-type]
    )
    assert creds.use_default is True


# ---------------------------------------------------------------------------
# T6 — LotUpsertResult invariant: was_new=True ⇒ changes==[]
# ---------------------------------------------------------------------------
def test_lot_upsert_result_invariant_new_no_changes():
    fc = FieldChange(field="status", old_value=None, new_value="Свободен")

    LotUpsertResult(was_new=True, changes=[])
    LotUpsertResult(was_new=False, changes=[fc])

    with pytest.raises(ValidationError):
        LotUpsertResult(was_new=True, changes=[fc])


# ---------------------------------------------------------------------------
# T7 — FieldChange Literal whitelist
# ---------------------------------------------------------------------------
def test_field_change_tracked_field_literal():
    FieldChange(field="status", old_value="a", new_value="b")
    with pytest.raises(ValidationError):
        FieldChange(field="unknown", old_value=None, new_value=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T8 — LotPublicDTO MUST NOT carry user-state fields
# ---------------------------------------------------------------------------
def test_lot_public_dto_no_user_state_fields():
    for forbidden in ("starred", "submitted", "submitted_at", "note", "seen_at"):
        assert forbidden not in LotPublicDTO.model_fields, (
            f"LotPublicDTO must not expose user-state field {forbidden!r}"
        )

    for required in ("age_seconds", "tier", "freshness"):
        assert required in LotPublicDTO.model_fields


# ---------------------------------------------------------------------------
# T9 — LotUserDTO IS-A LotPublicDTO and adds user-state fields
# ---------------------------------------------------------------------------
def test_lot_user_dto_inherits_public():
    assert issubclass(LotUserDTO, LotPublicDTO)
    for required in ("starred", "submitted", "submitted_at", "note", "seen_at"):
        assert required in LotUserDTO.model_fields
    for required in ("age_seconds", "tier", "freshness", "id", "cadastral_no"):
        assert required in LotUserDTO.model_fields


# ---------------------------------------------------------------------------
# T10 — ErrorCategory Literal rejects unknown values
# ---------------------------------------------------------------------------
def test_error_category_literal_rejects_unknown():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    SseCycleError(timestamp=now, cycle_id=1, error_category="network")
    with pytest.raises(ValidationError):
        SseCycleError(timestamp=now, cycle_id=1, error_category="foo")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T10b — SseCycleError canon shape (no `message`)
# ---------------------------------------------------------------------------
def test_sse_cycle_error_no_message_field():
    """`message` is free-form text → goes to app.jsonl, NOT to SSE (PII risk)."""
    assert "message" not in SseCycleError.model_fields
    assert set(SseCycleError.model_fields.keys()) == {
        "timestamp",
        "cycle_id",
        "error_category",
    }


# ---------------------------------------------------------------------------
# T10c — SseSmtpFailed canon shape (channel_id, no recipient_hash/message_id)
# ---------------------------------------------------------------------------
def test_sse_smtp_failed_canon_fields():
    """recipient_hash / message_id live in `notifications` table, NOT in SSE."""
    fields = SseSmtpFailed.model_fields
    assert set(fields.keys()) == {
        "timestamp",
        "channel_id",
        "attempt_no",
        "error_category",
    }
    assert "recipient_hash" not in fields
    assert "message_id" not in fields


# ---------------------------------------------------------------------------
# T13 — ResolvedSmtpEndpoint frozen dataclass
# ---------------------------------------------------------------------------
def test_resolved_smtp_endpoint_frozen():
    ep = ResolvedSmtpEndpoint(
        ip="10.0.0.1",
        family=socket.AF_INET,
        port=587,
        original_host="smtp.example.com",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ep.port = 25  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T14 — Strict int on id rejects coercion from string
# ---------------------------------------------------------------------------
def test_lot_strict_int_id(make_lot):
    with pytest.raises(ValidationError):
        make_lot(id="123")


# ---------------------------------------------------------------------------
# T15 — `raw_json` excluded from LotPublicDTO / LotUserDTO dumps
# ---------------------------------------------------------------------------
def test_lot_dumps_include_raw_json(make_lot):
    """Base `Lot` keeps `raw_json` for repo / cache layer."""
    lot = make_lot()
    assert "raw_json" in lot.model_dump()
    assert "raw_json" in lot.model_dump_json()


def test_lot_public_dto_excludes_raw_json(make_lot):
    """`LotPublicDTO` is fan-out shape — `raw_json` is heavy + PII-adjacent."""
    base = make_lot()
    public = LotPublicDTO(
        **base.model_dump(),
        age_seconds=120,
        tier="match",
        freshness="hot",
    )
    assert "raw_json" not in public.model_dump()
    assert "raw_json" not in public.model_dump_json()


def test_lot_user_dto_excludes_raw_json(make_lot):
    """`LotUserDTO` inherits the exclusion."""
    base = make_lot()
    user = LotUserDTO(
        **base.model_dump(),
        age_seconds=120,
        tier="match",
        freshness="hot",
        starred=True,
    )
    assert "raw_json" not in user.model_dump()
    assert "raw_json" not in user.model_dump_json()


# ---------------------------------------------------------------------------
# T16 / T17 — DTO immutability
# ---------------------------------------------------------------------------
def test_lot_public_dto_is_frozen(make_lot):
    base = make_lot()
    public = LotPublicDTO(
        **base.model_dump(),
        age_seconds=10,
        tier="match",
        freshness="hot",
    )
    with pytest.raises(ValidationError):
        public.tier = "silent"  # type: ignore[misc]


def test_lot_user_dto_is_frozen(make_lot):
    base = make_lot()
    user = LotUserDTO(
        **base.model_dump(),
        age_seconds=10,
        tier="match",
        freshness="hot",
    )
    with pytest.raises(ValidationError):
        user.starred = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T18 / T19 — SSE DTO frozen + extra=forbid
# ---------------------------------------------------------------------------
def test_sse_cycle_error_frozen_and_extra_forbid():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    evt = SseCycleError(timestamp=now, cycle_id=1, error_category="network")
    with pytest.raises(ValidationError):
        evt.cycle_id = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SseCycleError(
            timestamp=now,
            cycle_id=1,
            error_category="network",
            stacktrace="leaky",  # type: ignore[call-arg]
        )


def test_sse_smtp_failed_frozen_and_extra_forbid():
    now = datetime(2026, 5, 13, tzinfo=UTC)
    evt = SseSmtpFailed(
        timestamp=now,
        channel_id="email:user@example.com",
        attempt_no=1,
        error_category="network",
    )
    with pytest.raises(ValidationError):
        evt.attempt_no = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SseSmtpFailed(
            timestamp=now,
            channel_id="email:user@example.com",
            attempt_no=1,
            error_category="network",
            recipient="leak@example.com",  # type: ignore[call-arg]
        )
