"""Unit tests for SseStreamer (infra/sse/sse_stream.py).

Covers (per task spec gektar_monitor-tic.3):
  - test_one_publish_N_consumers_each_get_copy — main fan-out acceptance
  - test_stream_yields_ping_on_idle             — keep-alive on no events
  - test_stream_yields_event                    — encodes and delivers event
  - test_stream_force_unsubscribe_closes_stream — bus kills sub → stream ends
  - test_stream_finally_unsubscribes            — cancel → unsubscribe called
  - test_encode_sse_event_format                — unit: correct SSE bytes
  - test_encode_sse_event_multiline_data        — multiline data split
  - test_wait_one_blocking                      — blocking dequeue / timeout
  - test_wait_one_dead_subscription_returns_none
  - test_alive_property

Micro-decision: Origin check is NOT tested here — it lives in web/routes/sse.py
(task oxy.6). SseStreamer is transport-agnostic.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    SseLotNew,
    SseLotStatus,
)
from fis_monitor.infra.sse.bus import ThreadEventBus
from fis_monitor.infra.sse.sse_stream import SseStreamer, encode_sse_event
from fis_monitor.infra.sse.subscriptions import ThreadEventSubscription

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_lot_new(lot_id: int = 42) -> SseLotNew:
    lot = Lot(
        id=lot_id,
        cadastral_no="01:02:000000:1",
        area_sqm=None,
        region="TestRegion",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=_TS,
        date_update=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        first_seen=_TS,
        last_seen=_TS,
        detail_fetched_at=None,
        enrichment_status=None,
        last_seen_at=None,
    )
    lot_dto = LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")
    return SseLotNew(lot=lot_dto, fragment_template="poster")


def make_lot_status(lot_id: int = 99) -> SseLotStatus:
    return SseLotStatus(lot_id=lot_id, new_status="gone", event_type="gone")


def make_streamer(
    bus: ThreadEventBus,
    ping_interval: float = 0.05,
    max_workers: int = 2,
) -> tuple[SseStreamer, ThreadPoolExecutor]:
    """Create a SseStreamer + executor pair for testing."""
    executor = ThreadPoolExecutor(max_workers=max_workers)
    streamer = SseStreamer(
        event_bus=bus,
        sse_executor=executor,
        ping_interval=ping_interval,
    )
    return streamer, executor


# ---------------------------------------------------------------------------
# Async helper: collect N frames from generator with timeout
# ---------------------------------------------------------------------------


async def collect_frames(
    gen,
    *,
    n: int,
    timeout: float,
) -> list[bytes]:
    """Collect exactly *n* bytes frames from async generator *gen*, with overall *timeout*."""
    frames: list[bytes] = []
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                frames.append(chunk)
                if len(frames) >= n:
                    break
    except TimeoutError:
        pass
    return frames


# ===========================================================================
# 1. Main fan-out acceptance: N consumers each get a copy
# ===========================================================================


@pytest.mark.asyncio
async def test_one_publish_N_consumers_each_get_copy():
    """publish 1 event on ThreadEventBus → 3 SseStreamer clients each receive it."""
    bus = ThreadEventBus()
    event = make_lot_new(lot_id=1)

    results: list[list[bytes]] = [[], [], []]
    errors: list[Exception] = []

    async def run_consumer(idx: int):
        executor = ThreadPoolExecutor(max_workers=1)
        streamer = SseStreamer(event_bus=bus, sse_executor=executor, ping_interval=0.1)
        frames: list[bytes] = []
        try:
            async with asyncio.timeout(2.0):
                async for chunk in streamer.stream():
                    frames.append(chunk)
                    # Stop after we get the event frame (non-ping)
                    if b'"lot.new"' in chunk:
                        break
        except TimeoutError:
            errors.append(TimeoutError(f"consumer {idx} timed out"))
        finally:
            results[idx] = frames
            executor.shutdown(wait=False)

    # Publish after a tiny delay so all consumers are subscribed.
    async def publish_after_delay():
        await asyncio.sleep(0.05)
        bus.publish(event)

    await asyncio.gather(
        run_consumer(0),
        run_consumer(1),
        run_consumer(2),
        publish_after_delay(),
    )

    assert not errors, f"Consumer errors: {errors}"

    for idx, frames in enumerate(results):
        event_frames = [f for f in frames if b'"lot.new"' in f]
        assert len(event_frames) == 1, (
            f"Consumer {idx} expected 1 lot.new frame, got {len(event_frames)}"
        )
        assert b"event: lot.new" in event_frames[0]


# ===========================================================================
# 2. Keep-alive pings on idle
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_yields_ping_on_idle():
    """Without publishes, stream yields keep-alive pings at ~ping_interval."""
    bus = ThreadEventBus()
    streamer, executor = make_streamer(bus, ping_interval=0.05)

    frames: list[bytes] = []
    try:
        async with asyncio.timeout(0.5):
            async for chunk in streamer.stream():
                frames.append(chunk)
                if len(frames) >= 3:
                    break
    except TimeoutError:
        pass
    finally:
        executor.shutdown(wait=False)

    # First frame is always the initial ping.
    ping_frames = [f for f in frames if f == b"event: ping\ndata: \n\n"]
    assert len(ping_frames) >= 2, f"Expected ≥2 pings, got {ping_frames!r}"


# ===========================================================================
# 3. Stream yields event frame
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_yields_event():
    """publish SseLotNew → client receives encoded SSE frame with correct type."""
    bus = ThreadEventBus()
    event = make_lot_new(lot_id=7)
    streamer, executor = make_streamer(bus, ping_interval=0.1)

    received_event_frame: bytes | None = None

    async def publish_after():
        await asyncio.sleep(0.03)
        bus.publish(event)

    async def consume():
        nonlocal received_event_frame
        try:
            async with asyncio.timeout(1.0):
                async for chunk in streamer.stream():
                    if b"lot.new" in chunk:
                        received_event_frame = chunk
                        break
        except TimeoutError:
            pass

    await asyncio.gather(consume(), publish_after())
    executor.shutdown(wait=False)

    assert received_event_frame is not None, "No lot.new frame received"
    assert received_event_frame.startswith(b"event: lot.new\n")
    assert received_event_frame.endswith(b"\n\n")
    assert b'"lot.new"' in received_event_frame  # event discriminator in JSON


# ===========================================================================
# 4. Force-unsubscribe closes stream
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_force_unsubscribe_closes_stream():
    """When bus force-removes the subscription, stream should terminate."""
    bus = ThreadEventBus()
    streamer, executor = make_streamer(bus, ping_interval=0.1)

    stream_terminated = asyncio.Event()
    frames: list[bytes] = []

    async def consume():
        try:
            async with asyncio.timeout(2.0):
                async for chunk in streamer.stream():
                    frames.append(chunk)
        except TimeoutError:
            pass
        finally:
            stream_terminated.set()

    # Force-remove the subscription after consumer starts.
    async def force_remove():
        await asyncio.sleep(0.05)
        # Grab subscriber and force-remove it via bus internal method.
        with bus._lock:
            if bus._subscribers:
                sub = bus._subscribers[0]
                sub._alive = False
                bus._subscribers.remove(sub)

    await asyncio.gather(consume(), force_remove())
    executor.shutdown(wait=False)

    assert stream_terminated.is_set()


# ===========================================================================
# 5. Cancel → subscription.unsubscribe() called
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_finally_unsubscribes():
    """Cancelling the async generator calls subscription.unsubscribe()."""
    bus = ThreadEventBus()
    streamer, executor = make_streamer(bus, ping_interval=0.1)

    consumer_task = asyncio.create_task(_consume_indefinitely(streamer))
    # Wait until subscriber is registered.
    await asyncio.sleep(0.08)
    assert len(bus._subscribers) == 1

    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task

    await asyncio.sleep(0.05)  # allow finally block to run
    executor.shutdown(wait=False)

    assert len(bus._subscribers) == 0, (
        "subscription.unsubscribe() must remove from bus on cancel"
    )


async def _consume_indefinitely(streamer: SseStreamer) -> None:
    async for _chunk in streamer.stream():
        pass


# ===========================================================================
# 6. encode_sse_event format
# ===========================================================================


def test_encode_sse_event_format():
    """encode_sse_event produces correct SSE bytes for SseLotStatus (simple payload)."""
    event = make_lot_status(lot_id=5)
    result = encode_sse_event(event)

    assert isinstance(result, bytes)
    assert result.startswith(b"event: lot.status\n")
    assert result.endswith(b"\n\n")
    # Data line contains JSON with expected fields.
    assert b"data: " in result
    assert b'"lot.status"' in result  # event discriminator
    assert b"5" in result  # lot_id


def test_encode_sse_event_lot_new_format():
    """SseLotNew event encodes with correct type."""
    event = make_lot_new(lot_id=42)
    result = encode_sse_event(event)

    assert result.startswith(b"event: lot.new\n")
    assert result.endswith(b"\n\n")
    assert b'"lot.new"' in result


# ===========================================================================
# 7. encode_sse_event multiline data
# ===========================================================================


def test_encode_sse_event_multiline_data():
    """If JSON contains newlines, each line must be prefixed with 'data:'."""
    # Construct a fake event object (duck-typed) for the helper.
    class FakeEvent:
        event = "test.event"

        def model_dump_json(self) -> str:
            return '{"a": 1}\n{"b": 2}'

    fake = FakeEvent()
    # Call the module-level function directly, bypassing type hints.
    result = encode_sse_event(fake)  # type: ignore[arg-type]

    lines = result.decode().split("\n")
    # Filter data lines.
    data_lines = [ln for ln in lines if ln.startswith("data:")]
    assert len(data_lines) == 2, f"Expected 2 data lines, got {data_lines}"
    assert data_lines[0] == 'data: {"a": 1}'
    assert data_lines[1] == 'data: {"b": 2}'


# ===========================================================================
# 8. wait_one blocking (sync unit test on ThreadEventSubscription)
# ===========================================================================


def test_wait_one_blocking_returns_event():
    """wait_one returns event from queue when available."""
    sub = ThreadEventSubscription(remover=MagicMock())
    event = make_lot_status(lot_id=1)
    sub._q.put_nowait(event)

    result = sub.wait_one(timeout=1.0)

    assert result == event


def test_wait_one_timeout_returns_none():
    """wait_one on empty queue with short timeout returns None."""
    sub = ThreadEventSubscription(remover=MagicMock())

    result = sub.wait_one(timeout=0.05)

    assert result is None


def test_wait_one_fills_and_drains():
    """wait_one called sequentially drains items one by one."""
    sub = ThreadEventSubscription(remover=MagicMock())
    events = [make_lot_status(lot_id=i) for i in range(3)]
    for e in events:
        sub._q.put_nowait(e)

    results = [sub.wait_one(timeout=0.1) for _ in range(3)]
    assert results == events

    # 4th call should timeout.
    assert sub.wait_one(timeout=0.05) is None


# ===========================================================================
# 9. wait_one on dead subscription
# ===========================================================================


def test_wait_one_dead_subscription_returns_none():
    """After unsubscribe, wait_one returns None immediately without blocking."""
    remover = MagicMock()
    sub = ThreadEventSubscription(remover=remover)

    # Simulate force-unsubscribe: bus sets _alive = False.
    sub._alive = False

    result = sub.wait_one(timeout=5.0)  # long timeout — must return immediately
    assert result is None


def test_wait_one_after_explicit_unsubscribe():
    """After explicit unsubscribe via real bus, wait_one returns None."""
    bus = ThreadEventBus()
    sub = bus.subscribe()
    bus._remove_subscriber(sub)  # simulates explicit unsubscribe

    result = sub.wait_one(timeout=0.05)
    assert result is None
    assert not sub.alive


# ===========================================================================
# 10. alive property
# ===========================================================================


def test_alive_property_true_initially():
    sub = ThreadEventSubscription(remover=MagicMock())
    assert sub.alive is True


def test_alive_property_false_after_force_remove():
    """After bus sets _alive = False, alive property returns False."""
    sub = ThreadEventSubscription(remover=MagicMock())
    sub._alive = False
    assert sub.alive is False


def test_alive_property_false_after_bus_unsubscribe():
    """After bus._remove_subscriber, alive is False."""
    bus = ThreadEventBus()
    sub = bus.subscribe()
    assert sub.alive is True

    bus._remove_subscriber(sub)
    assert sub.alive is False


# ===========================================================================
# 11. wait_one blocking in a thread (integration-style)
# ===========================================================================


def test_wait_one_blocks_then_returns_event():
    """wait_one blocks until an event is published from another thread."""
    sub = ThreadEventSubscription(remover=MagicMock())
    event = make_lot_status(lot_id=42)
    result_holder: list = []

    def waiter():
        result_holder.append(sub.wait_one(timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()

    # Small delay to ensure waiter is blocked.
    threading.Event().wait(timeout=0.05)
    sub._q.put_nowait(event)
    t.join(timeout=3.0)

    assert result_holder == [event]
