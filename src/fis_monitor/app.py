"""FastAPI application factory + lifespan context manager.

Implements the three-phase shutdown flow from ADR-014 (extended with phase 1.5
for pw_executor / Playwright headed-login, R3-C3).

Phase overview
--------------
Phase 1  (graceful, grace_timeout=35s):
    ``supervisor.shutdown()`` — sets stop_event, joins supervised threads.
    Also signals the Dispatcher's own stop_event (a distinct threading.Event
    constructed inside NotifierDispatcher; the supervisor's stop_event is
    different — see design note below).

Phase 1.5 (Playwright headed-login teardown, R3-C3):
    1. ``login.cancel_active_job()`` — closes browser from outside worker.
    2. ``pw_executor.shutdown(wait=True, cancel_futures=True)`` wrapped in a
       daemon Thread + join(5.0) so a zombified Chromium process cannot block
       shutdown indefinitely (R4-M1).

Phase 2  (forceful executor shutdown):
    ``enrichment_pool.shutdown(wait=False, cancel_futures=True)``,
    ``sse_executor.shutdown(wait=False, cancel_futures=True)``,
    ``config_subscription.unsubscribe()``,
    ``conn_provider.close_all()`` (ONLY after phase 2 — ADR-014 invariant).

Phase 2b (config watchdog observer teardown, bye.9):
    ``config_source.stop()`` — stops the watchdog observer thread.

Phase 3  (lock release — always executes):
    ``locker.release(lock_handle)`` — must execute even if all previous phases
    raised, otherwise the next process start finds "Already running".

Design note — Dispatcher stop_event
------------------------------------
``NotifierDispatcher.consumer_loop()`` does NOT take a stop_event argument; it
uses ``self.stop_event`` (a threading.Event stored at construction time in
``composition.py``). The supervisor only manages ``full_scan``'s stop_event via
the shared ``ThreadSupervisor.stop_event``.

To stop the dispatcher thread, we call
``container.services.notifier_dispatcher.stop_event.set()`` explicitly in
lifespan (in addition to ``supervisor.shutdown()``) — without modifying the
Dispatcher's public API.

The ``notifier`` thread is started as::

    supervisor.start("notifier", lambda _e: container.services.notifier_dispatcher.consumer_loop())

so the supervisor's stop_event is ignored by the lambda; the real signal is the
dispatcher's own stop_event which we set in phase 1.

Injection seams
---------------
``create_app`` accepts factory overrides (``container_factory``,
``locker_factory``, ``executor_factory``) so tests can substitute fakes
without patching internals.  Production callers use the defaults.

Out-of-scope follow-ups (tracked as bd-issues)
-----------------------------------------------
- ``MonitorCycleService.run_forever`` does not exist (only ``run_cycle(region)``
  is defined).  A scheduler that calls ``run_cycle`` per configured region with
  ``stop_event.wait(poll_interval)`` is needed.  NOT supervised in this task.
- ``SessionMonitor`` is stubbed (``_NotImplementedSessionMonitor``); supervised
  loop deferred to ``a4t.9``.  NOT started here.
- ``EnrichmentService.bind_executor`` does not exist; it uses a per-call
  cycle-scoped executor inside ``enrich_lots()``.  ``enrichment_pool`` is
  created in lifespan for future use and stored in ``app.state`` but not bound
  to the service.  Follow-up required.

See: docs/architecture/04-composition-root.md §4.4, ADR-014.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fis_monitor.container import Container
from fis_monitor.domain.models import Settings
from fis_monitor.infra.clock import SystemClock
from fis_monitor.infra.lock import FileLocker
from fis_monitor.infra.thread_supervisor import ThreadSupervisor
from fis_monitor.utils.log import setup_logging
from fis_monitor.web.middleware import CsrfHostOriginMiddleware, loopback_csrf_config
from fis_monitor.web.onboarding_gate import OnboardingGateMiddleware
from fis_monitor.web.routes import auth, diagnostics, events, lots, notifications, onboarding
from fis_monitor.web.routes import settings as settings_routes
from fis_monitor.web.templates import STATIC_DIR, build_templates

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type alias for the container factory seam (tests substitute a fake)
# ---------------------------------------------------------------------------
ContainerFactory = Callable[[Settings | None, Path], Container]
LockerFactory = Callable[[Path], FileLocker]


def _default_locker_factory(data_dir: Path) -> FileLocker:
    return FileLocker(path=data_dir / "app.lock")


# ---------------------------------------------------------------------------
# Lazy proxy — resolves OnboardingService from app.state at request time
# ---------------------------------------------------------------------------


class _LazyOnboardingProxy:
    """Adapter, lazy over Container.  Implements OnboardingQuery Protocol.

    Middleware is constructed before the lifespan starts, so a direct reference
    to OnboardingService is not yet available.  The proxy holds a reference to
    the FastAPI instance and resolves the service on each request via
    ``app.state.container``.  By the time the first request arrives the lifespan
    has already run and populated ``app.state.container``.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def current(self) -> object:
        return self._app.state.container.services.onboarding.current()

    def url_for_current_step(self) -> str:
        return self._app.state.container.services.onboarding.url_for_current_step()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan_impl(
    app: FastAPI,
    *,
    settings: Settings | None,
    data_dir: Path,
    container_factory: ContainerFactory,
    locker_factory: LockerFactory,
):
    """Inner lifespan implementation — separated from ``create_app`` so
    ``container_factory`` / ``locker_factory`` seams are injectable by tests.

    All phases are wrapped in individual try/except blocks (R4-M4) so that
    a failure in one phase does not prevent subsequent phases from running.
    The lock release is in the outermost finally so it ALWAYS executes.
    """
    locker = locker_factory(data_dir)
    lock_handle = locker.acquire()
    logger.info("lifespan: lock acquired at %s", data_dir / "app.lock")

    # Pre-initialise shutdown-phase variables to None so the finally block can
    # guard with "if x is not None" even when startup fails mid-way.
    container = None
    pw_executor = None
    sse_executor = None
    enrichment_pool = None
    supervisor = None

    # Wrap ALL of startup + yield + shutdown in a try/finally keyed on the lock.
    # This guarantees the lock is released even if build_container or executor
    # creation raises (acceptance criterion: lock must not be left held on any
    # startup error path).
    try:
        # --- STARTUP -----------------------------------------------------------
        # Reconfigure logging for full runtime (stdout, INFO).
        # setup_logging is idempotent — replaces the bootstrap stderr handler.
        import os as _os
        import sys as _sys2

        _json_fmt = _os.getenv("LOG_JSON", "1") == "1"
        setup_logging(
            clock=SystemClock(),
            stream=_sys2.stdout,
            level=logging.INFO,
            json_format=_json_fmt,
        )

        container = container_factory(settings, data_dir)
        app.state.container = container

        # pw_executor: one-worker pool for Playwright headed-login (phase 1.5 R3-C3).
        pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw-login")
        container.services.login.bind_executor(pw_executor)  # j19 closed here
        app.state.pw_executor = pw_executor

        # sse_executor: SSE q.get — separate pool, not shared with FastAPI handlers.
        sse_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sse-wait")
        app.state.sse_executor = sse_executor

        # enrichment_pool: for future bind_executor on EnrichmentService (follow-up).
        # EnrichmentService.bind_executor does not exist yet; stored on app.state for
        # future use. TODO: bind once EnrichmentService.bind_executor is implemented.
        enrichment_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="enrich")
        app.state.enrichment_pool = enrichment_pool
        logger.info("lifespan: executor pools created (pw=1, sse=64, enrich=10)")

        # Supervised background threads.
        # NOTE: monitor_cycle (no run_forever) and session_monitor (stubbed) are
        # intentionally excluded — see module docstring for follow-up tracking.
        supervisor = ThreadSupervisor()
        supervisor.start("full-scan", container.services.full_scan.run_forever)
        # Dispatcher's consumer_loop() takes NO stop_event arg — it uses its own
        # self.stop_event.  We wrap it in a lambda that accepts the supervisor's
        # stop_event (which is ignored by the lambda) and call consumer_loop directly.
        # The dispatcher's stop_event is set explicitly in phase 1 shutdown below.
        supervisor.start(
            "notifier",
            lambda _stop_event: container.services.notifier_dispatcher.consumer_loop(),
        )
        app.state.supervisor = supervisor
        logger.info("lifespan: supervisor started (full-scan, notifier)")

        yield

    finally:
        # ------------------------------------------------------------------ #
        # SHUTDOWN — three phases + 1.5.                                       #
        # R4-M4: each phase in its own try/except.                             #
        # Lock release is in the outermost try/finally — ALWAYS executes.      #
        # ------------------------------------------------------------------ #
        try:
            # Phase 1: graceful shutdown of supervised threads (35s grace).
            # Also signal the Dispatcher's own stop_event so consumer_loop exits.
            if container is not None:
                try:
                    container.services.notifier_dispatcher.stop_event.set()
                except Exception:
                    logger.exception("phase 1: dispatcher stop_event.set() failed")

            if supervisor is not None:
                try:
                    report = supervisor.shutdown(grace_timeout=35.0)
                    if not report.clean:
                        logger.warning(
                            "lifespan: dangling threads at shutdown: %s", report.pending
                        )
                except Exception:
                    logger.exception("lifespan: phase 1 shutdown failed")

            # Phase 1.5: cancel active Playwright headed-login job (R3-C3).
            # LoginService.cancel_active_job() → browser.close() → worker exits.
            if container is not None:
                try:
                    container.services.login.cancel_active_job()
                except Exception:
                    logger.exception("lifespan: phase 1.5 login cancel failed")

            # R4-M1: pw_executor.shutdown(wait=True) may hang on zombie Chromium.
            # Wrap in Thread + join(5.0); on timeout log warning, continue shutdown.
            if pw_executor is not None:
                try:
                    _pw = pw_executor  # capture to avoid closure mutation
                    shutdown_thread = threading.Thread(
                        target=lambda: _pw.shutdown(wait=True, cancel_futures=True),
                        daemon=True,
                        name="pw-shutdown",
                    )
                    shutdown_thread.start()
                    shutdown_thread.join(timeout=5.0)
                    if shutdown_thread.is_alive():
                        logger.warning(
                            "lifespan: pw_executor.shutdown timed out; "
                            "Chromium may zombify until interpreter exit"
                        )
                except Exception:
                    logger.exception("lifespan: pw_executor shutdown failed")

            # Phase 2: forceful shutdown of remaining executors.
            if enrichment_pool is not None:
                try:
                    enrichment_pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    logger.exception("lifespan: enrichment_pool shutdown failed")

            if sse_executor is not None:
                try:
                    sse_executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    logger.exception("lifespan: sse_executor shutdown failed")

            # close_all connections — ONLY after phase 2 (ADR-014: otherwise
            # writers crash with SQLITE_MISUSE mid-commit).
            if container is not None:
                try:
                    container.infra.conn_provider.close_all()
                except Exception:
                    logger.exception("lifespan: conn_provider.close_all() failed")

            # Phase 2b: stop config watchdog observer (bye.9).
            if container is not None:
                try:
                    container.infra.config_source.stop()
                except Exception:
                    logger.exception("lifespan: config_source.stop() failed")

        finally:
            # Phase 3 / lock release — the outermost finally block.
            # Executes even if all phases above raised.
            # CRITICAL: without this, next startup hangs on "Already running".
            try:
                locker.release(lock_handle)
                logger.info("lifespan: lock released")
            except Exception:
                logger.exception(
                    "lifespan: lock release failed — manual cleanup may be required"
                )
            # Flush and close all logging handlers (SLO-L2: zero loss on shutdown).
            with contextlib.suppress(Exception):
                logging.shutdown()


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def create_app(
    data_dir: Path,
    settings: Settings | None = None,
    *,
    container_factory: ContainerFactory,
    locker_factory: LockerFactory = _default_locker_factory,
    port: int = 8000,
) -> FastAPI:
    """Create and return a FastAPI instance with lifespan, routes, and middleware.

    ``Container`` is stored in ``app.state.container``.  All seven routers are
    registered and both CSRF + OnboardingGate middleware are mounted here.

    ``container_factory`` is mandatory (no default) so that ``app.py`` does not
    import ``fis_monitor.composition`` — import-linter places them in the same
    tier and forbids cross-imports.  Production callers (e.g. an entrypoint
    module) import ``build_container`` from ``composition`` and pass it here.

    Args:
        data_dir:          Application data directory (used for DB, lock file).
        settings:          Optional pre-loaded settings (passes through to
                           ``container_factory``; ``None`` means factory reads
                           from DB / defaults).
        container_factory: Required DI seam — production code passes
                           ``composition.build_container``; tests pass a fake.
        locker_factory:    DI seam for tests — defaults to
                           ``FileLocker(data_dir / "app.lock")``.
        port:              TCP port the server will listen on.  Used to derive
                           loopback CSRF allow-lists.  Defaults to 8000.

    Returns:
        Configured ``FastAPI`` instance.  Start it with uvicorn or ASGI runner.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # type: ignore[misc]
        async with _lifespan_impl(
            app,
            settings=settings,
            data_dir=data_dir,
            container_factory=container_factory,
            locker_factory=locker_factory,
        ):
            yield

    app = FastAPI(lifespan=_lifespan)

    # --- Templates ---------------------------------------------------------
    app.state.templates = build_templates()

    # --- Static files ------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- Routers -----------------------------------------------------------
    for r in (
        lots.router,
        notifications.router,
        settings_routes.router,
        auth.router,
        onboarding.router,
        diagnostics.router,
        events.router,
    ):
        app.include_router(r)

    # --- Middleware (last add_middleware = outermost ASGI layer) ------------
    # OnboardingGate is inner (closer to route handlers); CSRF is outer.
    # _LazyOnboardingProxy defers Container lookup until request time, after
    # lifespan startup has populated app.state.container.
    app.add_middleware(OnboardingGateMiddleware, svc=_LazyOnboardingProxy(app))
    host_allowlist, origin_whitelist = loopback_csrf_config(port=port)
    # Store origin_whitelist on app.state so get_csrf_origin_whitelist() in
    # deps.py can serve it to the SSE events route without coupling to container.
    app.state.csrf_origin_whitelist = origin_whitelist
    app.add_middleware(
        CsrfHostOriginMiddleware,
        host_allowlist=host_allowlist,
        origin_whitelist=origin_whitelist,
    )

    return app


# ---------------------------------------------------------------------------
# Console-script entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point declared in pyproject.toml as ``fis-monitor``."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="fis-monitor",
        description="Дальневосточный гектар lot-monitoring service.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./var"),
        help="Application data directory (DB, lock file).  Created if absent.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8000, help="TCP port.")
    args = parser.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)

    # Bootstrap handler: captures catastrophic errors before lifespan startup
    # (e.g. build_container failures).  Uses stderr so it does not mix with
    # stdout in systemd journal.  Lifespan startup will replace it via the
    # idempotent setup_logging call.
    import os
    import sys as _sys

    _json = os.getenv("LOG_JSON", "1") == "1"
    setup_logging(
        clock=SystemClock(),
        stream=_sys.stderr,
        level=logging.WARNING,
        json_format=_json,
    )

    # importlib.import_module avoids a static import statement that would
    # violate the import-linter contract: app and composition are peers in the
    # same tier and cannot cross-import at the module level.
    import importlib

    composition = importlib.import_module("fis_monitor.composition")
    build_container = composition.build_container

    application = create_app(
        args.data_dir,
        container_factory=build_container,
        port=args.port,
    )
    uvicorn.run(application, host=args.host, port=args.port)
