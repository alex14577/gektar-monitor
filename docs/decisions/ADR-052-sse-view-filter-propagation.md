# ADR-052 — SSE View-Filter Propagation to Per-Connection Predicate

**Status**: Accepted
**Date**: 2026-05-18
**Deciders**: Backend Architect, SRE (brainstorm — gektar_monitor-10t5)
**Tags**: sse, view-filters, per-connection, DI, predicate, filter-propagation
**Extends**: [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I5

---

## Context

`lot.new` SSE events were published to all connected subscribers unconditionally.
`ViewFilters` (subjects / area_min / area_max / only_stars / only_new) were applied
server-side only during HTTP-render (`GET /` and `POST /filters/view`) — not during
SSE fan-out. As a result, new lots arrived live in the feed (#feed `sse-swap="lot.new"`)
regardless of the user's active filter, then vanished on the next F5 or
`POST /filters/view`. Bug: **visual inconsistency between live SSE state and HTTP-render
state**.

---

## Decision

Apply view-filters **server-side, per-connection, via DI-injected predicate**.

1. **New pure factory** `services/sse_view_filter.make_sse_view_filter(vf: ViewFilters)
   -> Callable[[SseEvent], bool]`:
   - Stateless closure. Captures filter state at connection time (snapshot).
   - Fast path: if all filter fields are default (no subjects, no area bounds,
     `only_stars=False`) — returns an always-True sentinel (avoids per-event
     isinstance checks on idle tabs).
   - Non-`SseLotNew` events always pass through (predicate is not applied to
     `cycle.done`, `status`, `session.expired`, etc.).

2. **`SseStreamer.stream(*, event_filter=None)`** — new optional kwarg. When set,
   called before `_event_encoder`; events where `event_filter(event)` returns
   `False` are silently suppressed. `SseStreamer` does NOT import `ViewFilters` or
   `make_sse_view_filter` — receives only a `Callable`.

3. **`GET /events` route** reads the `view_filters` cookie once at connection time,
   calls `ViewFiltersService.deserialize`, then `make_sse_view_filter`, and passes
   the predicate to `streamer.stream(event_filter=...)`. Missing or malformed cookie
   → `None` (pass-through).

---

## Filter semantics (per field)

| Field | SSE lot.new behaviour |
|---|---|
| `subjects` (non-empty) | `lot.region_id in {int(s) for s in subjects}` — suppress on mismatch or `region_id=None` (conservative). |
| `area_min` (not None) | `lot.area_sqm >= area_min` — suppress below bound. `area_sqm=None` → **pass-through** (fail-open, enrichment pending). |
| `area_max` (not None) | `lot.area_sqm <= area_max` — suppress above bound. `area_sqm=None` → **pass-through**. |
| `only_new=True` | **Always pass** for `lot.new` (by definition, every `lot.new` is a new lot — no-op). |
| `only_stars=True` | **Always suppress** all `lot.new` (new lots are never starred; `LotPublicDTO` has no `starred` field — only `LotUserDTO` does, and it never crosses the SSE bus per §3.6.1 of architecture/03-protocols). |

---

## Alternatives considered

| Option | Reason rejected |
|---|---|
| **Client-side filter (data-attrs + JS)** | Duplicates filter logic in JS; DRY violation; data-attrs on `<article>` already carry `data-region`/`data-area` for browser notifications (ADR-049), not for filter evaluation. Per ADR-035 I5, "client-side" means per-user view, not necessarily JS-evaluated. |
| **Server-side with per-type fan-out queues** | Would require a separate queue per active filter combination — N queues for N connected clients with N distinct filters. Memory and complexity O(N). The predicate approach is O(1) per event per client. |
| **Suppress at publish-time (BackfillService / BrowserSseNotifier)** | Publisher doesn't know about per-connection view-filters; bus is multi-subscriber. Filtering at publish time would suppress the event for ALL subscribers, not just the one with the active filter. |

---

## Consequences

### Per MVP (this ADR)
- `lot.new` events are suppressed for connections where the active view-filter
  would exclude the lot. Browser feed stays visually consistent with the
  server-rendered feed without F5.
- Non-`lot.new` events (status updates, cycle progress, ping) are never affected.
- `SseStreamer` remains agnostic of view-filter logic (low coupling, OCP).
- Filter snapshot is taken at `EventSource` connect time (HTTP handshake). It does
  NOT update if the user changes filters without reconnecting.

### Deferred scope (follow-up bd tasks)
- **Live cookie sync**: when the user submits `POST /filters/view`, the SSE
  connection reads a stale predicate until F5 (new EventSource). Live sync would
  require either SSE reconnect signalling from the filter endpoint, or a
  per-connection mutable reference. Deferred — acceptable for MVP given that F5
  already refreshes the feed.
- **`only_stars` UX**: should browser notifications / sounds be suppressed for
  `lot.new` events that fail `only_stars`? Currently the SSE event is not sent at
  all, so JS never fires the notification sound. This is intentional for MVP.
- **Filter-eval observability**: no metrics beyond `_log.debug("sse.event.filtered")`
  per suppressed event. Adding a counter (Prometheus / structured aggregate log)
  is deferred.
- **`region_id=None` conservative suppress**: currently `subjects` filter suppresses
  lots where `lot.region_id is None`. Once enrichment reliably populates `region_id`
  for all lots, this may be relaxed to pass-through (same fail-open logic as `area_sqm`).

### Cookie-change-while-connected
The predicate snapshot is **immutable for the lifetime of the SSE connection**.
Changing the view-filter via `POST /filters/view` updates the cookie but does NOT
update the live SSE predicate. The user must reload (F5) to get a new `EventSource`
connection with the updated predicate.

This is intentional and documented here. Live predicate update requires:
(a) an out-of-band SSE message to signal reconnect, or
(b) the filter endpoint explicitly closing the existing SSE connection.
Both are non-trivial; deferred as a follow-up bd task.

---

## Amendment — m72b (2026-05-18): Live cookie sync via HX-Trigger + client-side reconnect

**Deferred scope "Live cookie sync" is now closed.**

`POST /filters/view` and `POST /filters/clear` now return the HTTP response header
`HX-Trigger: filter-changed`. htmx fires a `filter-changed` CustomEvent on the
request element which bubbles to `document.body`; `app.js` listens for this event
and cycles the `sse-connect` attribute on `#sse-root`:

1. `root.removeAttribute('sse-connect')` — htmx-sse MutationObserver tears down the
   existing `EventSource`.
2. `root.setAttribute('sse-connect', url)` + `htmx.process(root)` (in a `setTimeout(0)`
   tick) — htmx-sse creates a new `EventSource`, which re-reads the updated
   `view_filters` cookie during the `GET /events` handshake.
3. A 200 ms debounce (module-scoped `_reconnectTimer`) prevents multiple rapid filter
   clicks (region/area toggles) from spawning several reconnects.

**Test plan:**
- Layer 4 (`TestClient`): `test_post_view_filters_returns_hx_trigger_header` and
  `test_post_clear_filters_returns_hx_trigger_header` in
  `tests/unit/web/routes/test_filters.py`.
- Layer 3 JS (`app.js`): not covered automatically (Playwright excluded per
  `docs/architecture/09-test-strategy.md`); smoke test manually via browser DevTools
  → Network tab → confirm new `GET /events` request after filter submit.

**Cookie-change-while-connected** section below is now resolved for the POST
`/filters/view` and `/filters/clear` paths. The predicate snapshot is still immutable
within a single `EventSource` lifetime; the fix works by forcing a reconnect after each
filter mutation so a fresh snapshot is taken.

---

## Amendment — qhw8 (2026-05-18)

`only_stars` удалён как часть продуктового решения по удалению фичи «Избранное»
(см. [[decisions/ADR-053-remove-favorites-feature|ADR-053]]). Строка `only_stars=True`
в таблице «Filter semantics» выше — исторический контекст; поле больше не существует
в `ViewFilters`. Special-case ветка в `make_sse_view_filter` удалена: fast-path и
predicate-логика больше не ссылаются на `only_stars`.

---

## Amendment — i7n (2026-06-03): membership-предикат композится поверх view-фильтра

[[decisions/ADR-066-sse-membership-filter|ADR-066]] добавляет **второй** per-connection
предикат — membership-фильтр (`make_sse_membership_filter`, читает `region_subscriptions`
snapshot на connect). `_build_event_filter` теперь **всегда** возвращает `Callable` (не
`None`): при отсутствующем/битом `view_filters` cookie — membership-only предикат; при
валидном cookie — `lambda e: membership(e) and view(e)` (short-circuit `and`). Строка
«missing/malformed cookie → `None` (pass-through)» в §Decision выше относится только к
view-фильтру; membership применяется безусловно. `make_sse_view_filter` и
`SseStreamer.stream` — без изменений (фикс аддитивный, OCP).

---

## References

- `src/fis_monitor/services/sse_view_filter.py` — predicate factory (new)
- `src/fis_monitor/infra/sse/sse_stream.py::SseStreamer.stream()` — `event_filter` kwarg
- `src/fis_monitor/web/routes/events.py::sse_events` — cookie → predicate wiring
- `tests/unit/services/test_sse_view_filter.py` — Layer 1 predicate unit tests
- `tests/unit/web/routes/test_events_filter.py` — Layer 4 SSE endpoint filter tests
- [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I5 — browser channel receives all lots; view-scope per-connection (this ADR)
- [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] — EventBus design (multi-subscriber, normal/critical routing)
- [[architecture/09-test-strategy]] §Layer 4 SSE
