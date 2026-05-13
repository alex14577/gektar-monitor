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

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from fis_monitor.domain.errors import ParseBugError, ParserVersionMismatch, UpstreamError
from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    CyclesRepository,
    EventBus,
    HttpClient,
    ListParser,
    LotRepository,
)
from fis_monitor.domain.models import (
    CycleResult,
    ErrorCategory,
    Lot,
    LotPublicDTO,
    ParsedListRow,
    SseCycleError,
    SseLotNew,
    TrackedField,
)
from fis_monitor.services.enrichment import EnrichmentService

if TYPE_CHECKING:
    from fis_monitor.services.notifier_dispatcher import NotifierDispatcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DEFAULT_TRACKED_FIELDS — fields written to lots_history on every upsert.
# Derived from the TrackedField Literal; chosen as the minimal MVP set that
# drives notifications (status, area) and history queries (date_update).
# ---------------------------------------------------------------------------
DEFAULT_TRACKED_FIELDS: tuple[TrackedField, ...] = (
    "status",
    "area_sqm",
    "date_update",
    "is_active",
    "list_presence",
)

_LIST_URL_DEFAULT = (
    "https://torgi.gov.ru/new/public/lots/search"
    "?catCode=10&lotStatus=PUBLISHED&region={region}&page=0&size=100"
)


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


def _parsed_row_to_lot(row: ParsedListRow, now: datetime) -> Lot:
    """Construct a minimal ``Lot`` from a ``ParsedListRow``.

    Detail fields (lat, lon, raw_json, enrichment_status, …) are set to
    sensible defaults — ``EnrichmentService`` will fill them in.
    """
    return Lot(
        id=row.id,
        cadastral_no=row.cadastral_no,
        area_sqm=row.area_sqm,
        region=row.region,
        municipality=row.municipality,
        land_category=row.land_category,
        permitted_use=row.permitted_use,
        ogv=row.ogv,
        status=row.status,
        date_create=row.date_create,
        date_update=row.date_update,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=now,
        last_seen=now,
        detail_fetched_at=None,
        enrichment_status="pending",
        last_seen_at=now,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
    )


def _lot_to_public_dto(lot: Lot) -> LotPublicDTO:
    """Construct a ``LotPublicDTO`` from a ``Lot`` with default presentation hints.

    ``age_seconds``, ``tier``, and ``freshness`` are presentation hints that
    are computed properly by the web layer.  For the EventBus fan-out we use
    safe defaults — downstream consumers that need accurate tiers should
    recompute from the lot's timestamps.
    """
    return LotPublicDTO(
        **lot.model_dump(),
        age_seconds=0,
        tier="match",
        freshness="hot",
    )


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
        list_url_template: str = _LIST_URL_DEFAULT,
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
        self._list_url_template = list_url_template
        self._enrichment_workers = enrichment_workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        except (UpstreamError, ParseBugError, ParserVersionMismatch):
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
        url = self._list_url_template.format(region=region)
        try:
            self.cycle_progress_signal.set()
            try:
                response = self._http.get(url)
            finally:
                self.cycle_progress_signal.clear()
        except UpstreamError as exc:
            return self._close_with_upstream_error(
                exc, cycle_id=cycle_id, region=region, started_at=started_at
            )

        # ---------- Step 2b: parse list ------------------------------------
        try:
            parsed_rows = self._list_parser.parse(response.text)
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
        new_lots_count = 0
        for lot in enriched_lots:
            upsert_result = self._lot_repo.upsert(lot, tracked=DEFAULT_TRACKED_FIELDS)

            if upsert_result.was_new:
                new_lots_count += 1
                public_dto = _lot_to_public_dto(lot)
                self._event_bus.publish(
                    SseLotNew(lot=public_dto, fragment_template="poster")
                )
                self._notifier_dispatcher.dispatch(public_dto)
            elif upsert_result.changes:
                # Publish status update for changed lots (optional, best-effort).
                for change in upsert_result.changes:
                    if change.field == "status":
                        from fis_monitor.domain.models import SseLotStatus

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
