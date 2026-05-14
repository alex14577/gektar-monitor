"""Unit tests for fis_monitor.utils.log.

Tests cover:
- JsonFormatter: JSON validity, required fields, extras-in-ctx, exc_info,
  Clock injection, Pydantic model serialisation, datetime serialisation.
- setup_logging: idempotency, propagate=True, plain-text mode.
- get_logger: returns child of fis_monitor.
- Performance: P99 < 500 µs (marked slow).
"""

from __future__ import annotations

import io
import json
import logging
import timeit
from datetime import UTC, datetime
from logging import LogRecord

import pytest

from fis_monitor.utils.log import JsonFormatter, get_logger, setup_logging

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic clock for testing timestamps."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def monotonic(self) -> float:
        return 0.0


_FIXED_DT = datetime(2026, 5, 14, 15, 23, 1, 123456, tzinfo=UTC)
_FIXED_TS = "2026-05-14T15:23:01.123456Z"


def _make_record(
    name: str = "fis_monitor.test",
    level: int = logging.WARNING,
    msg: str = "hello",
    args: tuple = (),
    exc_info=None,
    extra: dict | None = None,
) -> LogRecord:
    """Build a LogRecord with optional extras."""
    record = LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def _make_formatter() -> JsonFormatter:
    return JsonFormatter(clock=FakeClock(_FIXED_DT))


# ---------------------------------------------------------------------------
# JsonFormatter tests
# ---------------------------------------------------------------------------


def test_json_formatter_outputs_valid_json() -> None:
    """format() produces a string parseable by json.loads."""
    formatter = _make_formatter()
    record = _make_record()
    output = formatter.format(record)
    parsed = json.loads(output)
    assert isinstance(parsed, dict)


def test_json_formatter_required_fields() -> None:
    """All reserved top-level keys are present in the output."""
    formatter = _make_formatter()
    record = _make_record(msg="required fields test")
    parsed = json.loads(formatter.format(record))

    for field in ("timestamp", "level", "logger", "message", "service", "trace_id"):
        assert field in parsed, f"Missing field: {field}"

    assert parsed["service"] == "fis_monitor"
    assert parsed["trace_id"] is None
    assert parsed["level"] == "WARNING"
    assert parsed["message"] == "required fields test"
    assert parsed["logger"] == "fis_monitor.test"


def test_json_formatter_extras_in_ctx() -> None:
    """Extra kwargs are nested under 'ctx', NOT flattened to top level."""
    formatter = _make_formatter()
    record = _make_record(extra={"lot_id": 123, "duration_ms": 45})
    parsed = json.loads(formatter.format(record))

    assert "ctx" in parsed
    assert parsed["ctx"]["lot_id"] == 123
    assert parsed["ctx"]["duration_ms"] == 45
    # Must NOT be at top level
    assert "lot_id" not in parsed
    assert "duration_ms" not in parsed


def test_json_formatter_exc_info_serialized() -> None:
    """When exc_info is set, 'exc' field is a non-empty string with class name."""
    formatter = _make_formatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = _make_record(exc_info=exc_info)
    parsed = json.loads(formatter.format(record))

    assert "exc" in parsed
    assert isinstance(parsed["exc"], str)
    assert len(parsed["exc"]) > 0
    assert "ValueError" in parsed["exc"]


def test_json_formatter_uses_clock() -> None:
    """Timestamp in JSON matches the FakeClock's fixed datetime."""
    formatter = _make_formatter()
    record = _make_record()
    parsed = json.loads(formatter.format(record))
    assert parsed["timestamp"] == _FIXED_TS


def test_json_formatter_pydantic_extras() -> None:
    """Pydantic BaseModel in extras is serialised via model_dump(), not repr."""
    from pydantic import SecretStr

    from fis_monitor.domain.models import SmtpCredentials

    creds = SmtpCredentials(
        smtp_user="user@example.com",
        smtp_password=SecretStr("secret123"),
        smtp_host="smtp.example.com",
        smtp_port=587,
    )
    formatter = _make_formatter()
    record = _make_record(extra={"settings": creds})
    # Must not raise TypeError; must produce valid JSON.
    output = formatter.format(record)
    parsed = json.loads(output)
    assert "ctx" in parsed
    ctx = parsed["ctx"]
    assert "settings" in ctx
    # model_dump() → dict, not string repr
    assert isinstance(ctx["settings"], dict)
    # SecretStr fields must be masked — plaintext must NEVER appear in output.
    # Pydantic serialises SecretStr as '**********' (not the raw value).
    assert "secret123" not in output, (
        "SecretStr plaintext leaked into log output — field must be declared as SecretStr"
    )
    assert ctx["settings"]["smtp_password"] == "**********", (
        "SecretStr field should serialise as Pydantic mask repr '**********'"
    )
    # Non-secret fields are preserved as-is.
    assert ctx["settings"]["smtp_user"] == "user@example.com"


def test_json_formatter_datetime_extras() -> None:
    """datetime objects in extras are serialised via .isoformat(), not repr."""
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    formatter = _make_formatter()
    record = _make_record(extra={"at": dt})
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["ctx"]["at"] == dt.isoformat()


# ---------------------------------------------------------------------------
# setup_logging tests
# ---------------------------------------------------------------------------


def test_setup_logging_idempotent() -> None:
    """Calling setup_logging twice results in exactly one handler on fis_monitor."""
    stream = io.StringIO()
    clock = FakeClock(_FIXED_DT)

    setup_logging(clock=clock, stream=stream, level=logging.DEBUG)
    setup_logging(clock=clock, stream=stream, level=logging.DEBUG)

    root = logging.getLogger("fis_monitor")
    assert len(root.handlers) == 1


def test_setup_logging_propagate_true() -> None:
    """fis_monitor logger has propagate=True after setup_logging (for caplog compat)."""
    stream = io.StringIO()
    setup_logging(clock=FakeClock(_FIXED_DT), stream=stream)

    root = logging.getLogger("fis_monitor")
    assert root.propagate is True


def test_setup_logging_plain_text_mode() -> None:
    """json_format=False produces non-JSON plain text output."""
    stream = io.StringIO()
    setup_logging(clock=FakeClock(_FIXED_DT), stream=stream, json_format=False)

    log = logging.getLogger("fis_monitor.plain_test")
    log.warning("plain message")

    output = stream.getvalue()
    assert output  # something was written
    # Should NOT be valid JSON (plain formatter)
    with pytest.raises((json.JSONDecodeError, ValueError)):
        json.loads(output.strip())


def test_setup_logging_second_call_replaces_stream() -> None:
    """A second setup_logging call with a different stream replaces the handler."""
    stream1 = io.StringIO()
    stream2 = io.StringIO()
    clock = FakeClock(_FIXED_DT)

    setup_logging(clock=clock, stream=stream1, level=logging.DEBUG)
    setup_logging(clock=clock, stream=stream2, level=logging.DEBUG)

    root = logging.getLogger("fis_monitor")
    # Only one handler, pointing at stream2
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert handler.stream is stream2  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# get_logger tests
# ---------------------------------------------------------------------------


def test_get_logger_returns_fis_monitor_child() -> None:
    """get_logger returns a Logger whose name is the given string."""
    log = get_logger("fis_monitor.foo")
    assert isinstance(log, logging.Logger)
    assert log.name == "fis_monitor.foo"
    # Parent should be the fis_monitor logger
    assert log.parent is logging.getLogger("fis_monitor")


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_perf_p99_under_500us() -> None:
    """P99 log-call latency must be ≤ 500 µs (SLO-L1).

    Uses timeit over 1 000 iterations; P99 derived from sorted samples.
    """
    stream = io.StringIO()
    clock = FakeClock(_FIXED_DT)
    setup_logging(clock=clock, stream=stream, level=logging.DEBUG)
    log = logging.getLogger("fis_monitor.perf_test")

    n = 1000
    times: list[float] = []
    for _ in range(n):
        t = timeit.timeit(
            lambda: log.info("perf test", extra={"x": 1}),
            number=1,
        )
        times.append(t)

    times.sort()
    p99_us = times[int(n * 0.99)] * 1_000_000
    assert p99_us <= 500, f"P99 latency {p99_us:.1f} µs exceeds 500 µs SLO"
