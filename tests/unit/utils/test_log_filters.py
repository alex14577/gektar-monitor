"""Unit tests for fis_monitor.utils.log_filters.StackPIIFilter.

Test matrix
-----------
URL scrubbing:
  - URL with query string → query stripped.
  - URL without query string → unchanged.
  - Plain ``?`` in non-URL text → NOT scrubbed (no false positive).

Token scrubbing:
  - 32-char hex string → scrubbed.
  - Short alphanumeric string (< 24 chars) → NOT scrubbed.

Combined / exc_text:
  - ``exc_text`` containing both a URL query and a token → both scrubbed.

Filter contract:
  - ``filter()`` always returns ``True``.
  - ``filter()`` is idempotent (applying twice = same result).

Integration (handler-level):
  - ``setup_logging(..., filters=[StackPIIFilter()])`` installs the filter on
    the handler; emitting a record with a URL query produces output without the
    query string.
"""

from __future__ import annotations

import io
import logging
from logging import LogRecord

from fis_monitor.utils.log_filters import StackPIIFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    msg: str = "",
    args: tuple | None = None,
    exc_text: str | None = None,
) -> LogRecord:
    record = LogRecord(
        name="fis_monitor.test",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg=msg,
        args=args or (),
        exc_info=None,
    )
    if exc_text is not None:
        record.exc_text = exc_text
    return record


_FILTER = StackPIIFilter()


# ---------------------------------------------------------------------------
# URL query scrubbing
# ---------------------------------------------------------------------------


class TestUrlQueryScrubbing:
    def test_url_with_query_is_scrubbed(self) -> None:
        """Query string component is replaced with ``?[scrubbed]``."""
        record = _make_record(
            msg="GET https://api.example.com/?token=ABC123XYZ"
        )
        _FILTER.filter(record)
        assert "token=ABC123XYZ" not in record.msg
        assert "?[scrubbed]" in record.msg

    def test_url_with_multiple_query_params_is_scrubbed(self) -> None:
        """All query params are replaced in one pass."""
        record = _make_record(
            msg="Request: https://example.com/path?a=1&b=secret"
        )
        _FILTER.filter(record)
        assert "a=1" not in record.msg
        assert "b=secret" not in record.msg
        assert "?[scrubbed]" in record.msg

    def test_url_without_query_is_untouched(self) -> None:
        """URL with no query component must not be modified."""
        original = "GET https://api.example.com/path/to/resource"
        record = _make_record(msg=original)
        _FILTER.filter(record)
        assert record.msg == original

    def test_plain_question_mark_not_scrubbed(self) -> None:
        """A bare ``?`` in ordinary text (not a URL) must NOT be scrubbed."""
        original = "Is this working? Yes it is!"
        record = _make_record(msg=original)
        _FILTER.filter(record)
        assert record.msg == original

    def test_https_url_scrubbed(self) -> None:
        """https:// URLs are also scrubbed."""
        record = _make_record(
            msg="Redirect to https://secure.example.com/?session=XYZ123"
        )
        _FILTER.filter(record)
        assert "session=XYZ123" not in record.msg

    def test_url_in_parens_preserves_closing_paren(self) -> None:
        """Closing ``)`` after URL query must NOT be consumed by the scrub."""
        record = _make_record(
            msg="See (https://example.com/path?q=secret) for details"
        )
        _FILTER.filter(record)
        assert "secret" not in record.msg
        assert ")" in record.msg

    def test_url_in_square_brackets_preserves_closing_bracket(self) -> None:
        """Closing ``]`` after URL query must NOT be consumed."""
        record = _make_record(
            msg="[https://example.com/path?token=abc123] is the link"
        )
        _FILTER.filter(record)
        assert "abc123" not in record.msg
        assert "]" in record.msg

    def test_url_in_angle_brackets_preserves_closing_bracket(self) -> None:
        """Closing ``>`` after URL query must NOT be consumed."""
        record = _make_record(
            msg="<https://example.com/path?key=val> was requested"
        )
        _FILTER.filter(record)
        assert "key=val" not in record.msg
        assert ">" in record.msg

    def test_url_in_double_quotes_preserves_closing_quote(self) -> None:
        """Closing ``"`` after URL query must NOT be consumed."""
        record = _make_record(
            msg='href="https://example.com/path?x=secret" in HTML'
        )
        _FILTER.filter(record)
        assert "secret" not in record.msg
        assert '"' in record.msg

    def test_url_in_single_quotes_preserves_closing_quote(self) -> None:
        """Closing ``'`` after URL query must NOT be consumed."""
        record = _make_record(
            msg="url='https://example.com/path?y=hidden' in config"
        )
        _FILTER.filter(record)
        assert "hidden" not in record.msg
        assert "'" in record.msg


# ---------------------------------------------------------------------------
# Token scrubbing
# ---------------------------------------------------------------------------


class TestTokenScrubbing:
    def test_32char_hex_token_scrubbed(self) -> None:
        """A 32-character hex string in message is scrubbed."""
        token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        assert len(token) == 32
        record = _make_record(msg=f"Bearer {token}")
        _FILTER.filter(record)
        assert token not in record.msg
        assert "[token-scrubbed]" in record.msg

    def test_short_alphanumeric_not_scrubbed(self) -> None:
        """A short string (< 24 chars) must NOT be scrubbed."""
        short = "abc123def456"  # 12 chars
        assert len(short) < 24
        original = f"ID: {short}"
        record = _make_record(msg=original)
        _FILTER.filter(record)
        assert record.msg == original

    def test_exactly_24char_token_scrubbed(self) -> None:
        """Boundary condition: exactly 24 chars is scrubbed."""
        token = "A" * 24
        record = _make_record(msg=f"token={token}")
        _FILTER.filter(record)
        assert token not in record.msg

    def test_23char_token_not_scrubbed(self) -> None:
        """Boundary condition: 23 chars is NOT scrubbed.

        Note: the separator must NOT be part of the token alphabet (which
        includes ``=``).  We use a space so the 23-char run stands alone.
        """
        token = "A" * 23
        original = f"ref {token} end"  # space delimiters — not in token alphabet
        record = _make_record(msg=original)
        _FILTER.filter(record)
        assert token in record.msg


# ---------------------------------------------------------------------------
# exc_text scrubbing
# ---------------------------------------------------------------------------


class TestExcTextScrubbing:
    def test_exc_text_url_query_scrubbed(self) -> None:
        """URL query in ``exc_text`` is scrubbed."""
        exc_text = (
            "Traceback:\n"
            "  requests.get('https://api.example.com/?secret=hunter2')\n"
        )
        record = _make_record(msg="error", exc_text=exc_text)
        _FILTER.filter(record)
        assert "secret=hunter2" not in record.exc_text
        assert "?[scrubbed]" in record.exc_text

    def test_exc_text_token_scrubbed(self) -> None:
        """Token-like string in ``exc_text`` is scrubbed."""
        token = "deadbeefdeadbeefdeadbeefdeadbeef"
        exc_text = f"Error: invalid auth token {token}"
        record = _make_record(msg="error", exc_text=exc_text)
        _FILTER.filter(record)
        assert token not in record.exc_text
        assert "[token-scrubbed]" in record.exc_text

    def test_exc_text_both_url_and_token_scrubbed(self) -> None:
        """Both URL query string and token in ``exc_text`` are scrubbed."""
        token = "cafebabecafebabecafebabecafebabe"
        exc_text = (
            f"GET https://example.com/api?key=mysecret returned auth={token}"
        )
        record = _make_record(msg="request failed", exc_text=exc_text)
        _FILTER.filter(record)
        assert "key=mysecret" not in record.exc_text
        assert token not in record.exc_text
        assert "?[scrubbed]" in record.exc_text
        assert "[token-scrubbed]" in record.exc_text

    def test_none_exc_text_unchanged(self) -> None:
        """``exc_text=None`` (default) must not cause an error."""
        record = _make_record(msg="no exc")
        assert record.exc_text is None
        result = _FILTER.filter(record)
        assert result is True
        assert record.exc_text is None


# ---------------------------------------------------------------------------
# Filter contract: always True, idempotent
# ---------------------------------------------------------------------------


class TestFilterContract:
    def test_filter_returns_true_always(self) -> None:
        """``filter()`` must return ``True`` for every record (never blocks)."""
        for msg in [
            "plain message",
            "https://example.com/?q=secret",
            "token=" + "x" * 32,
        ]:
            record = _make_record(msg=msg)
            assert _FILTER.filter(record) is True

    def test_filter_is_idempotent(self) -> None:
        """Applying the filter twice produces the same result as once."""
        record = _make_record(
            msg="GET https://example.com/?k=v auth=" + "a1b2" * 8
        )
        _FILTER.filter(record)
        msg_after_first = record.msg

        _FILTER.filter(record)
        msg_after_second = record.msg

        assert msg_after_first == msg_after_second


# ---------------------------------------------------------------------------
# args scrubbing
# ---------------------------------------------------------------------------


class TestArgsScrubbing:
    def test_string_args_are_scrubbed(self) -> None:
        """String values in ``record.args`` tuple are scrubbed."""
        token = "b" * 32
        record = _make_record(msg="event %s", args=(token,))
        _FILTER.filter(record)
        assert isinstance(record.args, tuple)
        assert token not in record.args[0]
        assert "[token-scrubbed]" in record.args[0]

    def test_non_string_args_are_untouched(self) -> None:
        """Non-string values in ``record.args`` are left unchanged."""
        record = _make_record(msg="count %d", args=(42,))
        _FILTER.filter(record)
        assert record.args == (42,)


# ---------------------------------------------------------------------------
# Integration: setup_logging + StackPIIFilter
# ---------------------------------------------------------------------------


class FakeClock:
    """Minimal Clock for setup_logging tests."""

    from datetime import UTC, datetime

    _fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

    def now(self):  # type: ignore[override]
        return self._fixed

    def monotonic(self) -> float:
        return 0.0


class TestSetupLoggingIntegration:
    def test_setup_logging_installs_filter(self) -> None:
        """``setup_logging(filters=[StackPIIFilter()])`` puts filter on handler."""
        from fis_monitor.utils.log import setup_logging

        stream = io.StringIO()
        pii_filter = StackPIIFilter()
        setup_logging(
            clock=FakeClock(),
            stream=stream,
            level=logging.DEBUG,
            filters=[pii_filter],
        )
        root = logging.getLogger("fis_monitor")
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert pii_filter in handler.filters

    def test_setup_logging_output_scrubbed(self) -> None:
        """Emitting a log with URL query → output does not contain query value."""
        from fis_monitor.utils.log import setup_logging

        stream = io.StringIO()
        setup_logging(
            clock=FakeClock(),
            stream=stream,
            level=logging.DEBUG,
            json_format=False,  # plain text easier to assert
            filters=[StackPIIFilter()],
        )
        log = logging.getLogger("fis_monitor.integration_test")
        log.warning("GET https://api.example.com/?apikey=supersecretvalue123")

        output = stream.getvalue()
        assert "supersecretvalue123" not in output
        assert "apikey" not in output

    def test_setup_logging_no_filters_works(self) -> None:
        """``setup_logging`` without ``filters`` kwarg works (backward compat)."""
        from fis_monitor.utils.log import setup_logging

        stream = io.StringIO()
        setup_logging(clock=FakeClock(), stream=stream, level=logging.DEBUG)
        root = logging.getLogger("fis_monitor")
        assert len(root.handlers) == 1
        assert root.handlers[0].filters == []
