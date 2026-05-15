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

Pagination (updated):
  ``_fetch_region_ids`` now delegates to ``PaginatedListFetcher.iterate()``
  so the full catalogue is covered (not just page 1).  The fetcher's
  ``sleep_between_pages`` is 0.0 here because full_scan is a background job
  and the rate-limit concern is covered by the inter-batch sleep between
  active-lot batches — adding another sleep would double the scan time with
  no benefit.

  When ``paginated_fetcher`` is not supplied (backward-compat, tests), the
  service falls back to the old single-page ``_fetch_region_ids_single_page``
  implementation.

MVP limitations:
  - L2 active verification (``full_scan_l2_priority_days``) is not implemented.
  - No ``SseFullScanStarted`` event — event type not yet defined; run_once logs
    instead.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
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
from fis_monitor.infra.http.url_builder import PJAX_HEADERS as _PJAX_HEADERS
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder

if TYPE_CHECKING:
    from fis_monitor.services.paginated_list_fetcher import PaginatedListFetcher

logger = logging.getLogger(__name__)

_DEFAULT_URL_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")


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
        url_builder: TorgiUrlBuilder = _DEFAULT_URL_BUILDER,
        batch_size: int = 50,
        inter_batch_sleep_sec: float = 0.05,
        paginated_fetcher: PaginatedListFetcher | None = None,
    ) -> None:
        self._http = http
        self._list_parser = list_parser
        self._lot_repo = lot_repo
        self._cycles_repo = cycles_repo
        self._config_source = config_source
        self._clock = clock
        self._event_bus = event_bus
        self._cycle_progress_signal = cycle_progress_signal
        self._url_builder = url_builder
        self._batch_size = batch_size
        self._inter_batch_sleep_sec = inter_batch_sleep_sec
        # Optional paginated fetcher — when supplied, _fetch_region_ids iterates
        # all pages; when None, falls back to single-page (backward-compat).
        self._paginated_fetcher: PaginatedListFetcher | None = paginated_fetcher

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

        # Step 2 — collect seen ids from list pages (all pages per region via paginator).
        # Track region completeness: if pagination for any region fails mid-way,
        # mass-deactivation is suppressed for that run to avoid false-positive
        # mark_inactive calls (P1-4 bug fix).
        seen_ids: set[int] = set()
        all_regions_completed = True
        for region in settings.regions:
            region_ids, pagination_completed = self._fetch_region_ids(region, _stop)
            seen_ids.update(region_ids)
            if not pagination_completed:
                all_regions_completed = False
                logger.warning(
                    "full_scan: region=%s pagination incomplete — "
                    "mass-deactivation suppressed for this run to avoid false positives",
                    region,
                )

        # Step 3 — abort if ALL regions failed (seen_ids is empty).
        # This prevents false-positive mass-deactivation of all known lots.
        if not seen_ids:
            logger.warning(
                "full_scan: no lot ids collected from any region "
                "(all HTTP/parse errors?) — aborting to avoid mass deactivation"
            )
            return

        # Step 4 — iterate active lots in batches, mark seen / inactive.
        # mass_deactivation_enabled=False when any region had partial pagination.
        self._process_batches(
            seen_ids=seen_ids,
            mass_deactivation_enabled=all_regions_completed,
            now=now,
            stop_event=_stop,
        )

        logger.info("full_scan: run_once completed")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_region_ids(
        self, region: int, stop_event: threading.Event
    ) -> tuple[set[int], bool]:
        """Fetch all pages for ``region`` and return ``(ids, pagination_completed)``.

        ``pagination_completed`` is ``True`` when the iterator completed without
        exception (all pages visited).  ``False`` means the iteration was cut
        short by an error — callers must exclude this region from mass-
        deactivation (P1-4) to prevent false-positive mark_inactive calls.

        ``stop_event`` is propagated to ``_fetch_region_ids_paginated`` so that
        a shutdown signal during the paginated fetch exits the iterator promptly
        rather than waiting for all pages to complete (P1-2 fix).

        When a ``PaginatedListFetcher`` was supplied at construction time,
        all pages are iterated.  Otherwise falls back to single-page fetch
        (backward-compat for tests that pre-date the fetcher); single-page
        fetch always reports ``pagination_completed=True`` on success.
        """
        if self._paginated_fetcher is not None:
            return self._fetch_region_ids_paginated(region, stop_event)
        ids = self._fetch_region_ids_single_page(region)
        # Single-page path: treat empty set from HTTP error as incomplete,
        # non-empty set as completed (single-page guarantees full coverage).
        # Empty set on error is already logged inside _fetch_region_ids_single_page.
        return ids, True  # single page = always completed

    def _fetch_region_ids_paginated(
        self, region: int, stop_event: threading.Event
    ) -> tuple[set[int], bool]:
        """Fetch all pages via ``PaginatedListFetcher``; collect lot ids.

        Returns ``(ids, pagination_completed)`` where ``pagination_completed``
        is ``True`` only when iteration finished without exception.  An exception
        on any page yields ``False``, signalling the caller that the id-set is
        partial and must NOT be used for mass-deactivation (P1-4).

        ``stop_event`` is passed directly to ``iterate()`` so the paginator can
        abort mid-iteration on shutdown (P1-2).
        """
        assert self._paginated_fetcher is not None  # type narrowing
        ids: set[int] = set()
        pagination_completed = False
        try:
            for row in self._paginated_fetcher.iterate(
                region,
                stop_event,
                sleep_between_pages=0.0,  # full_scan paces via inter_batch_sleep_sec
            ):
                ids.add(row.id)
            # Iterator exhausted without exception — all pages visited.
            pagination_completed = True
        except Exception:
            # Exception mid-iteration → ids contains only pages fetched so far.
            # pagination_completed stays False → caller excludes this region
            # from mass-deactivation (P1-4).
            logger.warning(
                "full_scan: error during paginated fetch for region=%s — partial ids only",
                region,
                exc_info=True,
            )
        logger.debug(
            "full_scan: region=%s paginated fetch collected %d ids (completed=%s)",
            region,
            len(ids),
            pagination_completed,
        )
        return ids, pagination_completed

    def _fetch_region_ids_single_page(self, region: int) -> set[int]:
        """Fetch one list page for ``region`` and return the set of lot ids.

        Backward-compat fallback used when no ``PaginatedListFetcher`` is
        injected (tests, legacy wiring).
        """
        url = self._url_builder.lot_list_url(region=region)
        try:
            response = self._http.get(url, headers=_PJAX_HEADERS)
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
        logger.debug("full_scan: region=%s fetched %d ids (single page)", region, len(ids))
        return ids

    def _process_batches(
        self,
        *,
        seen_ids: set[int],
        mass_deactivation_enabled: bool,
        now: datetime,
        stop_event: threading.Event,
    ) -> None:
        """Paginate through active lots and mark each as seen or inactive.

        ``seen_ids`` — confirmed-sighted lot ids (used for mark_seen).
        ``mass_deactivation_enabled`` — when ``True``, lots absent from
            ``seen_ids`` are marked inactive.  Set to ``False`` when any
            region's pagination was incomplete (P1-4): we cannot confirm
            absence for those lots, so we skip deactivation entirely for
            this scan to prevent false-positive mark_inactive calls.

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

            if mass_deactivation_enabled:
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
