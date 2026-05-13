"""Tests for SsePayloadSchema whitelist (PII fail-closed)."""

from __future__ import annotations

from fis_monitor.domain import SsePayloadSchema


# ---------------------------------------------------------------------------
# T11a — cycle.error whitelist is EXACT (no `message`, no PII)
# ---------------------------------------------------------------------------
def test_sse_payload_schema_cycle_error_exact_whitelist():
    assert SsePayloadSchema.for_event("cycle.error") == frozenset(
        {"timestamp", "cycle_id", "error_category"}
    )


# ---------------------------------------------------------------------------
# T11b — smtp.failed whitelist is EXACT (channel_id, no recipient/message_id)
# ---------------------------------------------------------------------------
def test_sse_payload_schema_smtp_failed_exact_whitelist():
    assert SsePayloadSchema.for_event("smtp.failed") == frozenset(
        {"timestamp", "channel_id", "error_category", "attempt_no"}
    )


# ---------------------------------------------------------------------------
# T11c — session.expired whitelist is EXACT
# ---------------------------------------------------------------------------
def test_sse_payload_schema_session_expired_exact_whitelist():
    assert SsePayloadSchema.for_event("session.expired") == frozenset(
        {"timestamp", "redirect_url"}
    )


# ---------------------------------------------------------------------------
# T12 — Unknown / empty event types → fail-closed empty frozenset
# ---------------------------------------------------------------------------
def test_sse_payload_schema_unknown_event_returns_empty_frozenset():
    assert SsePayloadSchema.for_event("totally.unknown") == frozenset()
    assert SsePayloadSchema.for_event("") == frozenset()
    assert SsePayloadSchema.for_event("unknown.xyz") == frozenset()


# ---------------------------------------------------------------------------
# T13 — Class constants are frozenset (immutable, cannot be runtime-mutated)
# ---------------------------------------------------------------------------
def test_sse_payload_schema_class_constants_are_frozensets():
    for name in ("SESSION_EXPIRED", "CYCLE_ERROR", "SMTP_FAILED"):
        value = getattr(SsePayloadSchema, name)
        assert isinstance(value, frozenset), f"{name} must be frozenset for immutability"


# ---------------------------------------------------------------------------
# Sanity: known PII vectors are NOT in any whitelist.
# ---------------------------------------------------------------------------
def test_sse_payload_schema_no_pii_vectors_anywhere():
    pii_vectors = {
        "stacktrace",
        "exception_repr",
        "recipient",
        "recipient_hash",
        "message_id",
        "message",
        "smtp_response",
        "smtp_code",
        "cookie",
        "token",
    }
    for evt in ("cycle.error", "smtp.failed", "session.expired"):
        whitelist = SsePayloadSchema.for_event(evt)
        leaks = pii_vectors & whitelist
        assert not leaks, f"{evt}: PII fields leaked into whitelist: {leaks}"
