# ADR-028 — Paginated Catalogue Backfill

**Status**: Accepted (Auto-trigger section superseded by ADR-032; updated with delta-trigger generation — 2026-05-15)
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: backfill, pagination, catalogue, cold-start, delta-trigger

> **Note (Updated 2026-05-15: delta-trigger generation)**: The «Auto-trigger heuristic» section now documents
> three generations. The primary mechanism is now a **delta-based trigger** in `MonitorCycleService` via
> `BackfillService.maybe_start(region, site_total, db_count, stop_event) -> bool` with threshold
> `len(parsed_lots) + 3`. The `count_active() == 0` + `on_login_success` approach (ADR-032) is **secondary
> fallback** used only when `total_count is None` (site did not return paginator markup).
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

Three generations (newest first):

**Generation 3 — Delta-based trigger in `MonitorCycleService` (primary, 2026-05-15)**

After each head-poll `MonitorCycleService` calls
`BackfillService.maybe_start(region, site_total, db_count, stop_event) -> bool`.

- `site_total` — `ParsedListPage.total_count` (int | None) extracted from
  `<div class="table-paginate__info">Найдено записей: N из N</div>` (ADR-036 update).
- Trigger condition: `site_total is not None` and `site_total - db_count > len(parsed_lots) + 3`.
- If condition met, `maybe_start` fires a supervised `backfill-auto` thread.
  The `+3` slack absorbs normal churn (concurrent deactivations / in-flight monitor writes).
- No TTL. Single-flight lock inside `BackfillService.start()` keeps it idempotent.

**Generation 2 — `on_login_success` callback, `count_active() == 0` guard (secondary fallback)**

Used when `total_count is None` (paginator markup absent). See [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]] for full rationale.
ADR-032 is now marked **deprecated to secondary fallback** — it fires only when Generation 3 cannot.

**Generation 1 — lifespan `count_active() == 0` (superseded)**

`lifespan` checked `lot_repo.count_active() == 0` after startup.  Superseded by ADR-032 (race
with login), then again by Generation 3 (race with empty DB on fresh start).

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
- Lots discovered during backfill do NOT generate email notifications (by design —
  mass-notify on cold-start would spam users). SSE/UI feed IS updated in real-time via
  direct `EventBus.publish(SseLotNew)` after each upsert; email suppression is enforced
  by `SubscribedAtFilteredNotifier` at the channel level, not at `BackfillService` level.

---

## References

- `src/fis_monitor/services/backfill.py` — BackfillService implementation
- `src/fis_monitor/services/paginated_list_fetcher.py` — PaginatedListFetcher
- `src/fis_monitor/web/routes/backfill.py` — HTTP endpoints
- `src/fis_monitor/app.py` — auto-trigger in lifespan
- ADR-019: notification state-machine (dispatcher not called by backfill)
- docs/architecture/04-composition-root.md §4.4
