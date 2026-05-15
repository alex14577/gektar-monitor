"""MonitorCycleService — list → enrich → upsert → notify pipeline.

One call to ``run_cycle(region)`` executes a complete monitor cycle:
  1. Open a ``cycles`` row (CyclesRepository).
  2. Fetch the lot-list page (HttpClient) and parse it (ListParser).
  3. Convert ``ParsedListRow`` → ``Lot`` (minimal, pending enrichment).
  4. Enrich lots in parallel (EnrichmentService).
  5. Upsert each lot (LotRepository); publish SSE / dispatch notifications for
     new lots.
  6. Close the ``cycles`` row with status and counters.

Exception handling follows docs/architecture/08-error-strategy.md:
  - ``UpstreamError``  → ErrorCategory mapped from ``category``; cycle "error";
    return CycleResult (do NOT re-raise).
  - ``ParseBugError``  → cycle "error" + SseCycleError; return CycleResult.
  - ``ParserVersionMismatch`` → log warning; skip affected lot(s); no cycle.error.
  - Any other ``Exception`` (bug) → cycle "error" + SseCycleError; **re-raise**.

PII contract:
  - ``CycleResult.error`` is hard-capped at 200 chars.
  - ``CycleResult.error`` MUST NOT contain stacktraces, URLs with tokens,
    recipient addresses, or credentials — only safe diagnostic strings.

``cycle_progress_signal`` (``threading.Event``) is ``set()`` before every slow
IO operation (HTTP fetch, enrichment) and ``clear()``'d after, so the supervisor
/ lifespan thread can detect liveness.

See docs/architecture/03-protocols.md §3 and docs/architecture/08-error-strategy.md.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from fis_monitor.domain.errors import (
    ParseBugError,
    ParserVersionMismatch,
    SessionExpiredError,
    UpstreamError,
)
from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    CyclesRepository,
    EventBus,
    FilterMatcher,
    HttpClient,
    ListParser,
    LotRepository,
)
from fis_monitor.domain.models import (
    DEFAULT_TRACKED_FIELDS,
    CycleResult,
    ErrorCategory,
    Lot,
    SseCycleError,
    SseSessionExpired,
)
from fis_monitor.domain.models import (
    lot_to_public_dto as _lot_to_public_dto,
)
from fis_monitor.domain.models import (
    parsed_row_to_lot as _parsed_row_to_lot,
)
from fis_monitor.infra.http.url_builder import PJAX_HEADERS as _PJAX_HEADERS
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.services.enrichment import EnrichmentService

if TYPE_CHECKING:
    from fis_monitor.services.notifier_dispatcher import NotifierDispatcher

logger = logging.getLogger(__name__)

# DEFAULT_TRACKED_FIELDS and _parsed_row_to_lot are re-exported from domain/models.py
# for backward-compat imports (e.g. tests that import from monitor_cycle directly).
# The canonical definitions live in fis_monitor.domain.models.
__all__ = ["DEFAULT_TRACKED_FIELDS", "MonitorCycleService"]

_DEFAULT_URL_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _upstream_to_error_category(cat: str) -> ErrorCategory:
    """Map ``UpstreamError.category`` to a valid ``ErrorCategory`` for SSE."""
    # ErrorCategory Literal values match UpstreamError.UpstreamCategory directly,
    # so the mapping is 1:1. Unknown values (future-proofing) fall back to "network".
    _valid: frozenset[str] = frozenset(
        ("network", "http_5xx", "http_4xx", "redirect_login", "timeout",
         "parse_bug", "schema_anomaly", "internal_error")
    )
    if cat in _valid:
        return cat  # type: ignore[return-value]
    return "network"  # type: ignore[return-value]


def _safe_error_str(exc: BaseException) -> str:
    """Return a PII-safe, 200-char-capped error string from an exception.

    Uses only the exception *type name* + a short, safe portion of the
    message.  Never includes URLs with tokens, recipient addresses, or
    credentials.  The 200-char cap matches ``CycleResult.error`` max_length.
    """
    type_name = type(exc).__name__
    # Take only the first 150 chars of the message to leave room for the type prefix.
    raw_msg = str(exc)[:150]
    combined = f"{type_name}: {raw_msg}"
    return combined[:200]


# ---------------------------------------------------------------------------
# MonitorCycleService
# ---------------------------------------------------------------------------

class MonitorCycleService:
    """Execute one monitor cycle: list → enrich → upsert → notify.

    All external dependencies are injected via constructor (DIP).  The service
    has no persistent state beyond what is stored in the repositories.
    """

    def __init__(
        self,
        *,
        http: HttpClient,
        list_parser: ListParser,
        enrichment: EnrichmentService,
        lot_repo: LotRepository,
        cycles_repo: CyclesRepository,
        notifier_dispatcher: NotifierDispatcher,
        event_bus: EventBus,
        config_source: ConfigSource,
        clock: Clock,
        cycle_progress_signal: threading.Event,
        filter_matcher: FilterMatcher,
        url_builder: TorgiUrlBuilder = _DEFAULT_URL_BUILDER,
        enrichment_workers: int = 4,
    ) -> None:
        self._http = http
        self._list_parser = list_parser
        self._enrichment = enrichment
        self._lot_repo = lot_repo
        self._cycles_repo = cycles_repo
        self._notifier_dispatcher = notifier_dispatcher
        self._event_bus = event_bus
        self._config_source = config_source
        self._clock = clock
        self.cycle_progress_signal = cycle_progress_signal
        self._filter_matcher = filter_matcher
        self._url_builder = url_builder
        self._enrichment_workers = enrichment_workers
        # Trigger queue: request_run_now() places a sentinel here; _wait_for_next_pass()
        # consumes it to wake the scheduler early.  maxsize=1 ensures at most one
        # pending sentinel exists; a second put_nowait raises queue.Full which is
        # suppressed in request_run_now(), giving idempotent coalescing semantics.
        self._trigger_q: queue.Queue[None] = queue.Queue(maxsize=1)
        # Backfill skip-set: regions currently being backfilled by BackfillService.
        # run_forever skips any region present in this set to prevent concurrent
        # catalogue writes.  Guarded by _backfill_lock.
        self._regions_in_backfill: set[int] = set()
        self._backfill_lock = threading.Lock()

    # Default poll interval used when ``interval_minutes`` is 0 (continuous mode).
    _DEFAULT_POLL_INTERVAL_SEC: float = 60.0
    # Backoff sleep (seconds) after an unexpected exception from run_cycle.
    _UNEXPECTED_BACKOFF_SEC: float = 5.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_region_in_backfill(self, region: int) -> None:
        """Register ``region`` as currently being backfilled.

        Called by ``BackfillService`` before iterating pages for a region.
        Thread-safe: acquires ``_backfill_lock``.
        """
        with self._backfill_lock:
            self._regions_in_backfill.add(region)

    def clear_region_in_backfill(self, region: int) -> None:
        """Deregister ``region`` from the backfill skip-set.

        Called by ``BackfillService`` in a ``finally`` block after finishing
        (or aborting) a region's backfill.  Idempotent — safe if ``region``
        was never added.  Thread-safe: acquires ``_backfill_lock``.
        """
        with self._backfill_lock:
            self._regions_in_backfill.discard(region)

    def request_run_now(self) -> None:
        """Wake the scheduler for an immediate pass (non-blocking, idempotent).

        Places a sentinel into ``_trigger_q`` so that ``_wait_for_next_pass``
        returns early on the next inter-pass sleep.  The queue is bounded to
        maxsize=1; if a sentinel is already pending (i.e. a previous
        request_run_now call has not been consumed yet), queue.Full is raised
        by put_nowait and suppressed — making this call a no-op.  A burst of
        triggers therefore collapses into exactly one extra pass.

        Thread-safe: may be called from any thread, including the ASGI request
        handler thread.
        """
        with contextlib.suppress(queue.Full):
            self._trigger_q.put_nowait(None)

    def run_forever(self, stop_event: threading.Event) -> None:
        """Scheduler loop: iterate configured regions, call run_cycle, sleep, repeat.

        Exits cleanly when ``stop_event`` is set (checked between regions and
        via ``stop_event.wait(poll_interval)`` between full passes).

        Exception handling:
          - ``UpstreamError`` / ``ParseBugError`` / ``ParserVersionMismatch`` from
            ``run_cycle`` are domain errors (data quality / upstream issues) — logged
            at WARNING level, no backoff.  Per run_cycle contract, domain errors may
            re-raise when they escape _run_cycle_inner on uncovered code paths —
            handled here so the loop survives.
          - Any other ``Exception`` (unexpected bug): logged at ERROR level,
            then a short backoff sleep before the next region.  The loop
            continues — one bad region must not kill the scheduler.
        """
        logger.info("monitor_cycle: scheduler started")

        while not stop_event.is_set():
            settings = self._config_source.current()
            regions = settings.regions

            # Poll interval: convert interval_minutes → seconds.
            # 0 means "continuous" — use the default backoff.
            poll_interval = (
                settings.interval_minutes * 60
                if settings.interval_minutes > 0
                else self._DEFAULT_POLL_INTERVAL_SEC
            )

            for region in regions:
                if stop_event.is_set():
                    logger.info(
                        "monitor_cycle: stop requested before region=%s — exiting",
                        region,
                    )
                    break

                with self._backfill_lock:
                    in_backfill = region in self._regions_in_backfill
                if in_backfill:
                    logger.debug(
                        "monitor_cycle: region=%s is being backfilled — skipping cycle",
                        region,
                    )
                    continue

                try:
                    self.run_cycle(region)
                except UpstreamError:
                    # Per run_cycle contract: domain errors may re-raise when they
                    # escape _run_cycle_inner — handled here so the loop survives.
                    logger.warning(
                        "monitor_cycle: UpstreamError escaped run_cycle for region=%s "
                        "(cycle row already closed); continuing loop",
                        region,
                        exc_info=True,
                    )
                except (ParseBugError, ParserVersionMismatch, SessionExpiredError):
                    # Per run_cycle contract: parse-domain errors and session expiry may
                    # re-raise when they escape _run_cycle_inner — data-quality or auth
                    # issue, not a bug in the loop itself.  Log at WARNING and continue.
                    logger.warning(
                        "monitor_cycle: parse/session domain error escaped run_cycle for region=%s "
                        "(cycle row already closed); continuing loop",
                        region,
                        exc_info=True,
                    )
                except Exception:
                    logger.error(
                        "monitor_cycle: unexpected exception in run_cycle for region=%s; "
                        "sleeping %.1fs before next region",
                        region,
                        self._UNEXPECTED_BACKOFF_SEC,
                        exc_info=True,
                    )
                    stop_event.wait(self._UNEXPECTED_BACKOFF_SEC)

            if stop_event.is_set():
                break

            logger.debug(
                "monitor_cycle: full pass done; sleeping %.0fs before next pass",
                poll_interval,
            )
            self._wait_for_next_pass(stop_event, poll_interval)

        logger.info("monitor_cycle: scheduler stopped")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    # Internal polling granularity for _wait_for_next_pass.
    # Caps the latency between stop_event.set() and the scheduler exiting.
    # Small enough for responsive shutdown; large enough to avoid busy-wait.
    _STOP_CHECK_INTERVAL_SEC: float = 0.5

    def _wait_for_next_pass(self, stop_event: threading.Event, timeout: float) -> None:
        """Wait up to ``timeout`` seconds, returning early on a trigger or stop.

        Because ``_trigger_q`` has maxsize=1, at most one sentinel can be
        pending at any time — no drain loop is needed after a successful get().
        A burst of ``request_run_now()`` calls still collapses into one extra
        pass because every call beyond the first is suppressed by queue.Full.

        Stop-event integration: the outer ``while not stop_event.is_set()`` loop
        in ``run_forever`` rechecks stop_event immediately after this method
        returns.  To keep shutdown latency bounded (the poll interval can be many
        minutes), we poll ``stop_event`` every ``_STOP_CHECK_INTERVAL_SEC`` using
        short ``queue.get()`` calls in a loop instead of one long blocking get.

        Args:
            stop_event: Scheduler stop signal — checked each polling slice.
            timeout:    Maximum seconds to wait (the configured poll interval).
        """
        deadline = time.monotonic() + timeout
        slice_sec = self._STOP_CHECK_INTERVAL_SEC

        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait = min(slice_sec, remaining)
            try:
                self._trigger_q.get(timeout=wait)
                # Trigger received — wake up immediately.
                # No drain needed: maxsize=1 guarantees the queue is now empty.
                return
            except queue.Empty:
                # No trigger in this slice — loop and recheck stop_event.
                continue

    def run_cycle(self, region: int) -> CycleResult:
        """Execute one monitor cycle for ``region``.

        Always returns a ``CycleResult`` — exceptions are reflected in
        ``status="error"`` and published as ``SseCycleError``.  Only
        unexpected bugs (non-domain exceptions) are re-raised after the
        cycle row is closed.

        See module docstring for the full exception-handling contract.
        """
        started_at = self._clock.now()
        cycle_id = self._cycles_repo.open(region=region, at=started_at)

        try:
            return self._run_cycle_inner(
                region=region,
                cycle_id=cycle_id,
                started_at=started_at,
            )
        except (UpstreamError, ParseBugError, ParserVersionMismatch, SessionExpiredError):
            # Already handled inside _run_cycle_inner; these should not escape.
            # If they do (e.g. from an uncovered code path), re-raise so the
            # supervisor's run_forever can backoff.
            raise
        except Exception as exc:
            self._close_with_unexpected_error(
                exc,
                cycle_id=cycle_id,
                region=region,
                started_at=started_at,
            )
            raise

    def _run_cycle_inner(
        self,
        *,
        region: int,
        cycle_id: int,
        started_at: datetime,
    ) -> CycleResult:
        """Inner cycle logic — called from ``run_cycle`` which wraps for unexpected errors."""

        # ---------- Step 2: fetch list page --------------------------------
        # ADR-036: head-poll — page=1 with per_page=20 (newest lots first).
        url = self._url_builder.lot_list_url(region=region, per_page=20)
        try:
            self.cycle_progress_signal.set()
            try:
                response = self._http.get(url, headers=_PJAX_HEADERS)
            finally:
                self.cycle_progress_signal.clear()
        except UpstreamError as exc:
            return self._close_with_upstream_error(
                exc, cycle_id=cycle_id, region=region, started_at=started_at
            )

        # ---------- Step 2b: parse list ------------------------------------
        try:
            parsed_page = self._list_parser.parse(response.text)
            parsed_rows = parsed_page.rows
        except SessionExpiredError:
            # Session cookie expired: site returned an ESIA login page instead of
            # lot-list DOM.  This is an auth failure, NOT a site DOM change.
            # Log at WARN (not ERROR) and return a zero-lots cycle so the loop
            # continues without raising an alert.
            logger.warning(
                "monitor_cycle: session expired for region=%s "
                "(ESIA login page detected) — closing cycle with session_expired",
                region,
            )
            # Publish SseSessionExpired so subscribers (UI modal + email notifier)
            # react to the transition (first publication per expiry epoch).
            self._event_bus.publish(
                SseSessionExpired(timestamp=self._clock.now())
            )
            finished_at = self._clock.now()
            result = CycleResult(
                id=cycle_id,
                region=region,
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                lots_fetched=0,
                new_lots=0,
                error="session_expired",
            )
            self._cycles_repo.close(cycle_id, result)
            return result
        except ParseBugError as exc:
            return self._close_with_parse_bug(
                exc, cycle_id=cycle_id, region=region, started_at=started_at
            )

        # ---------- Step 3: convert ParsedListRow → Lot -------------------
        now = self._clock.now()
        lots: list[Lot] = []
        try:
            for index, row in enumerate(parsed_rows):
                try:
                    lots.append(_parsed_row_to_lot(row, now))
                except ValidationError as exc:
                    raise ParseBugError(
                        selector="<list-row-conversion>",
                        context=f"row_index={index}",
                    ) from exc
        except ParseBugError as exc:
            return self._close_with_parse_bug(
                exc, cycle_id=cycle_id, region=region, started_at=started_at
            )

        # ---------- Step 4: enrich -----------------------------------------
        enriched_lots: list[Lot]
        try:
            self.cycle_progress_signal.set()
            try:
                enriched_lots = self._enrichment.enrich_lots(
                    lots, max_workers=self._enrichment_workers
                )
            finally:
                self.cycle_progress_signal.clear()
        except ParserVersionMismatch:
            # Lazy-reparse signal — log + skip enrichment; not a cycle error.
            # Full lazy-reparse flow is out of MVP scope.
            logger.warning(
                "ParserVersionMismatch during enrichment for region=%s; "
                "skipping enrichment for this cycle (lazy reparse deferred)",
                region,
            )
            enriched_lots = lots  # fall back to non-enriched lots

        # ---------- Step 5: upsert + notify --------------------------------
        # Step-5 re-reads config_source.current() so the filter snapshot can be
        # hot-reloaded mid-pass without waiting for the next scheduler iteration.
        # Note: run_forever reads its own snapshot at the top of each iteration
        # for `regions` and `interval_minutes`.  The two snapshots may differ if
        # a config reload occurs during a cycle — this is by-design: filters take
        # effect ASAP (within the current pass); scheduling parameters take effect
        # on the next iteration.  ConfigSource.current() is required to return an
        # in-memory cached snapshot (no I/O), so the extra fetch is O(1).
        current_filters = self._config_source.current().filters

        new_lots_count = 0
        for lot in enriched_lots:
            upsert_result = self._lot_repo.upsert(lot, tracked=DEFAULT_TRACKED_FIELDS)

            if upsert_result.was_new:
                new_lots_count += 1
                public_dto = _lot_to_public_dto(lot)
                if self._filter_matcher.matches(public_dto, current_filters):
                    self._notifier_dispatcher.dispatch(public_dto)
            elif upsert_result.changes:
                # Publish status update for changed lots (optional, best-effort).
                # Apply the same filter: status-change noise for filtered-out
                # regions is equally unwanted as new-lot noise.
                for change in upsert_result.changes:
                    if change.field == "status":
                        from fis_monitor.domain.models import SseLotStatus

                        lot_dto_for_filter = _lot_to_public_dto(lot)
                        if self._filter_matcher.matches(
                            lot_dto_for_filter, current_filters
                        ):
                            self._event_bus.publish(
                                SseLotStatus(
                                    lot_id=lot.id,
                                    new_status=str(change.new_value),
                                    event_type="changed",
                                )
                            )
                        break  # one SseLotStatus per lot per cycle is enough

        # ---------- Step 6: close cycle ------------------------------------
        finished_at = self._clock.now()
        result = CycleResult(
            id=cycle_id,
            region=region,
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            lots_fetched=len(enriched_lots),
            new_lots=new_lots_count,
            error=None,
        )
        self._cycles_repo.close(cycle_id, result)
        return result

    # ------------------------------------------------------------------
    # Private error-handling helpers
    # ------------------------------------------------------------------

    def _close_with_upstream_error(
        self,
        exc: UpstreamError,
        *,
        cycle_id: int,
        region: int,
        started_at: datetime,
    ) -> CycleResult:
        """Publish SseCycleError, close cycle, return CycleResult(status='error')."""
        error_category = _upstream_to_error_category(exc.category)
        self._event_bus.publish(
            SseCycleError(
                timestamp=self._clock.now(),
                cycle_id=cycle_id,
                error_category=error_category,
            )
        )
        error_str = _safe_error_str(exc)
        finished_at = self._clock.now()
        result = CycleResult(
            id=cycle_id,
            region=region,
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            lots_fetched=0,
            new_lots=0,
            error=error_str,
        )
        self._cycles_repo.close(cycle_id, result)
        return result

    def _close_with_parse_bug(
        self,
        exc: ParseBugError,
        *,
        cycle_id: int,
        region: int,
        started_at: datetime,
    ) -> CycleResult:
        """Publish SseCycleError(parse_bug), close cycle, return CycleResult(status='error')."""
        self._event_bus.publish(
            SseCycleError(
                timestamp=self._clock.now(),
                cycle_id=cycle_id,
                error_category="parse_bug",
            )
        )
        error_str = _safe_error_str(exc)
        finished_at = self._clock.now()
        result = CycleResult(
            id=cycle_id,
            region=region,
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            lots_fetched=0,
            new_lots=0,
            error=error_str,
        )
        self._cycles_repo.close(cycle_id, result)
        return result

    def _close_with_unexpected_error(
        self,
        exc: Exception,
        *,
        cycle_id: int,
        region: int,
        started_at: datetime,
        lots_fetched: int = 0,
        new_lots: int = 0,
    ) -> CycleResult:
        """Publish SseCycleError(internal_error), close cycle. Caller MUST re-raise ``exc``."""
        self._event_bus.publish(
            SseCycleError(
                timestamp=self._clock.now(),
                cycle_id=cycle_id,
                error_category="internal_error",
            )
        )
        error_str = _safe_error_str(exc)
        finished_at = self._clock.now()
        result = CycleResult(
            id=cycle_id,
            region=region,
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            lots_fetched=lots_fetched,
            new_lots=new_lots,
            error=error_str,
        )
        self._cycles_repo.close(cycle_id, result)
        return result
