"""Logging tests for SseStreamer DEBUG events (gektar_monitor-b9wq).

Covers:
- sse.subscribe (DEBUG — on stream() entry, with client_id + total_subscribers)
- sse.unsubscribe (DEBUG — on stream() exit, with client_id + reason)

These are tested via the _active_subscribers counter and log emission because
stream() is an async generator — we drive it via a simple async test helper.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from fis_monitor.infra.sse.bus import ThreadEventBus
from fis_monitor.infra.sse.sse_stream import SseStreamer

_LOGGER = "fis_monitor.infra.sse.sse_stream"
_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_emits_sse_subscribe_debug(caplog: pytest.LogCaptureFixture) -> None:
    """sse.subscribe emitted at DEBUG when a client connects (stream() entered)."""
    bus = ThreadEventBus()
    executor = ThreadPoolExecutor(max_workers=2)
    streamer = SseStreamer(event_bus=bus, ping_interval=0.05)
    streamer.bind_executor(executor)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        gen = streamer.stream()
        # Read just the first chunk (initial ping) then close.
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
        finally:
            await gen.aclose()

    executor.shutdown(wait=False)

    records = [r for r in caplog.records if r.getMessage() == "sse.subscribe"]
    assert records, "expected sse.subscribe debug event"
    rec = records[0]
    assert rec.__dict__.get("client_id") == 1
    assert "total_subscribers" in rec.__dict__


@pytest.mark.asyncio
async def test_stream_emits_sse_unsubscribe_on_close(caplog: pytest.LogCaptureFixture) -> None:
    """sse.unsubscribe emitted at DEBUG when stream() is closed (client disconnect)."""
    bus = ThreadEventBus()
    executor = ThreadPoolExecutor(max_workers=2)
    streamer = SseStreamer(event_bus=bus, ping_interval=0.05)
    streamer.bind_executor(executor)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        gen = streamer.stream()
        try:
            await gen.__anext__()  # initial ping
        except StopAsyncIteration:
            pass
        finally:
            await gen.aclose()

    executor.shutdown(wait=False)

    records = [r for r in caplog.records if r.getMessage() == "sse.unsubscribe"]
    assert records, "expected sse.unsubscribe debug event on disconnect"
    rec = records[0]
    assert rec.__dict__.get("client_id") == 1
    assert rec.__dict__.get("reason") == "disconnect"
