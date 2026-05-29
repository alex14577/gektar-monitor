# ADR-060 — Backfill SSE insertion at bottom + counter as true total

**Status:** Accepted
**Date:** 2026-05-29
**Tags:** sse, backfill, feed, counter

## Context

Two related UX bugs were discovered in the lot feed:

### Bug dr21 — Backfill lots inserted at top, silently identical to live lots

`BackfillService` published `SseLotNew(event="lot.new")` identical to the live
monitor-cycle path (`BrowserSseNotifier`).  The htmx `sse-swap="lot.new"` binding
uses `hx-swap="afterbegin"` on `#feed`, so every backfill lot was prepended at the
**top** of the visible list — pushing genuine new live lots down and confusing the
user.  Backfill also triggered `onLotNew` (sound + browser notification +
escalation), despite being historical catch-up data with no real-time significance.

### Bugs ddpf + hke7 — Counter frozen at page size, not updated on SSE

`#feed-lot-count` displayed `zones.today|length` — capped at `_FEED_PAGE_SIZE`
(200) and never updated when new lots arrived via SSE.  The load-more OOB swap
overwrote the counter with `shown_total` (the running page-display count) rather
than the true DB total.

## Decision

### 1. `SseLotNew.is_backfill: bool = False`

Add a field to `SseLotNew` in `domain/models.py`.  Default `False` preserves
backward compatibility.  `BackfillService` sets `is_backfill=True`; the live path
(`BrowserSseNotifier`) leaves the default.

**Why not a separate `"lot.backfill"` event?**
A separate event name bypasses [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]]:
the per-connection view-filter only listens on `"lot.new"`.  A new event would
require a second `sse-swap` binding and duplicated filter logic in the JS layer,
increasing coupling.  The `is_backfill` flag inside the existing event name is
the lowest-coupling solution.

### 2. JS reposition — backfill cards moved to bottom synchronously

In the `htmx:sseMessage` handler:

```
if (node.dataset.backfill === '1') {
    // move to just before #load-more-trigger (or append to section.zone / #feed)
    // NO sound / notification / escalation
} else {
    onLotNew(node);   // live path: sound + notification + escalation
}
```

The move is synchronous in the same event-loop tick as the htmx insert — no
`setTimeout`, no visible flash.  The `data-backfill="1"` attribute is stamped by
`_lot_poster.html.jinja` when `lot.is_backfill` is truthy (passed from
`_SseLotNewViewModel`).

### 3. Counter = true total from `LotQueryService.count(filters)`

`LotQueryService.count(filters)` issues `SELECT COUNT(*) WHERE is_active = 1 AND
<same filter predicates as search() minus cursor/ORDER/LIMIT>`.  It is NOT capped
at page size and NOT affected by `only_new` (user-state predicate, in-memory only).

`build_feed_context` calls `count()` after `search()` and exposes `lot_count` in
the template context.  `#feed-lot-count` and `.zone__title-count` render
`lot_count`, both carry the `js-lot-count` class and `data-count=N` attribute.

The JS `incrementLotCounters()` function increments **every** `.js-lot-count`
element on each `lot.new` event (live + backfill), using Russian plural forms
matching the server-side Jinja filter.

### 4. OOB counter removed from `_feed_more.html.jinja`

The `hx-swap-oob` span that overwrote `#feed-lot-count` with `shown_total` is
removed.  Load-more no longer touches the counter — the JS handles it going forward.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-----------------|
| Separate `"lot.backfill"` SSE event | Bypasses ADR-052 view-filter; requires duplicate sse-swap binding; higher coupling |
| Server-side OOB counter on every SSE event | Would require HTML SSE response to include a counter fragment alongside the lot card; violates SRP of `SseLotNew` (domain event should not carry UI state) |
| Recompute counter via JS from DOM | Fragile: `section.zone` may contain load-more-trigger, backfill cards arriving before section is rendered, etc. Count from DB is authoritative |
| Keep OOB counter in load-more | Overwrites true total with shown_total (page-visible subset); misleads user |

## Consequences

- **Counter shows true total** including lots not yet visible (beyond page 1).
  `only_new` filter is intentionally ignored by `count()` — documented limitation.
  The counter represents the region+area scope, consistent with what the SSE
  view-filter (ADR-052) applies per-connection.
- **Live +1 is consistent with SSE view-filter**: only events that pass the
  per-connection filter increment the counter, so the counter stays coherent with
  the cards shown to the user.
- **Backfill lots are silent**: no sound escalation during catalog catch-up.
  This matches user expectation: backfill is historical data, not a real-time alert.
- **One extra DB query per feed render**: `count()` is a `SELECT COUNT(*)` with the
  same WHERE as `search()` — index-only scan on `is_active`, fast.
- `_SseLotNewViewModel.__slots__` now includes `_is_backfill`; `LotViewModel`
  gains a `is_backfill` property (always False) so server-rendered templates
  never raise `AttributeError` on the attribute.

## See also

- [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] — per-connection view-filter
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — BackfillService design
- [[data-model/sse]] — `SseLotNew.is_backfill` field documentation
- [[glossary#backfill lot]] — term definition
