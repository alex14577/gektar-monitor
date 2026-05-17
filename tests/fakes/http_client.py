"""FakeHttpClient — canonical in-memory fake for the HttpClient Protocol.

See ADR-041 §Fake signature canon — single fake per Protocol.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from fis_monitor.domain.models import HttpResponse


class _RaiseMarker:
    """Sentinel stored in the response queue to trigger an exception on get()."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class FakeHttpClient:
    """Queue-of-responses fake for ``HttpClient``.

    Usage (normal responses)::

        client = FakeHttpClient([
            HttpResponse(
                status=200, final_url="https://example.test/cabinet/", text="", headers={}
            ),
        ])
        resp = client.get("https://example.test/cabinet/")

    Usage (exception)::

        client = FakeHttpClient()
        client.enqueue_error(OSError("network unreachable"))
        with pytest.raises(OSError):
            client.get("https://example.test/cabinet/")

    Responses are consumed in FIFO order.  Exhausting the queue raises
    ``AssertionError`` to prevent silent test gaps.

    ``calls`` records every ``url`` passed to ``get()`` so tests can
    assert the exact probe URL.
    """

    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self._queue: deque[HttpResponse | _RaiseMarker] = deque(
            responses or []
        )
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        if not self._queue:
            raise AssertionError(
                f"FakeHttpClient.get() called but response queue is empty (url={url!r})"
            )
        item = self._queue.popleft()
        if isinstance(item, _RaiseMarker):
            raise item.exc
        return item

    def enqueue(self, response: HttpResponse) -> None:
        """Append a normal response to the back of the queue."""
        self._queue.append(response)

    def enqueue_error(self, exc: BaseException) -> None:
        """Append an error marker; the next ``get()`` call will raise *exc*."""
        self._queue.append(_RaiseMarker(exc))
