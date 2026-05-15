"""EnrichmentService — parallel detail-page enrichment via Executor.

Enriches a list of lots (produced by the list-stage) by fetching each
lot's detail page and merging the parsed result into the lot.

Design invariants:
- Executor DI: an ``Executor`` is injected via constructor.  The caller
  (composition root / lifespan) owns the executor lifecycle — EnrichmentService
  does NOT shut it down.  When no executor is provided a per-call scoped
  ``ThreadPoolExecutor(max_workers=max_workers)`` is created as a fallback for
  backward compatibility and tests that do not care about executor lifecycle.
- Per-lot exception isolation: any Exception (except ParserVersionMismatch)
  is caught, logged at WARNING, and the original lot is returned with
  ``enrichment_status='failed'``.
- ParserVersionMismatch propagates unmodified — caller (MonitorCycleService)
  decides whether to reparse or drop.
- max_workers is ignored when an executor is provided via constructor; it is
  honoured when no executor was injected (per-call fallback creation).
- Order of returned lots matches input order.

See docs/architecture/03-protocols.md §3.2 for HttpClient / DetailParser
contracts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import UTC, datetime

from fis_monitor.domain.errors import ParserVersionMismatch
from fis_monitor.domain.interfaces import DetailParser, HttpClient
from fis_monitor.domain.models import Lot, ParsedDetail
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder

logger = logging.getLogger(__name__)

_DEFAULT_URL_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")


class EnrichmentService:
    """Fetch and merge detail-page data into lots in parallel.

    Dependencies injected via constructor (DIP):
    - ``http``: ``HttpClient`` — synchronous HTTP GET seam.
    - ``parser``: ``DetailParser`` — parses detail-card HTML into ``ParsedDetail``.
    - ``url_builder``: ``TorgiUrlBuilder`` — composes detail-page URLs from lot_id.
    - ``executor``: ``concurrent.futures.Executor`` — thread pool for parallel
      enrichment.  Lifecycle (shutdown) is the **caller's** responsibility.
      When ``None``, a per-call scoped ``ThreadPoolExecutor`` is created inside
      ``enrich_lots`` (backward-compat / test convenience).
    """

    def __init__(
        self,
        http: HttpClient,
        parser: DetailParser,
        *,
        url_builder: TorgiUrlBuilder = _DEFAULT_URL_BUILDER,
        executor: Executor | None = None,
    ) -> None:
        self._http = http
        self._parser = parser
        self._url_builder = url_builder
        self._executor: Executor | None = executor

    # ------------------------------------------------------------------
    # Lifecycle seam
    # ------------------------------------------------------------------

    def bind_executor(self, executor: Executor) -> None:
        """Late-bind an external executor (DI seam, mirrors LoginService pattern).

        Called by the lifespan startup after the ``ThreadPoolExecutor`` is
        created.  EnrichmentService does NOT own the executor lifecycle —
        the caller (lifespan) is responsible for ``shutdown()``.

        Args:
            executor: The executor to use for parallel enrichment from this
                point forward.  Replaces any executor provided at construction
                time or the per-call fallback.
        """
        self._executor = executor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enrich_lots(
        self,
        lots: Sequence[Lot],
        *,
        max_workers: int,
    ) -> list[Lot]:
        """Fetch detail pages in parallel and return enriched lots.

        Args:
            lots: Lots from the list-stage. May be empty.
            max_workers: Thread-pool size used only when no executor was
                injected at construction time (per-call fallback).  When an
                executor is provided this argument is ignored.  Callers read
                this from ``ConfigSource`` (4-8 per target-site rate limits).

        Returns:
            List of ``Lot`` instances in the **same order as input**.
            Successfully enriched lots have ``enrichment_status='done'`` and
            merged coordinates / detail fields.
            Failed lots (per-lot errors) are returned in their original shape
            with ``enrichment_status='failed'``.

        Raises:
            ParserVersionMismatch: propagated from ``DetailParser.parse()`` —
                caller decides how to handle lazy reparse.
        """
        if not lots:
            return []

        # Submit one future per lot, preserving input order via index map.
        # ParserVersionMismatch must NOT be swallowed — it propagates through
        # submit/result boundaries as a Future exception and is re-raised
        # when we call future.result().
        futures: list[Future[Lot]] = []
        if self._executor is not None:
            # Injected executor — lifecycle is caller's responsibility.
            # Do NOT use a with-block; just submit and collect.
            for lot in lots:
                futures.append(self._executor.submit(self._enrich_one, lot))
            results: list[Lot] = []
            for future in futures:
                results.append(future.result())
        else:
            # Fallback: per-call scoped pool (backward compat / tests without DI).
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for lot in lots:
                    futures.append(pool.submit(self._enrich_one, lot))
                # Results collected inside with-block so pool lives during result().
                results = []
                for future in futures:
                    results.append(future.result())

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _enrich_one(self, lot: Lot) -> Lot:
        """Fetch and parse detail page for a single lot.

        Called from worker threads.  All exceptions except
        ``ParserVersionMismatch`` are caught — they propagate to the
        ``Future`` and are re-raised in ``enrich_lots`` only for
        ``ParserVersionMismatch`` (which intentionally escapes the
        per-lot guard).
        """
        url = self._url_builder.lot_detail_url(lot_id=lot.id)
        try:
            response = self._http.get(url)
            detail: ParsedDetail = self._parser.parse(response.text)
        except ParserVersionMismatch:
            # Intentionally NOT caught — propagates to caller.
            raise
        except Exception as exc:
            logger.warning(
                "enrichment failed for lot_id=%s: %s: %s",
                lot.id,
                type(exc).__name__,
                exc,
            )
            return lot.model_copy(update={"enrichment_status": "failed"})

        # Merge ParsedDetail fields into Lot (frozen → model_copy).
        return lot.model_copy(
            update={
                "lat": detail.lat,
                "lon": detail.lon,
                "has_boundaries": detail.has_boundaries,
                "date_update": (
                    detail.date_update if detail.date_update is not None else lot.date_update
                ),
                "raw_json": detail.raw_json,
                "parser_version": detail.parser_version,
                "detail_fetched_at": datetime.now(tz=UTC),
                "enrichment_status": "done",
            }
        )
