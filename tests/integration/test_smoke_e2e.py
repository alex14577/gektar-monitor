"""E2E smoke test for fis_monitor (bd task gektar_monitor-vgm.5).

Strategy (minimal manual Container — no DB, no Playwright):
  We build the app via create_app() with a fake container_factory that returns
  a hand-wired Container.  This avoids:
    - SQLite initialisation (init_db + migrations)
    - PlaywrightLoginSession import-time dependency
    - WatchdogConfigSource filesystem observer

  The test exercises the wiring that matters for the smoke:
    lifespan startup → ThreadEventBus → SseStreamer → SSE consumer receives event
    → graceful shutdown → lock released.

  OnboardingGateMiddleware checks svc.current() != "completed"; our FakeOnboarding
  returns OnboardingState.COMPLETED so the /events route is not redirected.

  get_sse_streamer is overridden via app.dependency_overrides even though
  container.infra.sse_streamer IS now a declared Infra dataclass field (fixed
  in ydj).  The override is kept because this smoke test uses a hand-wired
  Container built by _build_smoke_container(), not build_container().  The
  hand-wired Infra receives _UnusedStub for sse_streamer (it is never touched
  at runtime by the smoke), so we inject the real SseStreamer via dependency
  override to exercise the SSE path without going through the Infra field.

  SSE streaming approach:
    TestClient manages the ASGI lifespan synchronously (uses anyio in a
    background thread, which is the correct way to run blocking thread.join()
    without starving the asyncio event loop).  Inside the TestClient context,
    we run an asyncio event loop in a separate daemon thread to consume from
    SseStreamer.stream() directly.  This avoids the "async lifespan + blocking
    supervisor.shutdown()" deadlock where thread.join() would block the asyncio
    event loop that SSE futures depend on.

  test_smoke_e2e_error_path_lock_released uses the same TestClient pattern
  (no SSE consumption needed) to verify lock release on service-crash path.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from fis_monitor.app import create_app
from fis_monitor.container import Container, Infra, Services
from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    OnboardingState,
    Settings,
    SseLotNew,
)
from fis_monitor.infra.lock import FileLocker
from fis_monitor.infra.shutdown_cell import ShutdownRequesterCell
from fis_monitor.infra.sse.bus import ThreadEventBus
from fis_monitor.infra.sse.sse_stream import SseStreamer
from fis_monitor.web.deps import get_sse_streamer
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Fake helpers (test-only)
# ---------------------------------------------------------------------------


class _FakeConfigSource:
    """No-op ConfigSource."""

    def current_settings(self) -> Settings:
        return Settings()

    def stop(self) -> None:
        pass

    def subscribe(self, cb: Any) -> Any:
        class _NoopSub:
            def unsubscribe(self) -> None:
                pass

        return _NoopSub()


class _FakeConnProvider:
    """No-op connection provider."""

    def close_all(self) -> None:
        pass


class _FakeOnboarding:
    """Always reports COMPLETED so OnboardingGateMiddleware passes all routes."""

    def current(self) -> OnboardingState:
        return OnboardingState.COMPLETED

    def url_for_current_step(self) -> str:
        return "/"


class _FakeLogin:
    """Satisfies LoginService structural duck-type used in lifespan."""

    def bind_executor(self, executor: ThreadPoolExecutor) -> None:
        pass

    def cancel_active_job(self) -> None:
        pass

    def mark_browser_unavailable(self) -> None:
        pass


class _FakeNotifierDispatcher:
    """Satisfies NotifierDispatcher duck-type used in lifespan.

    stop_event exposed so app.py lifespan can call .stop_event.set().
    consumer_loop() blocks until stop_event is set.
    """

    def __init__(self) -> None:
        self.stop_event = threading.Event()

    def consumer_loop(self) -> None:
        self.stop_event.wait()


class _FakeRunForeverService:
    """Fake for monitor_cycle.

    Repeatedly publishes SseLotNew every 100ms until stop_event is set.
    Repeated publish avoids the race where the event fires before the SSE
    subscriber has connected.  Harmless extra events are dropped-from-tail.
    """

    def __init__(self, event_bus: ThreadEventBus, lot: LotPublicDTO) -> None:
        self._bus = event_bus
        self._lot = lot

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(timeout=0.1):
            self._bus.publish(SseLotNew(lot=self._lot, fragment_template="poster"))


class _FakeRaisingRunForeverService:
    """Fake service whose run_forever raises immediately.

    Used in the error-path test.  Exception propagates to the supervisor thread
    — intentional and expected.  pytest warns about unhandled thread exceptions
    but the critical assertion is lock release regardless.
    """

    def run_forever(self, stop_event: threading.Event) -> None:
        raise RuntimeError("fake crash in run_forever — expected in error-path smoke")


class _StubService:
    """Generic no-op stub for services not exercised by the smoke."""

    def run_forever(self, stop_event: threading.Event) -> None:
        stop_event.wait()

    def generate_bundle(self) -> object:
        raise NotImplementedError

    def recent_feed(self, *, limit: int) -> list:
        raise NotImplementedError

    def cancel(self) -> None:
        """No-op for BackfillService.cancel() called in lifespan shutdown."""

    def is_done(self) -> bool:
        """Stub: galka always set (no backfill resume needed in smoke tests)."""
        return True

    def start_resume(self, stop_event: object) -> bool:
        return True

    def bind_executor(self, executor: object) -> None:
        """No-op for EnrichmentService.bind_executor() called in lifespan (dmu)."""

    # SessionExpiredEmailService (dzm) — exposes stop_event + thread for lifespan
    stop_event = threading.Event()

    def start(self) -> None:
        """No-op for SessionExpiredEmailService.start() in lifespan."""

    def join(self, timeout: float | None = None) -> None:
        """No-op for SessionExpiredEmailService.join() in lifespan shutdown."""

    def consumer_loop(self) -> None:
        """No-op for SessionExpiredEmailService.consumer_loop() in supervised thread."""


class _StubLotRepo:
    """Minimal lot_repo for smoke: count_active() returns 1 so the lifespan
    auto-backfill path (triggered on empty DB) is skipped."""

    def count_active(self, region_ids: tuple[int, ...] = ()) -> int:
        return 1


# ---------------------------------------------------------------------------
# Container factory helpers
# ---------------------------------------------------------------------------


def _make_lot_public_dto() -> LotPublicDTO:
    """Build a minimal LotPublicDTO for the SseLotNew payload.

    Uses model_validate so a future Lot field unknown to LotPublicDTO raises
    cleanly at the validator (vs silently slipping through dict-unpack).
    """
    lot: Lot = make_lot()
    return LotPublicDTO.model_validate(
        {**lot.model_dump(), "age_seconds": 0, "tier": "silent", "freshness": "cold"}
    )


class _UnusedStub:
    """Sentinel for Infra fields the smoke test never touches at runtime.

    Any attribute access raises so a regression that *does* touch one of these
    fields fails loudly instead of silently working with a no-op.  The shared
    type: ignore on Infra(...) below is intentional and isolated — see comment.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"_UnusedStub.{name} — Infra field accessed but smoke wiring expected no access"
        )


def _build_smoke_container(
    event_bus: ThreadEventBus,
    *,
    use_raising_monitor: bool = False,
) -> Container:
    """Construct a hand-wired Container using only fakes (no DB, no Playwright).

    Typing note: Infra and Services declare Protocol/concrete types per field
    and our test stubs satisfy them structurally (duck-typing) but not
    statically.  A single ``type: ignore[arg-type]`` on the dataclass call is
    deliberate — narrowing each field individually would scatter ~30 ignores
    across the function body without adding safety.  Fields the smoke does NOT
    touch use ``_UnusedStub`` which raises on attribute access, so any future
    regression that *does* touch them fails loudly rather than silently.
    """
    lot_dto = _make_lot_public_dto()
    unused = _UnusedStub()

    class _StubLocker:
        def acquire(self) -> object:
            raise NotImplementedError

        def release(self, handle: object) -> None:
            pass

    class _StubSmtpHostPolicy:
        def resolve_and_check(self, host: str, port: int) -> object:
            raise NotImplementedError

    class _StubSmtpProviderCatalog:
        def lookup(self, email: str) -> object:
            return None

    class _StubSseStreamer:
        """Absorbs bind_executor() / bind_event_encoder() called by lifespan —
        smoke injects real SseStreamer via dependency_overrides, so this stub
        is never used for actual streaming."""

        def bind_executor(self, executor: object) -> None:
            pass  # lifespan binds executor; smoke overrides get_sse_streamer anyway

        def bind_event_encoder(self, encoder: object) -> None:
            pass  # lifespan wires HTML encoder; not needed for smoke stub

    infra = Infra(  # type: ignore[arg-type]
        clock=unused,
        event_bus=event_bus,
        conn_provider=_FakeConnProvider(),
        locker=_StubLocker(),
        config_source=_FakeConfigSource(),
        cycle_progress_signal=threading.Event(),
        lot_repo=_StubLotRepo(),
        user_state_repo=unused,
        settings_repo=unused,
        state_repo=unused,
        notif_repo=unused,
        cycles_repo=unused,
        smtp_creds_repo=unused,
        region_sub_repo=unused,
        http_client=unused,
        list_parser=unused,
        detail_parser=unused,
        login_session=unused,
        session_probe=unused,
        autostart=unused,
        smtp_host_policy=_StubSmtpHostPolicy(),
        smtp_provider_catalog=_StubSmtpProviderCatalog(),
        sse_streamer=_StubSseStreamer(),
    )

    if use_raising_monitor:
        monitor_svc = _FakeRaisingRunForeverService()  # type: ignore[assignment]
        full_scan_svc = _FakeRaisingRunForeverService()  # type: ignore[assignment]
    else:
        monitor_svc = _FakeRunForeverService(event_bus, lot_dto)  # type: ignore[assignment]
        full_scan_svc = _StubService()  # type: ignore[assignment]

    services = Services(  # type: ignore[arg-type]
        notifier_dispatcher=_FakeNotifierDispatcher(),
        monitor_cycle=monitor_svc,
        enrichment=_StubService(),
        full_scan=full_scan_svc,
        onboarding=_FakeOnboarding(),
        login=_FakeLogin(),
        settings_service=_StubService(),
        smtp_test=_StubService(),
        session_monitor=_StubService(),
        diagnostics=_StubService(),
        lot_query=_StubService(),
        lot_user_state=_StubService(),
        backfill=_StubService(),
        dnd=_StubService(),
        catchup_dismiss=_StubService(),
        session_expired_email=_StubService(),  # type: ignore[arg-type]
        license_expiry=_StubService(),  # type: ignore[arg-type]
        license_expiry_shutdown_cell=ShutdownRequesterCell(),
    )

    return Container(infra=infra, services=services)


# ---------------------------------------------------------------------------
# SSE async consumer helper
# ---------------------------------------------------------------------------


async def _collect_first_lot_new_event(
    streamer: SseStreamer,
    timeout: float = 6.0,
) -> list[str]:
    """Consume from streamer.stream() until a 'lot.new' event is received.

    Returns a list with the event type string (e.g. ["lot.new"]).
    Raises asyncio.TimeoutError if no event arrives within *timeout* seconds.
    """
    received: list[str] = []
    async with asyncio.timeout(timeout):
        async for chunk in streamer.stream():
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="replace")
            # SSE frames: "event: <type>\ndata: ...\n\n"
            lines = text.splitlines()
            event_type: str | None = None
            for line in lines:
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:") and event_type and event_type != "ping":
                    payload_str = line[len("data:"):].strip()
                    if payload_str:
                        payload = json.loads(payload_str)
                        actual = payload.get("event")
                        assert actual == event_type, (
                            f"event discriminator mismatch: {actual!r} != {event_type!r}"
                        )
                    received.append(event_type)
                    return received  # got it — exit generator cleanly
    return received  # unreachable if timeout fires (TimeoutError is raised)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_smoke_e2e_happy_path(tmp_path: Path) -> None:
    """Happy-path smoke: lifespan up → SSE event received → graceful shutdown → lock released.

    Verifies:
      1. TestClient lifespan starts without error (lifespan startup complete).
      2. SseStreamer.stream() delivers a 'lot.new' event from the fake monitor cycle.
      3. After TestClient.__exit__ (lifespan shutdown) a second FileLocker.acquire()
         on the same path succeeds — the lock was released.

    SSE streaming: an asyncio event loop runs in a background daemon thread that
    consumes from SseStreamer.stream() directly (not via HTTP).  TestClient is used
    solely for ASGI lifespan management (it runs its own anyio event loop in the
    main thread, keeping the blocking supervisor.shutdown() thread.join() calls off
    the asyncio event loop used for SSE consumption).
    """
    event_bus = ThreadEventBus()
    sse_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sse-smoke")

    streamer = SseStreamer(
        event_bus=event_bus,
        sse_executor=sse_executor,
        ping_interval=1.0,
    )

    def _container_factory(settings: Settings | None, data_dir: Path) -> Container:
        return _build_smoke_container(event_bus)

    lock_path = tmp_path / "app.lock"

    def _locker_factory(data_dir: Path) -> FileLocker:
        return FileLocker(path=lock_path)

    app = create_app(
        tmp_path,
        container_factory=_container_factory,
        locker_factory=_locker_factory,
        port=8765,
    )
    # get_sse_streamer override: smoke uses hand-wired Container (_build_smoke_container),
    # not build_container().  Infra.sse_streamer holds _UnusedStub here; we inject
    # the real SseStreamer via dependency override to exercise the SSE route.
    app.dependency_overrides[get_sse_streamer] = lambda: streamer

    # Result container shared between threads.
    received_events: list[str] = []
    consumer_exception: list[Exception] = []
    done_signal = threading.Event()

    def _run_consumer() -> None:
        """Run the async SSE consumer in its own event loop (daemon thread)."""
        try:
            result = asyncio.run(
                _collect_first_lot_new_event(streamer, timeout=6.0)
            )
            received_events.extend(result)
        except Exception as exc:
            consumer_exception.append(exc)
        finally:
            done_signal.set()

    try:
        with TestClient(app, raise_server_exceptions=True) as _client:
            # Lifespan startup completed.  fake monitor_cycle thread is running
            # and publishing SseLotNew events every 100ms.
            consumer_thread = threading.Thread(
                target=_run_consumer,
                daemon=True,
                name="sse-consumer-test",
            )
            consumer_thread.start()

            # Wait for SSE consumer to receive an event (max 8s).
            got = done_signal.wait(timeout=8.0)
            consumer_thread.join(timeout=2.0)

        # TestClient.__exit__: lifespan shutdown runs here.
    finally:
        sse_executor.shutdown(wait=False, cancel_futures=True)

    # Propagate consumer errors.
    if consumer_exception:
        raise AssertionError(
            f"SSE consumer raised: {consumer_exception[0]}"
        ) from consumer_exception[0]

    assert got, (
        "SSE consumer timed out — fake monitor_cycle should publish SseLotNew "
        "every 100ms and SseStreamer should deliver it to the stream"
    )
    assert received_events, "No SSE event received despite consumer completing"
    assert received_events[0] == "lot.new", f"Expected 'lot.new', got {received_events[0]!r}"

    # Lock release verification: a fresh FileLocker.acquire() must succeed after
    # the TestClient lifespan shutdown completed.
    # NOTE: in-process OS-lock tests cannot distinguish "released cleanly" from
    # "release was fail-suppressed in app.py phase 3 + fd garbage-collected on
    # thread exit".  The structural check (acquire succeeds) is the strongest
    # in-process assertion available.
    locker2 = FileLocker(path=lock_path)
    handle = locker2.acquire()
    locker2.release(handle)


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
def test_smoke_e2e_error_path_lock_released(tmp_path: Path) -> None:
    """Error-path smoke: fake monitor_cycle raises immediately.

    Verifies that even when supervised services crash at startup, the lifespan
    shutdown still completes and the lock is released (phase 3 always-executes
    invariant from app.py).

    No SSE consumption — just startup, shutdown, lock check.

    Note: pytest warns about PytestUnhandledThreadExceptionWarning for the
    crashing threads — this is expected and intentional.  The crash is the
    scenario under test.  The decorator above suppresses these warnings for
    the duration of this test only; any other thread-exception in this test
    will still surface in CI as a test failure if it changes the lock-release
    outcome (the assertion at the bottom is the source of truth).
    """
    event_bus = ThreadEventBus()
    sse_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sse-err-smoke")

    streamer = SseStreamer(
        event_bus=event_bus,
        sse_executor=sse_executor,
        ping_interval=1.0,
    )

    def _container_factory(settings: Settings | None, data_dir: Path) -> Container:
        return _build_smoke_container(event_bus, use_raising_monitor=True)

    lock_path = tmp_path / "app.lock"

    def _locker_factory(data_dir: Path) -> FileLocker:
        return FileLocker(path=lock_path)

    app = create_app(
        tmp_path,
        container_factory=_container_factory,
        locker_factory=_locker_factory,
        port=8766,
    )
    app.dependency_overrides[get_sse_streamer] = lambda: streamer

    try:
        # raise_server_exceptions=False: the supervised thread crashes (RuntimeError)
        # but this does not propagate to the main test — the supervisor joins
        # crashed threads cleanly.
        with TestClient(app, raise_server_exceptions=False) as _client:
            pass  # just start + stop lifespan; no requests needed
    finally:
        sse_executor.shutdown(wait=False, cancel_futures=True)

    # Lock MUST be released by shutdown phase 3 even on crash path.
    locker2 = FileLocker(path=lock_path)
    handle = locker2.acquire()
    locker2.release(handle)
