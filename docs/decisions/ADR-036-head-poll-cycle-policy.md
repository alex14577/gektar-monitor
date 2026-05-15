# ADR-036 — Head-Poll Cycle Policy: MonitorCycle vs FullScan vs Backfill

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: monitor-cycle, full-scan, backfill, pagination, head-poll, per_page, PaginatedListFetcher
**Supersedes**: —
**See also**: [[decisions/ADR-035-three-scope-filter-model|ADR-035]] (Notify scope still applies), [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]], [[decisions/ADR-033-web-editable-schedule|ADR-033]]

---

## Context

`MonitorCycleService` currently issues a **single-page HTTP fetch** per macro-region each cycle (`monitor_cycle.py:392` — `self._url_builder.lot_list_url(region=region)`, page defaults to 1). This single-page approach has always been the design for rapid new-lot discovery, but the intent was never formalised in an ADR.

`FullScanService` delegates to `PaginatedListFetcher.iterate()` (`full_scan.py:310`) and walks **all pages** until the iterator is exhausted, collecting the complete `seen_ids` set for mass-deactivation. Walking pages 1..N is a correctness requirement there: omitting any page causes `lot_repo.mark_inactive` to fire on lots that are still present on the site.

`BackfillService` similarly calls `PaginatedListFetcher.iterate()` with full pagination (`backfill.py:270–274`) to bootstrap the catalogue on first-successful-login — again a correctness requirement.

`PaginatedListFetcher.iterate()` currently accepts no `per_page` or `max_pages` arguments (`paginated_list_fetcher.py:71–77`); the URL builder `lot_list_url` likewise has no `per_page` query-param (`url_builder.py:49–79`). The site's Yii2 front-end does support a native per-page parameter (`FreeLotSearch[per-page]` or equivalent), but this has not yet been exercised — presence and exact param name must be verified in bd `gektar_monitor-3pw`.

MonitorCycle running full pagination every `interval_minutes` (default 1 min) would be wasteful: FullScan already walks the full catalogue daily at `monitoring.full_scan_time` (default 04:00). Each MonitorCycle pass is inherently latency-sensitive — the field expects new-lot discovery within one interval window — while FullScan is a heavy nightly sweep tolerant of higher cost.

**User decision (2026-05-15)**: the two operations serve fundamentally different purposes at different frequencies and should have explicitly different pagination contracts.

---

## Decision

Three trigger types with distinct pagination contracts:

| Service | Frequency | Page-size | Pagination | Purpose |
|---|---|---|---|---|
| `MonitorCycleService` | every `interval_minutes` (default 1 min) | **20** | page=1 only — no walk | New-lot discovery → upsert → notify via `FilterMatcher` |
| `FullScanService` | daily at `monitoring.full_scan_time` (default 04:00) | 50 | full walk until empty | Active-set for mass-deactivation |
| `BackfillService` | one-shot at first-successful-login | 50 | full walk | Bootstrap catalogue |

MonitorCycle fetches only page=1 with `per_page=20`. This is a **head-poll**: it captures the freshest lots (server returns newest-first via `sort=-DATE_CREATE`, `url_builder.py:25`) and exits without walking deeper pages. FullScan and Backfill keep full pagination with `per_page=50` (unchanged in semantics; parameter plumbing is new in `gektar_monitor-3pw`).

---

## Invariants

**H1. Head-poll freshness**: a lot published to the site between two MonitorCycle passes appears in page=1 at the next pass (within one `interval_minutes` window, ≤1 min by default).

**H2. Coverage gap is bounded and accepted**: lots published outside page=1 (i.e., not among the 20 freshest lots at poll time) are NOT visible to MonitorCycle until the next FullScan. The maximum gap is ≤24 h. This is acceptable because FullScan is the correctness safety net for deactivation and eventual discovery.

**H3. Notification gate is uniform**: `FilterMatcher.matches` (`filter_matcher.py:58–75`) is called identically for lots discovered via any trigger. There is a single notify-gate downstream regardless of whether a lot arrived via head-poll or full-scan.

**H4. Head-poll cost is bounded**: per MonitorCycle pass ≤ `len(settings.regions) × 1 HTTP request`. For the default Far East configuration (regions=[1, 2]), that is 2 requests per pass. FullScan cost is proportional to catalogue size and unbounded (full walk).

---

## Consequences

- **`PaginatedListFetcher.iterate`** gains two new kwargs: `per_page: int = 50` and `max_pages: int | None = None` (None = unbounded). Implemented in bd `gektar_monitor-3pw`. Existing callers (FullScan, Backfill) pass no new arguments and retain current semantics via defaults.
- **`MonitorCycleService._run_cycle_inner`** (`monitor_cycle.py:382`) transitions from a direct single HTTP call to invoking `PaginatedListFetcher.iterate(region, per_page=20, max_pages=1)`. Implemented in bd `gektar_monitor-3pw`.
- **`FullScanService._fetch_region_ids_paginated`** (`full_scan.py:310`) invokes `iterate(region, stop_event, per_page=50)` — semantically unchanged, new kwarg only.
- **`BackfillService._process_region`** (`backfill.py:270`) invokes `iterate(region, stop, per_page=50)` — semantically unchanged.
- **`infra/http/url_builder.lot_list_url`** (`url_builder.py:49`) does NOT currently support a `per_page` query param. bd `gektar_monitor-3pw` must verify whether the Yii2 front-end (`надальнийвосток.рф`) honours a `FreeLotSearch[per-page]` or equivalent parameter. If supported, `lot_list_url` gains a `per_page` kwarg and appends the param. If the site ignores the param, head-poll falls back to the site's native default page size (graceful degradation — correctness of discovery is unaffected; only the batch size differs).

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| (a) Drop MonitorCycle; rely solely on FullScan | New-lot discovery latency increases from ~1 min to ~24 h — unacceptable for the «конкурентная срочность» UX goal |
| (b) Keep full pagination in MonitorCycle but lower frequency | Still wasteful per cycle; gains nothing over head-poll for discovery purposes |
| (c) Single hybrid service with mode-switch (head/full) | Violates SRP; MonitorCycleService and FullScanService are already cleanly separated by `composition.py` wiring and have distinct lifecycle contracts |

---

## Migration / Rollout

Pure code change in bd `gektar_monitor-3pw`; no on-disk config change required. No new `config.json` fields are introduced. Existing `interval_minutes` retains its meaning (inter-cycle sleep). If the FIS site does not honour a `per_page` query param, MonitorCycle falls back to the site's default page size gracefully — the only observable difference is the number of lots returned per head-poll, not correctness.

---

## References

- `src/fis_monitor/services/monitor_cycle.py:382–392` — `_run_cycle_inner`: current single HTTP fetch per region
- `src/fis_monitor/services/paginated_list_fetcher.py:71–77` — `PaginatedListFetcher.iterate` current signature (no per_page/max_pages)
- `src/fis_monitor/services/paginated_list_fetcher.py:95–182` — full pagination walk: page loop, empty-page exit, `_PAGE_LIMIT=1000` guard
- `src/fis_monitor/services/full_scan.py:310–327` — `_fetch_region_ids_paginated`: full walk via `iterate()`
- `src/fis_monitor/services/backfill.py:270–274` — `_process_region`: full walk via `iterate()`
- `src/fis_monitor/infra/http/url_builder.py:49–79` — `lot_list_url`: no `per_page` param today
- [[decisions/ADR-035-three-scope-filter-model|ADR-035]] — Notify scope (FilterMatcher) applies uniformly (H3)
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — PaginatedListFetcher origin
- [[decisions/ADR-033-web-editable-schedule|ADR-033]] — `full_scan_time` schedule
- [[architecture/07-concurrency]] — thread layout for monitor-cycle, full-scan, backfill
- [[glossary#head-poll]], [[glossary#full-scan]], [[glossary#monitor-cycle]]
