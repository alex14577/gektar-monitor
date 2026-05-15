# ADR-028 — Paginated Catalogue Backfill

**Status**: Accepted (Auto-trigger section superseded by ADR-032)
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: backfill, pagination, catalogue, cold-start

> **Note**: The «Auto-trigger heuristic» section below is superseded by
> [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]]. Trigger has been
> moved from `lifespan` to `_handle_step4_next` (onboarding completion handler).
> All other sections of this ADR remain in effect.

---

## Context

The monitor cycle (`MonitorCycleService.run_cycle`) fetches one page of the lot
listing per call.  On a cold start (empty database) or after a long offline
window, the user only sees lots that happen to appear on the first listing page
of each configured region.  The full catalogue (potentially thousands of lots
spread across many pages) is never loaded.

This means:
- Users see a sparse initial view of the catalogue.
- Lots that appeared and disappeared before the first monitor cycle run are never
  discovered.
- `FullScanService` only operates on lots already in the DB — it cannot bootstrap.

---

## Decision

Introduce `PaginatedListFetcher` + `BackfillService` to address the cold-start
and manual-refresh gaps:

### PaginatedListFetcher
Iterates all pages for a given region using `TorgiUrlBuilder.lot_list_url(region, page)`.
Returns a lazy `Iterator[ParsedListRow]`.  Accepts a `stop_event` for cooperative
cancellation.  Sleeps `sleep_between_pages` seconds between pages for rate-limit
courtesy.

### BackfillService
- **Single-flight**: at most one backfill runs at a time (`_flight_lock`).
- **Cancellable**: `cancel()` sets `_stop_event`; running backfill exits within
  `sleep_between_pages` interval.
- **Thread-safe progress**: `status()` returns a consistent snapshot guarded by
  `_progress_lock`.
- **Notify caller-side**: `BackfillService.start()` does NOT call
  `NotifierDispatcher.dispatch()` — caller controls whether notifications fire.
  The `notify` parameter has been removed from `LotRepository.upsert()` (P1-3);
  notification dispatch is a caller responsibility.
- **Region skip-set**: while a region is being backfilled,
  `MonitorCycleService` skips that region to prevent concurrent catalogue writes.

### Auto-trigger heuristic
`lifespan` checks `lot_repo.count_active() == 0` after startup.  If the DB is
empty, a supervised `backfill-auto` thread is started that calls
`backfill.start(supervisor.stop_event)`.  This gives users a populated initial
catalogue within minutes of first launch.

### Manual trigger
`POST /backfill/start` spawns a daemon thread that calls `svc.start()`.
The route returns 202 immediately; the single-flight lock inside `start()`
ensures idempotency.  `GET /backfill/status` exposes progress; `POST
/backfill/cancel` cancels a running backfill.

---

## Alternatives Considered

| Alternative | Reason Rejected |
|---|---|
| Run full scan on cold start | FullScanService marks lots inactive — wrong semantics for bootstrap |
| Load all pages in monitor cycle | Would block the cycle; monitor cycle should stay single-page per call |
| Pre-populate DB from a snapshot file | Extra artifact; no self-healing if snapshot is stale |

---

## Consequences

### Positive
- Users see the full regional catalogue within minutes of first launch.
- Manual backfill allows refreshing after extended offline windows.
- `PaginatedListFetcher` is reused by `FullScanService` for full-catalogue
  coverage (all pages, not just page 1) in removal-detection scans.

### Negative
- Backfill may take several minutes for large regions (rate-limit pacing).
- During backfill, monitor cycle skips the region — new lots may be delayed.
- Lots discovered during backfill do not generate notifications (by design —
  mass-notify on cold-start would spam users).

---

## References

- `src/fis_monitor/services/backfill.py` — BackfillService implementation
- `src/fis_monitor/services/paginated_list_fetcher.py` — PaginatedListFetcher
- `src/fis_monitor/web/routes/backfill.py` — HTTP endpoints
- `src/fis_monitor/app.py` — auto-trigger in lifespan
- ADR-019: notification state-machine (dispatcher not called by backfill)
- docs/architecture/04-composition-root.md §4.4
