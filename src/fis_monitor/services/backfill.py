"""BackfillService — paginated catalogue backfill for cold-start or manual trigger.

Iterates all configured regions page by page, upserts every lot without SSE
notifications, and tracks progress so the HTTP layer can expose a status
endpoint.

Design invariants:
  - **Single-flight**: at most one backfill runs at a time.  A second ``start()``
    call returns immediately when one is already running (idempotent callers
    should use ``status().running`` to distinguish).
  - **Cancellable**: ``cancel()`` sets an internal stop-event; the running
    backfill exits within the ``sleep_between_pages`` interval.
  - **Thread-safe status**: progress counters are updated under a lock so
    ``status()`` always returns a consistent snapshot.
  - **Backfill skip-set**: before processing each region the service registers
    it in ``MonitorCycleService._regions_in_backfill``; ``run_forever`` skips
    those regions to prevent concurrent catalogue writes.

See docs/architecture (ADR for paginated backfill, to be written after review).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from fis_monitor.domain.models import (
    DEFAULT_TRACKED_FIELDS,
    SseLotNew,
)
from fis_monitor.domain.models import (
    lot_to_public_dto as _lot_to_public_dto,
)
from fis_monitor.domain.models import (
    parsed_row_to_lot as _parsed_row_to_lot,
)
from fis_monitor.domain.regions import subject_id_by_title as _subject_id_by_title

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import (
        ConfigSource,
        EventBus,
        LotRepository,
        PaginatedListFetcherProto,
    )

logger = logging.getLogger(__name__)

_DELTA_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Status snapshot (immutable, JSON-serialisable via dataclass)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillStatus:
    """Point-in-time snapshot of backfill progress.

    All fields are JSON-serialisable primitives so routes can return them
    directly without a separate Pydantic model.

    ``status`` is a string discriminant for the UI:
      ``"idle"``    — no backfill has run or none is currently active.
      ``"running"`` — a backfill thread is active right now.
      ``"done"``    — the most recent run completed (``running=False`` but
                      ``started_at`` is non-None).

    ``updated_at`` is the ISO-8601 UTC timestamp of the last progress update;
    ``None`` when no run has ever occurred.

    Note: ``lots_seen``, ``regions_done``, and ``total_pages_seen`` were removed
    in hs9c — they were dead code after hiq3 (UI reads only ``status``), and
    ``lots_seen`` was mis-counting on prod due to pagination drift (652 vs 412).
    """

    running: bool
    status: str  # "idle" | "running" | "done"
    current_region: int | None
    current_page: int | None
    regions_total: int
    started_at: str | None  # ISO-8601 UTC, or None when not running
    updated_at: str | None  # ISO-8601 UTC of last progress update


# ---------------------------------------------------------------------------
# Internal mutable progress state (guarded by BackfillService._lock)
# ---------------------------------------------------------------------------


@dataclass
class _Progress:
    running: bool = False
    current_region: int | None = None
    current_page: int | None = None
    regions_total: int = 0
    started_at: datetime | None = None
    updated_at: datetime | None = None
    done: bool = False  # True after a successful (non-cancelled) finish


# ---------------------------------------------------------------------------
# Protocol for MonitorCycleHandle (avoids direct import / circular dep)
# ---------------------------------------------------------------------------


class MonitorCycleHandle(Protocol):
    def mark_region_in_backfill(self, region: int) -> None: ...
    def clear_region_in_backfill(self, region: int) -> None: ...
    def request_run_now(self) -> None: ...


# ---------------------------------------------------------------------------
# Protocol for PaginatedListFetcher (avoids circular import in tests)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BackfillService
# ---------------------------------------------------------------------------


class BackfillService:
    """Single-flight paginated backfill across all configured regions.

    DI via constructor (keyword-only, same pattern as ``MonitorCycleService``).

    ``sleep_between_pages`` defaults to 2.0 s per the MVP rate-limit spec.
    Tests inject 0.0 for speed.
    """

    def __init__(
        self,
        *,
        fetcher: PaginatedListFetcherProto,
        lot_repo: LotRepository,
        config_source: ConfigSource,
        monitor_cycle: MonitorCycleHandle,
        event_bus: EventBus,
        sleep_between_pages: float = 2.0,
    ) -> None:
        self._fetcher = fetcher
        self._lot_repo = lot_repo
        self._config_source = config_source
        self._monitor_cycle = monitor_cycle
        self._event_bus = event_bus
        self._sleep_between_pages = sleep_between_pages

        # Single-flight lock: held while a backfill thread is running.
        # Used by start() to detect concurrent calls — it is NOT held for the
        # duration of the backfill (that would block status() / cancel()).
        self._flight_lock = threading.Lock()
        self._running = False  # guarded by _flight_lock

        # Progress state — guarded by _progress_lock (read-write).
        self._progress_lock = threading.Lock()
        self._progress = _Progress()

        # Internal stop-event; replaced each time start() begins a new run.
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        stop_event_external: threading.Event,
        regions: list[int] | None = None,
    ) -> bool:
        """Attempt to start a backfill in a daemon thread.

        Returns ``True`` immediately if a new backfill was started, ``False`` if
        one is already running (single-flight).

        ``regions``: subset of regions to backfill.  ``None`` = all configured
        regions (default behaviour).

        ``stop_event_external``: if set, the backfill will stop.  Merged with
        the internal stop-event so both ``cancel()`` and external shutdown abort
        the same backfill.
        """
        with self._flight_lock:
            if self._running:
                logger.debug(
                    "backfill.start.skip_running",
                    extra={
                        "regions_list": regions,
                        "current_running": True,
                    },
                )
                return False
            self._running = True
            # Fresh stop-event per run so a previous cancel() does not carry over.
            self._stop_event = threading.Event()

        logger.info(
            "backfill.start.entry",
            extra={
                "regions_list": regions,
                "single_flight_acquired": True,
            },
        )

        def _worker() -> None:
            try:
                self._run(stop_event_external, regions=regions)
            finally:
                with self._flight_lock:
                    self._running = False
                with self._progress_lock:
                    self._progress.running = False

        t = threading.Thread(target=_worker, daemon=True, name="backfill-worker")
        t.start()
        return True

    def maybe_start(
        self,
        region_id: int,
        site_total: int | None,
        db_count: int,
        stop_event: threading.Event,
        *,
        len_parsed_hint: int = 0,
    ) -> bool:
        """Conditionally start a backfill for a single region (delta-trigger gate).

        All checks are performed atomically inside ``_flight_lock`` to prevent
        TOCTOU races with concurrent callers.

        Returns ``True`` and fires backfill if triggered; ``False`` otherwise.
        """
        with self._flight_lock:
            threshold_computed = len_parsed_hint + _DELTA_THRESHOLD
            currently_running = self._running
            delta = (site_total - db_count) if site_total is not None else None

            logger.debug(
                "backfill.maybe_start.entry",
                extra={
                    "region_id": region_id,
                    "site_total": site_total,
                    "db_count": db_count,
                    "len_hint": len_parsed_hint,
                    "threshold_computed": threshold_computed,
                    "currently_running": currently_running,
                },
            )

            if site_total is None:
                logger.info(
                    "backfill.maybe_start.decision",
                    extra={
                        "region_id": region_id,
                        "decision": "skip_none",
                        "delta": None,
                        "threshold": threshold_computed,
                    },
                )
                return False

            if currently_running:
                logger.info(
                    "backfill.maybe_start.decision",
                    extra={
                        "region_id": region_id,
                        "decision": "skip_running",
                        "delta": delta,
                        "threshold": threshold_computed,
                    },
                )
                return False

            # delta is int here: site_total is not None (guarded above)
            assert delta is not None

            if delta < 0:
                logger.info(
                    "backfill.maybe_start.decision",
                    extra={
                        "region_id": region_id,
                        "decision": "skip_negative",
                        "delta": delta,
                        "threshold": threshold_computed,
                    },
                )
                return False

            if delta <= threshold_computed:
                logger.info(
                    "backfill.maybe_start.decision",
                    extra={
                        "region_id": region_id,
                        "decision": "skip_threshold",
                        "delta": delta,
                        "threshold": threshold_computed,
                    },
                )
                return False

            # Trigger: acquire the flight lock for real start.
            self._running = True
            self._stop_event = threading.Event()
            logger.info(
                "backfill.maybe_start.decision",
                extra={
                    "region_id": region_id,
                    "decision": "trigger",
                    "delta": delta,
                    "threshold": threshold_computed,
                },
            )
            logger.info(
                "backfill.delta_triggered",
                extra={
                    "region_id": region_id,
                    "delta": delta,
                    "threshold": threshold_computed,
                },
            )

        def _worker() -> None:
            try:
                self._run(stop_event, regions=[region_id])
            finally:
                with self._flight_lock:
                    self._running = False
                with self._progress_lock:
                    self._progress.running = False

        t = threading.Thread(target=_worker, daemon=True, name="backfill-worker")
        t.start()
        return True

    def status(self) -> BackfillStatus:
        """Return a consistent snapshot of current backfill progress."""
        with self._progress_lock:
            p = self._progress
            if p.running:
                status_str = "running"
            elif p.done:
                status_str = "done"
            else:
                status_str = "idle"
            return BackfillStatus(
                running=p.running,
                status=status_str,
                current_region=p.current_region,
                current_page=p.current_page,
                regions_total=p.regions_total,
                started_at=p.started_at.isoformat() if p.started_at else None,
                updated_at=p.updated_at.isoformat() if p.updated_at else None,
            )

    def cancel(self) -> None:
        """Cancel any running backfill.  Idempotent — safe to call when idle."""
        with self._flight_lock:
            was_running = self._running
            regions_in_flight: list[int] | None = None
        logger.info(
            "backfill.cancel.called",
            extra={
                "was_running": was_running,
                "regions_in_flight": regions_in_flight,
            },
        )
        self._stop_event.set()

    def is_running(self) -> bool:
        """Return ``True`` if a backfill is currently in progress."""
        with self._flight_lock:
            return self._running

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _combined_stop(self, external: threading.Event) -> threading.Event:
        """Return an event that fires when EITHER internal OR external stop fires.

        Implemented as a simple polling wrapper via a new Event + daemon thread.
        The overhead is negligible (one thread per backfill run).
        """
        combined = threading.Event()
        internal = self._stop_event

        def _watch() -> None:
            while not combined.is_set():
                if internal.is_set() or external.is_set():
                    combined.set()
                    return
                combined.wait(0.1)

        t = threading.Thread(target=_watch, daemon=True, name="backfill-stop-watcher")
        t.start()
        return combined

    def _run(
        self,
        stop_event_external: threading.Event,
        *,
        regions: list[int] | None = None,
    ) -> None:
        """Inner backfill loop — runs in the caller's thread."""
        stop = self._combined_stop(stop_event_external)

        if regions is None:
            settings = self._config_source.current()
            regions = list(settings.regions)
        now = datetime.now(UTC)

        with self._progress_lock:
            self._progress = _Progress(
                running=True,
                regions_total=len(regions),
                started_at=now,
                updated_at=now,
            )

        logger.info(
            "backfill.run.start",
            extra={"regions_list": regions, "regions_count": len(regions)},
        )

        for region in regions:
            if stop.is_set():
                logger.info(
                    "backfill.run.cancelled_before_region",
                    extra={"region_id": region},
                )
                break

            self._monitor_cycle.mark_region_in_backfill(region)
            try:
                self._process_region(region, stop)
            finally:
                self._monitor_cycle.clear_region_in_backfill(region)

            with self._progress_lock:
                self._progress.current_region = None
                self._progress.current_page = None

        # Mark done (normal completion only — cancel keeps done=False so the UI
        # can distinguish a successful finish from an interrupted one).
        # Check both the internal cancel event and the external stop event
        # directly — the combined `stop` event relies on a polling watcher that
        # may lag by up to 0.1 s, creating a TOCTOU window.
        cancelled = self._stop_event.is_set() or stop_event_external.is_set()
        if not cancelled:
            with self._progress_lock:
                self._progress.done = True
                self._progress.updated_at = datetime.now(UTC)
            try:
                self._monitor_cycle.request_run_now()
                logger.debug(
                    "backfill.request_run_now.invoked",
                    extra={"success": True, "error": None},
                )
            except Exception as exc:
                logger.warning("backfill: request_run_now() failed", exc_info=True)
                logger.debug(
                    "backfill.request_run_now.invoked",
                    extra={"success": False, "error": str(exc)},
                )

        # Signal the stop-watcher thread to exit on normal completion.
        # Without this, the watcher spins forever waiting for `combined` to be
        # set, since neither `internal` nor `external` is set on a clean finish.
        # Setting the internal stop-event here causes the watcher to detect it
        # and exit promptly.  Idempotent: safe if cancel() was already called.
        self._stop_event.set()
        logger.info("backfill.run.finished", extra={"cancelled": cancelled})

    def _process_region(self, region: int, stop: threading.Event) -> None:
        """Iterate all pages for ``region`` and upsert each lot."""
        region_start = time.monotonic()
        logger.info("backfill.region.start", extra={"region_id": region})

        with self._progress_lock:
            self._progress.current_region = region
            self._progress.current_page = None

        lots_upserted_per_page: dict[int, int] = {}
        current_page_num = 0

        def _on_page(page_num: int, items_count: int) -> None:
            nonlocal current_page_num
            current_page_num = page_num
            lots_upserted_per_page.setdefault(page_num, 0)
            with self._progress_lock:
                self._progress.current_page = page_num
                self._progress.updated_at = datetime.now(UTC)
            logger.debug(
                "backfill.region.page",
                extra={
                    "region_id": region,
                    "page_num": page_num,
                    "rows_fetched": items_count,
                    "lots_upserted": lots_upserted_per_page.get(page_num, 0),
                },
            )

        rows_processed = 0
        cancelled = False

        try:
            for row in self._fetcher.iterate(
                region,
                stop,
                sleep_between_pages=self._sleep_between_pages,
                per_page=20,  # ADR-036 updated 2026-05-16: reduced from 50 (timeout risk)
                page_callback=_on_page,
            ):
                if stop.is_set():
                    cancelled = True
                    break

                rows_processed += 1
                now = datetime.now(UTC)
                try:
                    lot = _parsed_row_to_lot(row, now, region_id=_subject_id_by_title(row.region))
                except Exception:
                    logger.warning(
                        "backfill: failed to convert row id=%s region=%s — skipping",
                        getattr(row, "id", "?"),
                        region,
                        exc_info=True,
                    )
                    continue

                try:
                    upsert_result = self._lot_repo.upsert(lot, tracked=DEFAULT_TRACKED_FIELDS)
                    lots_upserted_per_page[current_page_num] = (
                        lots_upserted_per_page.get(current_page_num, 0) + 1
                    )
                    # bd-bi7i: публикуем SseLotNew ТОЛЬКО для действительно новых
                    # лотов, как в monitor_cycle.py:597. Раньше backfill дёргал
                    # SSE на каждый upsert — фронт получал «новый лот» на
                    # исторические записи и запускал звуковую эскалацию
                    # (escalationStart → чип «Громче через …»). Семантически
                    # backfill — это исторический догон, не real-time событие.
                    if upsert_result.was_new and not stop.is_set():
                        try:
                            lot_dto = _lot_to_public_dto(lot)
                            self._event_bus.publish(
                                SseLotNew(lot=lot_dto, fragment_template="poster")
                            )
                        except Exception:
                            logger.warning(
                                "backfill: SseLotNew publish failed for lot id=%s — skipping",
                                lot.id,
                                exc_info=True,
                            )
                except Exception:
                    logger.warning(
                        "backfill: upsert failed for lot id=%s region=%s — skipping",
                        lot.id,
                        region,
                        exc_info=True,
                    )
                    continue

        except Exception:
            logger.error(
                "backfill.region.exception",
                exc_info=True,
                extra={"region_id": region, "page_num": current_page_num},
            )
            raise

        total_pages = len(lots_upserted_per_page)
        duration_ms = int((time.monotonic() - region_start) * 1000)
        logger.info(
            "backfill.region.finish",
            extra={
                "region_id": region,
                "total_rows": rows_processed,
                "total_pages": total_pages,
                "duration_ms": duration_ms,
                "cancelled": cancelled,
            },
        )
