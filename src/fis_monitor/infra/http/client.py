"""RequestsHttpClient — synchronous HTTP client with retry policy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

from fis_monitor.domain.models import HttpResponse

__all__ = ["RequestsHttpClient"]

_log = logging.getLogger(__name__)

# User-Agent header constant
_USER_AGENT = "fis-monitor/1.0"

# Default timeout tuple: (connect, read)
_DEFAULT_TIMEOUT = (5.0, 30.0)

# Retry configuration
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = (1.0, 2.0)  # Sleep durations after attempts 1 and 2


class RequestsHttpClient:
    """Synchronous HTTP GET client with exponential backoff retry policy.

    Retry triggers on:
    - HTTP 5xx status codes
    - HTTP 429 (Too Many Requests)
    - requests.exceptions.ConnectionError
    - requests.exceptions.Timeout

    Does NOT retry on 4xx status codes (400, 403, 404, etc.).

    Implementation uses dependency injection for:
    - requests.Session (for thread-local session management)
    - sleep_fn (for testability of sleep behavior)
    """

    def __init__(
        self,
        session: requests.Session,
        sleep_fn: Callable[[float], None] = time.sleep,
        *,
        verify: bool = True,
        default_timeout: tuple[float, float] | None = None,
    ) -> None:
        """Initialize RequestsHttpClient with DI dependencies.

        Args:
            session: requests.Session instance to use for HTTP requests.
            sleep_fn: Callable[[float], None] for sleeping between retries.
                      Defaults to time.sleep for production, can be mocked in tests.
            verify: Whether to verify SSL certificates. Default True (safe for tests).
                    Set to False in composition for upstreams with self-signed certs
                    (ADR-024).
            default_timeout: (connect, read) timeout tuple. If None, uses module-level
                             _DEFAULT_TIMEOUT (5.0, 30.0).
        """
        self._session = session
        self._sleep_fn = sleep_fn
        self._verify = verify
        self._default_timeout = default_timeout or _DEFAULT_TIMEOUT

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """Perform a GET request with automatic retry on transient failures.

        Retries up to 3 attempts total with exponential backoff:
        - Attempt 1 (immediate)
        - Sleep 1.0s, Attempt 2
        - Sleep 2.0s, Attempt 3

        Args:
            url: The URL to request.
            params: Optional query parameters as a mapping.
            headers: Optional HTTP headers as a mapping.
            timeout: Optional scalar timeout in seconds. When provided, used as both
                     connect AND read timeout (symmetric). For asymmetric control,
                     pass None and rely on the default (5.0, 30.0).

        Returns:
            HttpResponse: Domain model with status, text, headers, final_url.
                         On terminal failure, returns the last response if available,
                         otherwise re-raises the last exception.

        Raises:
            RequestException: Re-raised if all retries exhausted and no response
                            was ever received.
        """
        # Normalize timeout to tuple format
        timeout_tuple = self._default_timeout if timeout is None else (timeout, timeout)

        # Prepare headers with User-Agent
        request_headers = dict(headers or {})
        if "User-Agent" not in request_headers:
            request_headers["User-Agent"] = _USER_AGENT

        last_response: requests.Response | None = None
        last_exception: RequestException | None = None

        for attempt_no in range(_RETRY_ATTEMPTS):
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=timeout_tuple,
                    verify=self._verify,
                )

                # Check if response status should trigger a retry
                if self._should_retry(response.status_code):
                    last_response = response
                    # Only sleep if not the last attempt
                    if attempt_no < _RETRY_ATTEMPTS - 1:
                        sleep_duration = _RETRY_BACKOFF[attempt_no]
                        _log.warning(
                            "HTTP retry after %d (attempt %d/%d)",
                            response.status_code,
                            attempt_no + 1,
                            _RETRY_ATTEMPTS,
                            extra={"url": url, "attempt": attempt_no + 1},
                        )
                        self._sleep_fn(sleep_duration)
                    continue

                # Success: return response
                return HttpResponse(
                    status=response.status_code,
                    text=response.text,
                    headers=dict(response.headers),
                    final_url=response.url,
                )

            except RequestException as e:
                # Only retry on specific exception types
                if self._should_retry_exception(e):
                    last_exception = e
                    # Only sleep if not the last attempt
                    if attempt_no < _RETRY_ATTEMPTS - 1:
                        sleep_duration = _RETRY_BACKOFF[attempt_no]
                        _log.warning(
                            "HTTP retry after %s (attempt %d/%d)",
                            type(e).__name__,
                            attempt_no + 1,
                            _RETRY_ATTEMPTS,
                            extra={"url": url, "attempt": attempt_no + 1},
                        )
                        self._sleep_fn(sleep_duration)
                    continue

                # Non-retryable exception: re-raise immediately
                raise

        # All retries exhausted
        if last_response is not None:
            # Return the last response (likely 5xx or 429)
            return HttpResponse(
                status=last_response.status_code,
                text=last_response.text,
                headers=dict(last_response.headers),
                final_url=last_response.url,
            )

        # All attempts failed with exceptions and no response was received
        if last_exception is not None:
            raise last_exception

        # Should not reach here, but safeguard
        raise RuntimeError("RequestsHttpClient.get() exhausted retries with no result")

    @staticmethod
    def _should_retry(status_code: int) -> bool:
        """Determine if a status code should trigger a retry.

        Retries on 5xx and 429.
        Does NOT retry on 4xx (400, 403, 404, etc.).

        Args:
            status_code: HTTP status code.

        Returns:
            True if the status code should trigger a retry, False otherwise.
        """
        return (500 <= status_code < 600) or status_code == 429

    @staticmethod
    def _should_retry_exception(exc: RequestException) -> bool:
        """Determine if an exception should trigger a retry.

        Retries on:
        - requests.exceptions.ConnectionError
        - requests.exceptions.Timeout

        Args:
            exc: The exception raised during the request.

        Returns:
            True if the exception should trigger a retry, False otherwise.
        """
        return isinstance(exc, (ConnectionError, Timeout))
