"""Unit tests for the three JSONL file channels in fis_monitor.utils.log.

Coverage:
- All 3 files created under ``<tmp_path>/logs/`` when data_dir is set.
- Audit logger does NOT have StackPIIFilter applied.
- App logger DOES have StackPIIFilter applied.
- ``log_request()`` produces whitelist-only fields in requests.jsonl.
- URL with query stripped before writing url_path.
- Rotation settings: when="midnight", backupCount=30, utc=True.
- Fail-closed: unwritable logs dir → no exception, single WARNING, subsequent
  audit calls are silent no-ops.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fis_monitor.utils.log import (
    _AUDIT_DISABLED_ATTR,
    _AUDIT_LOGGER,
    _FIS_MONITOR_LOGGER,
    _REQUESTS_LOGGER,
    _apply_query_policy,
    _strip_query,
    log_audit,
    log_request,
    setup_logging,
)
from fis_monitor.utils.log_filters import StackPIIFilter

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic clock for testing timestamps."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._fixed

    def monotonic(self) -> float:
        return 0.0


_CLOCK = FakeClock()


def _setup(tmp_path: Path, filters=None) -> None:
    """Helper: call setup_logging with data_dir=tmp_path."""
    setup_logging(
        clock=_CLOCK,
        level=logging.DEBUG,
        json_format=True,
        filters=filters or [StackPIIFilter()],
        data_dir=tmp_path,
    )


def _flush_all() -> None:
    """Flush all handlers on the three loggers so writes land before read."""
    for name in (_FIS_MONITOR_LOGGER, _AUDIT_LOGGER, _REQUESTS_LOGGER):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.flush()


def _read_jsonl(path: Path) -> list[dict]:
    """Read all JSON lines from a file; return empty list if not present."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


# ---------------------------------------------------------------------------
# 1. All three files are created
# ---------------------------------------------------------------------------


def test_all_three_files_created(tmp_path: Path) -> None:
    _setup(tmp_path)
    logs_dir = tmp_path / "logs"

    assert (logs_dir / "app.jsonl").exists(), "app.jsonl not created"
    assert (logs_dir / "audit.jsonl").exists(), "audit.jsonl not created"
    assert (logs_dir / "requests.jsonl").exists(), "requests.jsonl not created"


# ---------------------------------------------------------------------------
# 2. Audit logger: no StackPIIFilter
# ---------------------------------------------------------------------------


def test_audit_logger_has_no_pii_filter(tmp_path: Path) -> None:
    _setup(tmp_path)
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    for handler in audit_logger.handlers:
        for f in handler.filters:
            assert not isinstance(f, StackPIIFilter), (
                f"StackPIIFilter must NOT be on audit handler, found {f!r}"
            )


def test_audit_logger_does_not_propagate(tmp_path: Path) -> None:
    """Audit logger must not propagate to parent (which has StackPIIFilter)."""
    _setup(tmp_path)
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    assert not audit_logger.propagate


def test_audit_logger_writes_pii_untouched(tmp_path: Path) -> None:
    """Confirm PII like a URL with query is NOT scrubbed in audit channel."""
    _setup(tmp_path)
    # log_audit sends through fis_monitor.audit which has no StackPIIFilter.
    log_audit("config changed", url="https://example.com/path?token=SECRETTOKEN123456789")
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "audit.jsonl")
    assert records, "audit.jsonl is empty"
    last = records[-1]
    # The query must survive — StackPIIFilter is absent on this channel.
    assert "SECRETTOKEN123456789" in json.dumps(last), (
        "PII was scrubbed from audit channel — StackPIIFilter must NOT be present"
    )


# ---------------------------------------------------------------------------
# 3. App logger: StackPIIFilter IS applied
# ---------------------------------------------------------------------------


def test_app_logger_has_pii_filter(tmp_path: Path) -> None:
    _setup(tmp_path)
    root_logger = logging.getLogger(_FIS_MONITOR_LOGGER)
    file_handlers = [
        h for h in root_logger.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert file_handlers, "No TimedRotatingFileHandler found on fis_monitor logger"
    # At least one file handler must carry StackPIIFilter.
    has_filter = any(
        any(isinstance(f, StackPIIFilter) for f in h.filters)
        for h in file_handlers
    )
    assert has_filter, "StackPIIFilter not found on app file handler"


def test_app_logger_scrubs_pii(tmp_path: Path) -> None:
    """Tokens in app.jsonl should be scrubbed by StackPIIFilter."""
    _setup(tmp_path)
    app_logger = logging.getLogger(_FIS_MONITOR_LOGGER)
    app_logger.info("found token ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "app.jsonl")
    assert records, "app.jsonl is empty"
    last = records[-1]
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234" not in json.dumps(last), (
        "Token was NOT scrubbed in app channel — StackPIIFilter must be present"
    )
    assert "[token-scrubbed]" in json.dumps(last)


# ---------------------------------------------------------------------------
# 4. log_request: whitelist-only fields
# ---------------------------------------------------------------------------


def test_log_request_whitelist_fields(tmp_path: Path) -> None:
    _setup(tmp_path)
    log_request("GET", "/cabinet/free-lot", 200, 12.3, 1024, parser_version="v3")
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "requests.jsonl")
    assert records, "requests.jsonl is empty"
    last = records[-1]

    # JsonFormatter places extras under "ctx"; whitelist fields live there.
    ctx = last.get("ctx", {})
    assert ctx.get("method") == "GET"
    assert ctx.get("url_path") == "/cabinet/free-lot"
    assert ctx.get("status") == 200
    assert ctx.get("duration_ms") == pytest.approx(12.3)
    assert ctx.get("bytes") == 1024
    assert ctx.get("parser_version") == "v3"

    # Standard envelope fields (top-level, set by JsonFormatter).
    assert "timestamp" in last
    assert "level" in last
    assert "logger" in last
    assert "message" in last
    assert "service" in last

    # Forbidden fields MUST NOT appear anywhere in the serialised record.
    raw = json.dumps(last)
    for forbidden in ("Cookie", "Authorization", "Set-Cookie"):
        assert forbidden not in raw, f"{forbidden!r} found in requests record"


def test_log_request_optional_parser_version_absent(tmp_path: Path) -> None:
    """parser_version is optional; when omitted it must not appear in the record."""
    _setup(tmp_path)
    log_request("POST", "/some/path", 201, 5.0, 0)
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "requests.jsonl")
    assert records
    last = records[-1]
    # parser_version should not appear in ctx or top-level when not supplied.
    ctx = last.get("ctx", {})
    assert "parser_version" not in ctx
    assert "parser_version" not in last


# ---------------------------------------------------------------------------
# 5. URL query stripping in log_request
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5a. _strip_query: always drops query AND fragment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_input,expected",
    [
        # Basic query stripping.
        ("/cabinet/free-lot?page=2", "/cabinet/free-lot"),
        ("/search?q=hello&page=1", "/search"),
        ("/no-query", "/no-query"),
        # Full URL: scheme + netloc preserved, query dropped.
        ("https://example.com/path?token=secret", "https://example.com/path"),
        # Fragment handling — must be dropped in both branches (MAJOR 2).
        ("https://host/path#frag", "https://host/path"),
        ("/path#frag", "/path"),
        ("/path?q=1#frag", "/path"),
    ],
)
def test_strip_query(url_input: str, expected: str) -> None:
    assert _strip_query(url_input) == expected


# ---------------------------------------------------------------------------
# 5b. _apply_query_policy: login-route masking (BLOCKER 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_input,expected",
    [
        # Login routes → query masked as ?<redacted>.
        ("/login?token=abc", "/login?<redacted>"),
        ("/login?next=%2Fdashboard", "/login?<redacted>"),
        ("/auth/oauth/callback?code=XYZ", "/auth/oauth/callback?<redacted>"),
        # Full URL with login prefix: netloc dropped, query masked.
        ("https://host/login?token=abc", "/login?<redacted>"),
        # Non-login routes → query dropped.
        ("/cabinet/free-lot?page=2", "/cabinet/free-lot"),
        ("/search?q=hello", "/search"),
        # No query → path returned as-is.
        ("/search", "/search"),
        ("/login", "/login"),
    ],
)
def test_apply_query_policy(url_input: str, expected: str) -> None:
    assert _apply_query_policy(url_input) == expected


def test_log_request_login_route_masked_in_record(tmp_path: Path) -> None:
    """Login route query must appear as ?<redacted> in requests.jsonl (BLOCKER 1)."""
    _setup(tmp_path)
    log_request("POST", "/login?token=abc", 302, 1.0, 0)
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "requests.jsonl")
    assert records
    ctx = records[-1].get("ctx", {})
    assert ctx.get("url_path") == "/login?<redacted>", (
        f"Login route query must be masked; url_path={ctx.get('url_path')!r}"
    )
    # Raw token must never appear in the log.
    assert "token=abc" not in json.dumps(records[-1])


def test_log_request_auth_route_masked_in_record(tmp_path: Path) -> None:
    """Auth sub-route query must appear as ?<redacted> in requests.jsonl (BLOCKER 1)."""
    _setup(tmp_path)
    log_request("GET", "/auth/oauth/callback?code=XYZ", 302, 2.0, 0)
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "requests.jsonl")
    assert records
    ctx = records[-1].get("ctx", {})
    assert ctx.get("url_path") == "/auth/oauth/callback?<redacted>", (
        f"Auth route query must be masked; url_path={ctx.get('url_path')!r}"
    )
    assert "code=XYZ" not in json.dumps(records[-1])


def test_log_request_strips_query_before_writing(tmp_path: Path) -> None:
    _setup(tmp_path)
    log_request("GET", "/cabinet/free-lot?page=3", 200, 8.0, 512)
    _flush_all()
    records = _read_jsonl(tmp_path / "logs" / "requests.jsonl")
    assert records
    last = records[-1]
    ctx = last.get("ctx", {})
    assert ctx.get("url_path") == "/cabinet/free-lot", (
        f"Query was not stripped; url_path={ctx.get('url_path')!r}"
    )
    assert "page=3" not in json.dumps(last)


# ---------------------------------------------------------------------------
# 6. Rotation handler settings
# ---------------------------------------------------------------------------


def test_rotation_handler_settings(tmp_path: Path) -> None:
    """Verify TimedRotatingFileHandler is configured correctly without rollover."""
    _setup(tmp_path)

    # Check app.jsonl handler on fis_monitor logger.
    root_logger = logging.getLogger(_FIS_MONITOR_LOGGER)
    app_fh = next(
        (h for h in root_logger.handlers
         if isinstance(h, logging.handlers.TimedRotatingFileHandler)),
        None,
    )
    assert app_fh is not None, "No TimedRotatingFileHandler on fis_monitor logger"
    assert app_fh.when.upper() == "MIDNIGHT"
    assert app_fh.backupCount == 30
    assert app_fh.utc is True

    # Check audit.jsonl handler on fis_monitor.audit logger.
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    audit_fh = next(
        (h for h in audit_logger.handlers
         if isinstance(h, logging.handlers.TimedRotatingFileHandler)),
        None,
    )
    assert audit_fh is not None, "No TimedRotatingFileHandler on fis_monitor.audit logger"
    assert audit_fh.backupCount == 30
    assert audit_fh.utc is True

    # Check requests.jsonl handler on fis_monitor.requests logger.
    req_logger = logging.getLogger(_REQUESTS_LOGGER)
    req_fh = next(
        (h for h in req_logger.handlers
         if isinstance(h, logging.handlers.TimedRotatingFileHandler)),
        None,
    )
    assert req_fh is not None, "No TimedRotatingFileHandler on fis_monitor.requests logger"
    assert req_fh.backupCount == 30
    assert req_fh.utc is True


# ---------------------------------------------------------------------------
# 7. Fail-closed: unwritable logs dir
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root can write to any dir")
def test_fail_closed_unwritable_logs_dir(tmp_path: Path, caplog) -> None:
    """Unwritable logs/ dir: no exception, single WARNING, subsequent audit no-op."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    # Remove write permission so mkdir inside will fail on a nested attempt,
    # but simpler: make the logs_dir itself non-writable so file creation fails.
    logs_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x — no write

    try:
        with caplog.at_level(logging.WARNING, logger=_FIS_MONITOR_LOGGER):
            # Must not raise.
            setup_logging(
                clock=_CLOCK,
                level=logging.DEBUG,
                json_format=True,
                filters=[StackPIIFilter()],
                data_dir=tmp_path,
            )

        # A single WARNING should have been emitted.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Expected at least one WARNING about unwritable logs dir"

        # Subsequent audit calls must be silent no-ops (no exception).
        audit_logger = logging.getLogger(_AUDIT_LOGGER)
        assert getattr(audit_logger, _AUDIT_DISABLED_ATTR, False), (
            "Audit logger should be marked disabled after fail-closed"
        )

        # log_audit must not raise.
        log_audit("should be silently dropped")

    finally:
        # Restore permissions so pytest tmp_path cleanup works.
        logs_dir.chmod(stat.S_IRWXU)


def test_fail_closed_no_exception_escapes(tmp_path: Path) -> None:
    """setup_logging must never raise even when logs dir is unwritable on Linux."""
    # We simulate the failure by making the parent dir non-writable.
    # On systems where we can't remove write for root, we skip.
    if os.getuid() == 0:
        pytest.skip("root can write anywhere")

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o555)

    try:
        # Should not raise at all.
        setup_logging(
            clock=_CLOCK,
            level=logging.DEBUG,
            json_format=True,
            data_dir=blocked,
        )
    finally:
        blocked.chmod(0o755)


# ---------------------------------------------------------------------------
# 8. Idempotency: calling setup_logging twice does not duplicate handlers
# ---------------------------------------------------------------------------


def test_idempotent_file_handlers(tmp_path: Path) -> None:
    """A second setup_logging call replaces file handlers, not duplicates them."""
    _setup(tmp_path)
    _setup(tmp_path)  # second call

    root_logger = logging.getLogger(_FIS_MONITOR_LOGGER)
    app_file_handlers = [
        h for h in root_logger.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(app_file_handlers) == 1, (
        f"Expected exactly 1 app file handler after 2 setup_logging calls, "
        f"got {len(app_file_handlers)}"
    )

    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    audit_file_handlers = [
        h for h in audit_logger.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(audit_file_handlers) == 1, (
        f"Expected exactly 1 audit file handler after 2 setup_logging calls, "
        f"got {len(audit_file_handlers)}"
    )


# ---------------------------------------------------------------------------
# 9. Console-only mode (data_dir=None) leaves file channels absent
# ---------------------------------------------------------------------------


def test_no_file_channels_when_data_dir_none(tmp_path: Path) -> None:
    """When data_dir=None, no file handlers are installed anywhere."""
    import io

    buf = io.StringIO()
    setup_logging(clock=_CLOCK, stream=buf, level=logging.DEBUG, data_dir=None)

    for name in (_FIS_MONITOR_LOGGER, _AUDIT_LOGGER, _REQUESTS_LOGGER):
        lg = logging.getLogger(name)
        file_handlers = [
            h for h in lg.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert not file_handlers, (
            f"File handlers found on {name!r} when data_dir=None: {file_handlers}"
        )


def test_data_dir_none_removes_previously_installed_file_handlers(
    tmp_path: Path,
) -> None:
    """data_dir=None must remove file handlers that were installed by a prior call.

    Regression: a fresh start with no data_dir is easy to pass; the hard case
    is installing handlers first and then resetting — ensures _remove_file_handlers
    is actually invoked on child loggers when data_dir is None (MAJOR 3).
    """
    # First call: install all three file channels.
    _setup(tmp_path)

    # Verify they are present before reset.
    for name in (_FIS_MONITOR_LOGGER, _AUDIT_LOGGER, _REQUESTS_LOGGER):
        lg = logging.getLogger(name)
        assert any(
            isinstance(h, logging.handlers.TimedRotatingFileHandler)
            for h in lg.handlers
        ), f"Pre-condition failed: no file handler on {name!r} after _setup()"

    # Second call: reset with data_dir=None.
    setup_logging(clock=_CLOCK, data_dir=None)

    # All file handlers must be gone.
    for name in (_FIS_MONITOR_LOGGER, _AUDIT_LOGGER, _REQUESTS_LOGGER):
        lg = logging.getLogger(name)
        stale = [
            h for h in lg.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert not stale, (
            f"Stale file handlers remain on {name!r} after data_dir=None reset: {stale}"
        )
