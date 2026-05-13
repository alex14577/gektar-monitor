"""Tests for RequestsHttpClient.

Coverage:
- Success case (200 OK) without retry
- Retry on 5xx status codes
- Retry on 429 (Too Many Requests)
- Retry on ConnectionError
- Retry on Timeout
- Exhausted retries (all 3 attempts fail)
- No retry on 4xx status codes
- Default timeout configuration
- Custom timeout parameter
- User-Agent header always present
- Dependency injection of Session
- Dependency injection of sleep_fn
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests
from requests.exceptions import ConnectionError, Timeout, TooManyRedirects

from fis_monitor.domain.models import HttpResponse
from fis_monitor.infra.http.client import RequestsHttpClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int,
    text: str = "response body",
    headers: dict[str, str] | None = None,
    url: str = "https://example.com/path",
) -> Mock:
    """Create a mock requests.Response object."""
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.text = text
    response.headers = headers or {"Content-Type": "text/html"}
    response.url = url
    return response


# ---------------------------------------------------------------------------
# Success case — no retry
# ---------------------------------------------------------------------------


def test_get_success_200() -> None:
    """Test successful GET request (200 OK) without retry."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    result = client.get("https://example.com")

    assert result.status == 200
    assert result.text == "OK"
    assert result.final_url == "https://example.com/path"
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Retry on 5xx
# ---------------------------------------------------------------------------


def test_get_retry_on_500() -> None:
    """Test retry on 500 Internal Server Error.

    Sequence: 500 → sleep(1.0) → 500 → sleep(2.0) → 200
    Total: 2 sleeps, 3 attempts.
    """
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(500, "Server Error"),
        _mock_response(500, "Server Error"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert result.text == "OK"
    assert session.get.call_count == 3
    assert sleep_fn.call_count == 2
    sleep_fn.assert_any_call(1.0)
    sleep_fn.assert_any_call(2.0)


def test_get_retry_on_502() -> None:
    """Test retry on 502 Bad Gateway."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(502, "Bad Gateway"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2
    assert sleep_fn.call_count == 1
    sleep_fn.assert_called_once_with(1.0)


def test_get_retry_on_503() -> None:
    """Test retry on 503 Service Unavailable."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(503, "Service Unavailable"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# Retry on 429 (Too Many Requests)
# ---------------------------------------------------------------------------


def test_get_retry_on_429() -> None:
    """Test retry on 429 Too Many Requests."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(429, "Too Many Requests"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2
    assert sleep_fn.call_count == 1


# ---------------------------------------------------------------------------
# Retry on ConnectionError
# ---------------------------------------------------------------------------


def test_get_retry_on_connection_error() -> None:
    """Test retry on requests.exceptions.ConnectionError."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        ConnectionError("Connection refused"),
        _mock_response(200, "OK"),
    ]

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2
    assert sleep_fn.call_count == 1
    sleep_fn.assert_called_once_with(1.0)


# ---------------------------------------------------------------------------
# Retry on Timeout
# ---------------------------------------------------------------------------


def test_get_retry_on_timeout() -> None:
    """Test retry on requests.exceptions.Timeout."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        Timeout("Read timed out"),
        _mock_response(200, "OK"),
    ]

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2
    assert sleep_fn.call_count == 1


# ---------------------------------------------------------------------------
# No retry on 4xx
# ---------------------------------------------------------------------------


def test_get_404_no_retry() -> None:
    """Test that 404 Not Found does NOT trigger retry."""
    session = Mock(spec=requests.Session)
    response = _mock_response(404, "Not Found")
    session.get.return_value = response

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 404
    assert session.get.call_count == 1
    assert sleep_fn.call_count == 0


def test_get_403_no_retry() -> None:
    """Test that 403 Forbidden does NOT trigger retry."""
    session = Mock(spec=requests.Session)
    response = _mock_response(403, "Forbidden")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)

    result = client.get("https://example.com")

    assert result.status == 403
    assert session.get.call_count == 1


def test_get_400_no_retry() -> None:
    """Test that 400 Bad Request does NOT trigger retry."""
    session = Mock(spec=requests.Session)
    response = _mock_response(400, "Bad Request")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)

    result = client.get("https://example.com")

    assert result.status == 400
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Exhausted retries
# ---------------------------------------------------------------------------


def test_get_exhausted_retries_5xx() -> None:
    """Test that all 3 attempts with 500 returns last response."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(500, "Error 1"),
        _mock_response(500, "Error 2"),
        _mock_response(500, "Error 3"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 500
    assert result.text == "Error 3"
    assert session.get.call_count == 3
    # 2 sleeps: after attempts 1 and 2
    assert sleep_fn.call_count == 2
    sleep_fn.assert_any_call(1.0)
    sleep_fn.assert_any_call(2.0)


def test_get_exhausted_retries_connection_error() -> None:
    """Test that all 3 attempts with ConnectionError re-raises last exception."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        ConnectionError("Failed 1"),
        ConnectionError("Failed 2"),
        ConnectionError("Failed 3"),
    ]

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    with pytest.raises(ConnectionError, match="Failed 3"):
        client.get("https://example.com")

    assert session.get.call_count == 3
    assert sleep_fn.call_count == 2


# ---------------------------------------------------------------------------
# Non-retryable exceptions
# ---------------------------------------------------------------------------


def test_get_non_retryable_exception_raises_immediately() -> None:
    """Test that non-retryable exceptions are raised immediately without retry."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = ValueError("Invalid URL")

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    with pytest.raises(ValueError, match="Invalid URL"):
        client.get("https://example.com")

    assert session.get.call_count == 1
    assert sleep_fn.call_count == 0


def test_get_non_retryable_request_exception_raises_immediately() -> None:
    """Test that non-retryable RequestException subclass raises immediately without retry."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = TooManyRedirects("too many")

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    with pytest.raises(TooManyRedirects, match="too many"):
        client.get("https://example.com")

    assert session.get.call_count == 1
    assert sleep_fn.call_count == 0


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------


def test_default_timeout() -> None:
    """Test that default timeout is (5.0, 30.0)."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com")

    # Check that session.get was called with timeout tuple
    assert session.get.call_count == 1
    call_args = session.get.call_args
    assert call_args is not None
    assert call_args.kwargs["timeout"] == (5.0, 30.0)


def test_explicit_timeout() -> None:
    """Test that explicit timeout is used as (timeout, timeout) tuple."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com", timeout=15.0)

    # Check that session.get was called with custom timeout
    call_args = session.get.call_args
    assert call_args is not None
    assert call_args.kwargs["timeout"] == (15.0, 15.0)


def test_timeout_none_uses_default() -> None:
    """Test that timeout=None uses default timeout."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com", timeout=None)

    call_args = session.get.call_args
    assert call_args is not None
    assert call_args.kwargs["timeout"] == (5.0, 30.0)


# ---------------------------------------------------------------------------
# User-Agent header
# ---------------------------------------------------------------------------


def test_user_agent_header_added() -> None:
    """Test that User-Agent header is automatically added."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com")

    call_args = session.get.call_args
    assert call_args is not None
    headers = call_args.kwargs["headers"]
    assert "User-Agent" in headers
    assert headers["User-Agent"] == "fis-monitor/1.0"


def test_user_agent_not_overridden_if_provided() -> None:
    """Test that user-provided User-Agent is preserved."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get(
        "https://example.com",
        headers={"User-Agent": "custom-agent/1.0"},
    )

    call_args = session.get.call_args
    assert call_args is not None
    headers = call_args.kwargs["headers"]
    assert headers["User-Agent"] == "custom-agent/1.0"


def test_user_agent_with_other_headers() -> None:
    """Test that User-Agent is added alongside other headers."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get(
        "https://example.com",
        headers={"Authorization": "Bearer token"},
    )

    call_args = session.get.call_args
    assert call_args is not None
    headers = call_args.kwargs["headers"]
    assert headers["User-Agent"] == "fis-monitor/1.0"
    assert headers["Authorization"] == "Bearer token"


# ---------------------------------------------------------------------------
# DI: Session
# ---------------------------------------------------------------------------


def test_di_session_used() -> None:
    """Test that injected Session is used for requests."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com", params={"key": "value"})

    session.get.assert_called_once()
    call_args = session.get.call_args
    assert call_args is not None
    assert call_args.args[0] == "https://example.com"
    assert call_args.kwargs["params"] == {"key": "value"}


# ---------------------------------------------------------------------------
# DI: sleep_fn
# ---------------------------------------------------------------------------


def test_di_sleep_fn_called() -> None:
    """Test that injected sleep_fn is called for backoff."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(500, "Error"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    client.get("https://example.com")

    sleep_fn.assert_called_once_with(1.0)


# ---------------------------------------------------------------------------
# Parameter passing
# ---------------------------------------------------------------------------


def test_params_passed_to_session() -> None:
    """Test that params are passed to session.get."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com", params={"q": "search"})

    call_args = session.get.call_args
    assert call_args is not None
    assert call_args.kwargs["params"] == {"q": "search"}


def test_headers_passed_to_session() -> None:
    """Test that headers are passed to session.get (plus User-Agent)."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    client.get("https://example.com", headers={"X-Custom": "value"})

    call_args = session.get.call_args
    assert call_args is not None
    headers = call_args.kwargs["headers"]
    assert headers["X-Custom"] == "value"
    assert "User-Agent" in headers


# ---------------------------------------------------------------------------
# Response model construction
# ---------------------------------------------------------------------------


def test_http_response_structure() -> None:
    """Test that HttpResponse is correctly constructed from requests.Response."""
    session = Mock(spec=requests.Session)
    response = _mock_response(
        200,
        "response text",
        headers={"Content-Type": "application/json", "X-Custom": "header"},
        url="https://example.com/redirected",
    )
    session.get.return_value = response

    client = RequestsHttpClient(session=session)
    result = client.get("https://example.com")

    assert isinstance(result, HttpResponse)
    assert result.status == 200
    assert result.text == "response text"
    assert result.final_url == "https://example.com/redirected"
    assert "Content-Type" in result.headers
    assert result.headers["Content-Type"] == "application/json"
    assert result.headers["X-Custom"] == "header"


# ---------------------------------------------------------------------------
# Backoff sequence verification
# ---------------------------------------------------------------------------


def test_backoff_sequence() -> None:
    """Test that backoff sleeps are (1.0, 2.0) after attempts 1 and 2.

    Attempt 1 fails → sleep(1.0)
    Attempt 2 fails → sleep(2.0)
    Attempt 3 fails → no sleep (last attempt)
    """
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        _mock_response(500, "Error"),
        _mock_response(500, "Error"),
        _mock_response(500, "Error"),
    ]

    sleep_calls = []

    def mock_sleep(duration: float) -> None:
        sleep_calls.append(duration)

    client = RequestsHttpClient(session=session, sleep_fn=mock_sleep)

    client.get("https://example.com")

    assert sleep_calls == [1.0, 2.0]


def test_no_sleep_on_success() -> None:
    """Test that no sleep occurs if first attempt succeeds."""
    session = Mock(spec=requests.Session)
    response = _mock_response(200, "OK")
    session.get.return_value = response

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    client.get("https://example.com")

    assert sleep_fn.call_count == 0


# ---------------------------------------------------------------------------
# Edge cases: Mixed retry/success scenarios
# ---------------------------------------------------------------------------


def test_retry_once_then_success() -> None:
    """Test 1 failure followed by success on attempt 2."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(500, "Error"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2
    assert sleep_fn.call_count == 1


def test_two_failures_then_success() -> None:
    """Test 2 failures followed by success on attempt 3."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(429, "Too Many Requests"),
        Timeout("Read timed out"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 3
    assert sleep_fn.call_count == 2
    assert sleep_fn.call_args_list[0][0] == (1.0,)
    assert sleep_fn.call_args_list[1][0] == (2.0,)


# ---------------------------------------------------------------------------
# Real requests.exceptions subclasses
# ---------------------------------------------------------------------------


def test_connection_error_retry() -> None:
    """Test that requests.exceptions.ConnectionError is retryable."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        requests.exceptions.ConnectionError("Connection reset by peer"),
        _mock_response(200, "OK"),
    ]

    client = RequestsHttpClient(session=session)
    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2


def test_timeout_error_retry() -> None:
    """Test that requests.exceptions.Timeout is retryable."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        requests.exceptions.ConnectTimeout("Connection timeout"),
        _mock_response(200, "OK"),
    ]

    client = RequestsHttpClient(session=session)
    result = client.get("https://example.com")

    assert result.status == 200
    assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_retry_logs_warning_on_status_code_retry(caplog) -> None:
    """Test that retry on status code logs warning with attempt number."""
    session = Mock(spec=requests.Session)
    responses = [
        _mock_response(500, "Server Error"),
        _mock_response(500, "Server Error"),
        _mock_response(200, "OK"),
    ]
    session.get.side_effect = responses

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    with caplog.at_level("WARNING"):
        result = client.get("https://example.com")

    assert result.status == 200
    # Should have 2 warnings: one after attempt 1, one after attempt 2
    assert len(caplog.records) == 2

    # Verify attempt numbers in log record attributes
    assert caplog.records[0].attempt == 1
    assert caplog.records[1].attempt == 2

    # Verify URL in log record attributes
    assert caplog.records[0].url == "https://example.com"
    assert caplog.records[1].url == "https://example.com"

    # Verify log messages contain status code
    assert "500" in caplog.records[0].message
    assert "500" in caplog.records[1].message


def test_retry_logs_warning_on_exception_retry(caplog) -> None:
    """Test that retry on exception logs warning with attempt number and exception name."""
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        ConnectionError("Connection refused"),
        Timeout("Read timed out"),
        _mock_response(200, "OK"),
    ]

    sleep_fn = Mock()
    client = RequestsHttpClient(session=session, sleep_fn=sleep_fn)

    with caplog.at_level("WARNING"):
        result = client.get("https://example.com")

    assert result.status == 200
    # Should have 2 warnings: one after attempt 1 (ConnectionError), one after attempt 2 (Timeout)
    assert len(caplog.records) == 2

    # Verify attempt numbers in log record attributes
    assert caplog.records[0].attempt == 1
    assert caplog.records[1].attempt == 2

    # Verify exception names in log messages
    assert "ConnectionError" in caplog.records[0].message
    assert "Timeout" in caplog.records[1].message
