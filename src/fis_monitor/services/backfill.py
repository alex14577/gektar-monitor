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

    ``total_pages_seen`` is the cumulative count of pages fetched across all
    regions in the current (or last) run.

    ``updated_at`` is the ISO-8601 UTC timestamp of the last progress update;
    ``None`` when no run has ever occurred.
    """

    running: bool
    status: str  # "idle" | "running" | "done"
    current_region: int | None
    current_page: int | None
    lots_seen: int
    regions_done: int
    regions_total: int
    total_pages_seen: int
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
    lots_seen: int = 0
    regions_done: int = 0
    regions_total: int = 0
    total_pages_seen: int = 0
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


class _PaginatedListFetcherProto(Protocol):
    def iterate(
        self,
        region: int,
        stop_event: threading.Event,
        *,
        sleep_between_pages: float,
        per_page: int | None = None,
        max_pages: int | None = None,
        page_callback: object = None,
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
        monitor_cycle: MonitorCycleHandle,
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
                return False
            self._running = True
            # Fresh stop-event per run so a previous cancel() does not carry over.
            self._stop_event = threading.Event()

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
            if site_total is None:
                logger.debug(
                    "maybe_start: region=%s site_total=None → skip", region_id
                )
                return False

            if self._running:
                logger.debug(
                    "maybe_start: region=%s already running → skip", region_id
                )
                return False

            delta = site_total - db_count

            if delta < 0:
                logger.debug(
                    "maybe_start: region=%s delta=%d decision=skip_negative",
                    region_id,
                    delta,
                )
                return False

            if delta <= len_parsed_hint + _DELTA_THRESHOLD:
                logger.debug(
                    "maybe_start: region=%s delta=%d hint=%d threshold=%d → below threshold",
                    region_id,
                    delta,
                    len_parsed_hint,
                    _DELTA_THRESHOLD,
                )
                return False

            # Trigger: acquire the flight lock for real start.
            self._running = True
            self._stop_event = threading.Event()
            logger.info(
                "backfill.delta_triggered",
                extra={
                    "region_id": region_id,
                    "delta": delta,
                    "threshold": len_parsed_hint + _DELTA_THRESHOLD,
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
                lots_seen=p.lots_seen,
                regions_done=p.regions_done,
                regions_total=p.regions_total,
                total_pages_seen=p.total_pages_seen,
                started_at=p.started_at.isoformat() if p.started_at else None,
                updated_at=p.updated_at.isoformat() if p.updated_at else None,
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
            except Exception:
                logger.warning("backfill: request_run_now() failed", exc_info=True)

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
            self._progress.current_page = None

        def _on_page(page_num: int, items_count: int) -> None:
            with self._progress_lock:
                self._progress.current_page = page_num
                self._progress.total_pages_seen += 1
                self._progress.updated_at = datetime.now(UTC)

        # Count rows processed for diagnostic logging.
        rows_processed = 0

        for row in self._fetcher.iterate(  # type: ignore[union-attr]
            region,
            stop,
            sleep_between_pages=self._sleep_between_pages,
            per_page=50,  # ADR-036: full walk with explicit page size
            page_callback=_on_page,
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
