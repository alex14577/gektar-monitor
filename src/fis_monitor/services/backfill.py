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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from fis_monitor.domain.models import DEFAULT_TRACKED_FIELDS
from fis_monitor.domain.models import parsed_row_to_lot as _parsed_row_to_lot

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import ConfigSource, LotRepository
    from fis_monitor.services.monitor_cycle import MonitorCycleService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status snapshot (immutable, JSON-serialisable via dataclass)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackfillStatus:
    """Point-in-time snapshot of backfill progress.

    All fields are JSON-serialisable primitives so routes can return them
    directly without a separate Pydantic model.
    """

    running: bool
    current_region: int | None
    current_page: int | None
    lots_seen: int
    regions_done: int
    regions_total: int
    started_at: str | None  # ISO-8601 UTC, or None when not running


# ---------------------------------------------------------------------------
# Internal mutable progress state (guarded by BackfillService._lock)
# ---------------------------------------------------------------------------

@dataclass
class _Progress:
    running: bool = False
    current_region: int | None = None
    current_page: int | None = None
    lots_seen: int = 0
    regions_done: int = 0
    regions_total: int = 0
    started_at: datetime | None = None


# ---------------------------------------------------------------------------
# Protocol for PaginatedListFetcher (avoids circular import in tests)
# ---------------------------------------------------------------------------

class _PaginatedListFetcherProto(Protocol):
    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float,
        subject_site_ids: tuple[int, ...] = (),
    ) -> object: ...  # returns Iterator[ParsedListRow]


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
        fetcher: _PaginatedListFetcherProto,
        lot_repo: LotRepository,
        config_source: ConfigSource,
        monitor_cycle: MonitorCycleService,
        sleep_between_pages: float = 2.0,
    ) -> None:
        self._fetcher = fetcher
        self._lot_repo = lot_repo
        self._config_source = config_source
        self._monitor_cycle = monitor_cycle
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

    def start(self, stop_event_external: threading.Event) -> bool:
        """Attempt to start a backfill in a daemon thread.

        Returns ``True`` immediately if a new backfill was started, ``False`` if
        one is already running (single-flight).

        Thread spawning is done INSIDE ``start()`` so callers can use the bool
        return value as the single-flight gate — no need for a TOCTOU-prone
        ``is_running()`` pre-check (P1-5).  The returned bool is race-free
        because the ``_flight_lock`` is held while checking and setting
        ``_running``.

        ``stop_event_external``: if set, the backfill will stop.  Merged with
        the internal stop-event so both ``cancel()`` and external shutdown abort
        the same backfill.
        """
        with self._flight_lock:
            if self._running:
                return False
            self._running = True
            # Fresh stop-event per run so a previous cancel() does not carry over.
            self._stop_event = threading.Event()

        def _worker() -> None:
            try:
                self._run(stop_event_external)
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
            return BackfillStatus(
                running=p.running,
                current_region=p.current_region,
                current_page=p.current_page,
                lots_seen=p.lots_seen,
                regions_done=p.regions_done,
                regions_total=p.regions_total,
                started_at=p.started_at.isoformat() if p.started_at else None,
            )

    def cancel(self) -> None:
        """Cancel any running backfill.  Idempotent — safe to call when idle."""
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

    def _run(self, stop_event_external: threading.Event) -> None:
        """Inner backfill loop — runs in the caller's thread."""
        stop = self._combined_stop(stop_event_external)

        settings = self._config_source.current()
        regions = list(settings.regions)
        now = datetime.now(UTC)

        with self._progress_lock:
            self._progress = _Progress(
                running=True,
                regions_total=len(regions),
                started_at=now,
            )

        logger.info("backfill: starting across %d region(s)", len(regions))

        for region in regions:
            if stop.is_set():
                logger.info("backfill: stop requested before region=%s", region)
                break

            self._monitor_cycle.mark_region_in_backfill(region)
            try:
                self._process_region(region, stop)
            finally:
                self._monitor_cycle.clear_region_in_backfill(region)

            with self._progress_lock:
                self._progress.regions_done += 1
                self._progress.current_region = None
                self._progress.current_page = None

        # Signal the stop-watcher thread to exit on normal completion.
        # Without this, the watcher spins forever waiting for `combined` to be
        # set, since neither `internal` nor `external` is set on a clean finish.
        # Setting the internal stop-event here causes the watcher to detect it
        # and exit promptly.  Idempotent: safe if cancel() was already called.
        self._stop_event.set()
        logger.info("backfill: finished")

    def _process_region(self, region: int, stop: threading.Event) -> None:
        """Iterate all pages for ``region`` and upsert each lot."""
        logger.info("backfill: processing region=%s", region)

        with self._progress_lock:
            self._progress.current_region = region
            self._progress.current_page = 1

        # Count rows processed for diagnostic logging.
        rows_processed = 0

        for row in self._fetcher.iterate(  # type: ignore[union-attr]
            region,
            stop,
            sleep_between_pages=self._sleep_between_pages,
        ):
            if stop.is_set():
                break

            rows_processed += 1
            now = datetime.now(UTC)
            try:
                lot = _parsed_row_to_lot(row, now)
            except Exception:
                logger.warning(
                    "backfill: failed to convert row id=%s region=%s — skipping",
                    getattr(row, "id", "?"),
                    region,
                    exc_info=True,
                )
                continue

            try:
                self._lot_repo.upsert(lot, tracked=DEFAULT_TRACKED_FIELDS)
            except Exception:
                logger.warning(
                    "backfill: upsert failed for lot id=%s region=%s — skipping",
                    lot.id,
                    region,
                    exc_info=True,
                )
                continue

            with self._progress_lock:
                self._progress.lots_seen += 1

        logger.info(
            "backfill: region=%s done, %d rows processed",
            region,
            rows_processed,
        )
