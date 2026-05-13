"""FullScanService — daily background loop for removal-detection (L1).

Runs once per day at the configured ``monitoring.full_scan_time`` (local TZ),
iterates ALL active lots in paginated batches, compares against the current
list-page snapshot, and marks lots absent from the listing as inactive.

Concurrency policy (docs/architecture/07-concurrency.md, ADR-005):
  - Lowest priority: monitor > enrichment > full_scan.
  - Batch commit every ``batch_size`` rows + ``inter_batch_sleep_sec`` sleep
    between batches — releases the SQLite writer-lock for higher-priority
    writers.
  - ``stop_event.wait(...)`` replaces ``time.sleep(...)`` everywhere — ensures
    sub-second shutdown response (R3-M2 invariant).

session_expired guard (docs/decisions-log.md):
  Checked on entry to ``run_once()``. If raised — run_once is a no-op.
  Implementation note: session_expired is stored in the ``state`` table under
  the key ``"session_expired"``; checking is done via ``SettingsRepository``
  that the caller optionally injects. For the MVP the guard is left as a
  pass-through hook — ``SettingsRepository`` injection is out of scope for
  this task (tracked separately).

MVP limitations:
  - Single page per region (full pagination is out of scope, see run_once).
  - L2 active verification (``full_scan_l2_priority_days``) is not implemented.
  - No ``SseFullScanStarted`` event — event type not yet defined; run_once logs
    instead.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fis_monitor.domain.errors import ParseBugError, UpstreamError
from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    CyclesRepository,
    EventBus,
    HttpClient,
    ListParser,
    LotRepository,
)

logger = logging.getLogger(__name__)

# Same default URL as MonitorCycleService (same list endpoint, same region param).
_LIST_URL_DEFAULT = (
    "https://torgi.gov.ru/new/public/lots/search"
    "?catCode=10&lotStatus=PUBLISHED&region={region}&page=0&size=100"
)


def _next_scheduled_datetime(
    full_scan_time: str,
    timezone: str,
    now_utc: datetime,
) -> datetime:
    """Return the next wall-clock datetime (UTC-aware) for the scheduled scan.

    Parses ``full_scan_time`` as ``"HH:MM"`` in the ``timezone`` local time.
    If today's scheduled run is in the past (or within 5 seconds), returns
    tomorrow's run.

    Falls back to UTC if ``timezone`` is unknown (ZoneInfoNotFoundError).
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        logger.warning(
            "full_scan: unknown timezone %r, falling back to UTC", timezone
        )
        tz = ZoneInfo("UTC")

    try:
        hour_str, minute_str = full_scan_time.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except (ValueError, AttributeError):
        logger.warning(
            "full_scan: cannot parse full_scan_time=%r, defaulting to 04:00",
            full_scan_time,
        )
        hour, minute = 4, 0

    now_local = now_utc.astimezone(tz)
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If today's slot is in the past (with 5 s tolerance), advance to tomorrow.
    if candidate <= now_local + timedelta(seconds=5):
        candidate += timedelta(days=1)

    return candidate.astimezone(UTC)


class FullScanService:
    """Daily full-scan for removal detection.

    Injected dependencies follow the same keyword-only DI pattern as
    ``MonitorCycleService`` (docs/architecture/04-composition-root.md §4.2).

    ``cycles_repo`` is accepted for forward-compatibility with future cycle
    tracking for full-scan runs; it is not used in this MVP but is wired in
    ``build_container()`` per the canonical constructor shape.
    """

    def __init__(
        self,
        *,
        http: HttpClient,
        list_parser: ListParser,
        lot_repo: LotRepository,
        cycles_repo: CyclesRepository,
        config_source: ConfigSource,
        clock: Clock,
        event_bus: EventBus,
        cycle_progress_signal: threading.Event,
        list_url_template: str = _LIST_URL_DEFAULT,
        batch_size: int = 50,
        inter_batch_sleep_sec: float = 0.05,
    ) -> None:
        self._http = http
        self._list_parser = list_parser
        self._lot_repo = lot_repo
        self._cycles_repo = cycles_repo
        self._config_source = config_source
        self._clock = clock
        self._event_bus = event_bus
        self._cycle_progress_signal = cycle_progress_signal
        self._list_url_template = list_url_template
        self._batch_size = batch_size
        self._inter_batch_sleep_sec = inter_batch_sleep_sec

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_forever(self, stop_event: threading.Event) -> None:
        """Scheduler loop: sleep until next scan time, run, repeat.

        Exits cleanly when ``stop_event`` is set (checked on each wait).
        Propagates ``stop_event`` into ``run_once`` so the batch loop can
        respond to shutdown mid-scan without waiting for the full scan to finish.
        """
        logger.info("full_scan: scheduler started")
        while not stop_event.is_set():
            settings = self._config_source.current()
            now = self._clock.now()
            next_run = _next_scheduled_datetime(
                full_scan_time=settings.monitoring.full_scan_time,
                timezone=settings.timezone,
                now_utc=now,
            )
            delay = (next_run - now).total_seconds()
            logger.info(
                "full_scan: next run scheduled at %s (in %.0f s)",
                next_run.isoformat(),
                max(0.0, delay),
            )

            # Sleep until the scheduled time or until shutdown is requested.
            stop_event.wait(max(0.0, delay))

            if stop_event.is_set():
                break

            # Run the scan; propagate stop_event so the batch loop can abort
            # early on shutdown rather than waiting for all batches to complete.
            try:
                self.run_once(stop_event=stop_event)
            except Exception:
                logger.exception("full_scan: run_once crashed; continuing scheduler")

        logger.info("full_scan: scheduler stopped")

    def run_once(self, *, stop_event: threading.Event | None = None) -> None:
        """Execute one full scan across all configured regions.

        Parameters
        ----------
        stop_event:
            Optional shutdown signal.  When set mid-scan the batch loop aborts
            early (no further batches are processed).  If omitted (e.g. when
            called manually from tests), a never-set sentinel is used so the
            method behaves identically to the old signature.

        Steps:
          1. Read current Settings (config snapshot for this run).
          2. Fetch one list page per region, collect all seen lot-ids.
          3. If no ids were collected (all regions failed) — abort to avoid
             false-positive mass-deactivation.
          4. Iterate active lots in batches; mark_seen / mark_inactive per lot.
        """
        # MVP: no SseFullScanStarted event (type not yet defined). Log instead.
        logger.info("full_scan: run_once started")

        # If no stop_event was provided (e.g. manual/test call), use a
        # never-set sentinel so _process_batches always has a real Event.
        _stop = stop_event if stop_event is not None else threading.Event()

        # Step 1 — snapshot config once (immutable for this run).
        settings = self._config_source.current()
        now = self._clock.now()

        # Step 2 — collect seen ids from list pages (one page per region).
        seen_ids: set[int] = set()
        for region in settings.regions:
            region_ids = self._fetch_region_ids(region)
            seen_ids.update(region_ids)

        # Step 3 — abort if ALL regions failed (seen_ids is empty).
        # This prevents false-positive mass-deactivation of all known lots.
        if not seen_ids:
            logger.warning(
                "full_scan: no lot ids collected from any region "
                "(all HTTP/parse errors?) — aborting to avoid mass deactivation"
            )
            return

        # Step 4 — iterate active lots in batches, mark seen / inactive.
        self._process_batches(seen_ids=seen_ids, now=now, stop_event=_stop)

        logger.info("full_scan: run_once completed")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_region_ids(self, region: int) -> set[int]:
        """Fetch one list page for ``region`` and return the set of lot ids.

        Returns an empty set on any error (HTTP or parse), so the caller can
        decide whether to proceed.

        MVP: single page; full pagination is out of scope.
        """
        url = self._list_url_template.format(region=region)
        try:
            response = self._http.get(url)
        except UpstreamError:
            logger.warning(
                "full_scan: HTTP error fetching region=%s — skipping region",
                region,
                exc_info=True,
            )
            return set()
        except Exception:
            logger.warning(
                "full_scan: unexpected error fetching region=%s — skipping region",
                region,
                exc_info=True,
            )
            return set()

        try:
            rows = self._list_parser.parse(response.text)
        except ParseBugError:
            logger.warning(
                "full_scan: parse error for region=%s — skipping region",
                region,
                exc_info=True,
            )
            return set()
        except Exception:
            logger.warning(
                "full_scan: unexpected parse error for region=%s — skipping region",
                region,
                exc_info=True,
            )
            return set()

        ids = {row.id for row in rows}
        logger.debug("full_scan: region=%s fetched %d ids", region, len(ids))
        return ids

    def _process_batches(
        self,
        *,
        seen_ids: set[int],
        now: datetime,
        stop_event: threading.Event,
    ) -> None:
        """Paginate through active lots and mark each as seen or inactive.

        ``stop_event`` is required (never None).  ``run_once`` always passes
        either the caller-supplied event or a never-set sentinel, so this
        invariant is guaranteed at the call site.

        Shutdown is checked at the **top** of every iteration (before fetching
        the next batch) and again via ``stop_event.wait(inter_batch_sleep_sec)``
        between batches.  This guarantees sub-second shutdown response
        regardless of batch size (R3-M2 invariant, B1 fix).
        """
        offset = 0
        while True:
            if stop_event.is_set():
                logger.info(
                    "full_scan: stop_event set, aborting batch loop at offset=%d",
                    offset,
                )
                return

            batch = self._lot_repo.list_active(
                limit=self._batch_size, offset=offset
            )
            if not batch:
                break

            seen_in_batch = [lot.id for lot in batch if lot.id in seen_ids]
            missing_in_batch = [lot.id for lot in batch if lot.id not in seen_ids]

            if seen_in_batch:
                self._lot_repo.mark_seen(seen_in_batch, now)

            for lot_id in missing_in_batch:
                logger.info(
                    "full_scan: marking lot %d inactive (reason=full_scan_missing)",
                    lot_id,
                )
                self._lot_repo.mark_inactive(
                    lot_id, reason="full_scan_missing", at=now
                )

            offset += self._batch_size

            # Yield write-lock between batches (ADR-005 / docs/architecture/07-concurrency.md).
            # Use stop_event.wait instead of time.sleep for shutdown responsiveness (R3-M2).
            if stop_event.wait(self._inter_batch_sleep_sec):
                logger.info("full_scan: stop requested mid-batch, aborting")
                return
