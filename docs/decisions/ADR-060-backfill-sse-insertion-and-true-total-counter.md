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
the template context.  The counter is rendered in **exactly one** canonical
place — `#feed-lot-count` in the filter bar — carrying the `js-lot-count` class
and `data-count=N` attribute (see Amendment below).

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

## Amendment (2026-06-01, bd gektar-monitor-0v9)

The original Decision §3 rendered `lot_count` in **two** elements simultaneously —
`#feed-lot-count` (filter bar) and `.zone__title-count` (zone `<h2>` heading) —
both carrying `js-lot-count`.  On screen this showed the same number twice, and
after the page-load render the two could diverge: a server-side filter re-render
(`POST /filters/view`) resets both to the DB total, but the zone heading was
otherwise a redundant copy of the filter-bar counter.

**Resolution:** the counter now lives in **exactly one** canonical element,
`#feed-lot-count` in the filter bar.  The `.zone__title-count` span was removed
from `_feed_lots.html.jinja`; the zone heading is now a plain `<h2>Список</h2>`.
The dead `.zone__title-count` CSS rule and the stale `incrementLotCounters()`
comment referencing the removed element were also dropped.  `incrementLotCounters()`
is unchanged — it now matches exactly one `.js-lot-count` element.

This removes the duplication without weakening the true-total invariant: the
single remaining counter is still driven by `LotQueryService.count(filters)` on
render and incremented per `lot.new` SSE event.

## Amendment (2026-06-01, bd gektar-monitor-z15) — counter re-sync on SSE (re)connect

**Problem.** Decision §3 sets `#feed-lot-count` to the true total via `count()` **at page render**, then mutates it **only** via JS `incrementLotCounters()` on each `lot.new` SSE event. If the browser is not connected to SSE while lots arrive (tab closed, SSE dropped, backfill running during a reconnect gap), those `lot.new` events are missed and never recovered — the counter freezes at its render-time value (observed: 195 while DB held 255). A full page reload re-runs `count()` and shows the correct total, but live re-sync never happens. The increment-only design assumed a continuously-connected SSE stream.

**Resolution (variant B1).** On SSE (re)connect the client re-syncs the counter to the authoritative `count()`:

- The `#feed-lot-count` span is extracted into a shared partial (`partials/_feed_lot_count.html.jinja`), included by `_feed_lots.html.jinja`.
- New read-only route `GET /feed/count` renders that partial: reads the `view_filters` cookie, builds `LotFilters` via the same `_view_filters_to_lot_filters(vf)` adapter used by `build_feed_context`, returns `<span id="feed-lot-count" … data-count=N>` via `LotQueryService.count(filters)` — identical filter parity to page render.
- `app.js` listens on `htmx:sseOpen` (bubbles from `#sse-root` to `document.body`) and issues an htmx OOB swap of `#feed-lot-count` from `GET /feed/count`. This reuses the OOB-swap pattern already used by `POST /filters/view`.

**Why not variant A (server emits a `SseLotCount` event at stream start).** Considered and rejected: A requires a new domain event (`SseLotCount`), an `initial_events` kwarg + a `subscribe()→count()→yield→drain` ordering invariant inside `SseStreamer` — the same fragile drain path implicated in the shutdown work (ADR-014 amendment / gektar-monitor-3l8, -1iz) — and couples the SSE infra layer to `LotQueryService`. B1 touches no domain models and no `SseStreamer`; the only cost is one lightweight HTTP round-trip per (re)connect, negligible for a single-user desktop app. The race A claimed to avoid is benign under B1: `count()` returns the absolute DB total, so a `lot.new` arriving during the in-flight `GET /feed/count` cannot cause a double-count — the absolute value overwrites accumulated increments correctly.

**Consequences.** Counter self-heals on every SSE (re)connect, not only on full page reload. No change to `SseLotNew`, `SseStreamer`, or the domain event union. `only_new` remains ignored by `count()` (unchanged §3 limitation). Filter parity guaranteed by reusing `_view_filters_to_lot_filters`.

## Amendment (2026-06-03, bd gektar-monitor-6jg) — counter semantics «Показано N из M» + «Всего в реестре: X» + 3-state cold-start indicator

Decision §3/§4 defined a **single** counter (`#feed-lot-count` = true filtered total M). 6jg
splits the display into a two-line `div.feed-scope` block inside `.filter-bar` and replaces the
perpetual «Загружаем каталог…» banner with a 3-state indicator. Builds on
[[decisions/ADR-065-feed-visibility-subject-membership|ADR-065]] + [[decisions/ADR-066-sse-membership-filter|ADR-066]]:
after ADR-066 the SSE `lot.new` stream delivers **only** subscribed lots, so the DOM card count
is a trustworthy «shown» number (this was the i7n prerequisite that blocked 6jg).

**Three counters:**
- **N — «Показано N»** = live DOM count `document.querySelectorAll('#feed article.lot').length`.
  New `.js-shown-count` span; `updateShownCount()` (app.js) recomputes from the DOM on
  `lot.new` (live + backfill), after load-more swap, and on `DOMContentLoaded`. N is **not** a
  stored accumulator — the DOM is the source of truth for «shown» (unlike M, where DB is
  authoritative; cf. §86 rejected «recompute M from DOM»). Initial server value =
  `zones.today|length` (the only zone rendering `article.lot`).
- **M — «из M»** = `LotQueryService.count(filters)` (subscribed-filtered total). UNCHANGED
  canonical `#feed-lot-count` (`js-lot-count`, `data-count`); `incrementLotCounters()` unchanged
  (+1 per `lot.new`); re-synced on SSE (re)connect via `GET /feed/count` (§z15). The M element is
  embedded inside the «Показано N из {M}» primary line.
- **X — «Всего в реестре: X»** = `LotRepository.count_active()` (GLOBAL, filter-independent
  `SELECT COUNT(*) WHERE is_active=1`). New `#registry-count` (`js-registry-count`,
  `data-count`). Server-rendered on page / `POST /filters/view` / `POST /filters/clear` (via new
  flat `active_lot_count` key in `build_feed_context`); resynced on SSE reconnect via the OOB span
  in the new `partials/_feed_count_resync.html.jinja` rendered by `GET /feed/count`; **live-updated
  during backfill** by `feed.js` polling `GET /backfill/status` (now carries `active_lot_count`).

**3-state loading indicator (`feed.js`, driven by `GET /backfill/status` `{status, active_lot_count}`):**
`coldStart` = `.feed-scope[data-registry-count] == "0"` at page render.
- **A ROUTINE** (registry non-empty): silence — no notice, no toast. The `#backfill-progress`
  «Загружаем каталог…» banner is **removed**. (Polling still runs while `status==running` to keep X live.)
- **B COLD-START** (registry empty, running): show `.feed-scope__notice` «Заполняем реестр…»;
  N/M/X grow live. Counter stays visible at 0 («Показано 0 из 0») — the
  `.filter-bar__count[data-count="0"]{display:none}` rule was removed.
- **C DONE** (cold-start, `running→done|idle`): `window.Monitor.toast('Каталог обновлён')` ONCE
  (`_toastFired` guard), hide notice, `coldStart=false`. Timer lifecycle unified:
  `shouldPoll = coldStart || status==='running'` (catches `idle→running→done` even when backfill
  starts after page load).

**«Показать ещё»** visibility stays server-authoritative via `{% if next_cursor %}` — UNCHANGED
(not driven by JS `N>=M`, avoiding SSE races).

**Backend deltas:** `GET /backfill/status` +`active_lot_count`; `GET /feed/count` renders M span +
OOB `#registry-count`; `build_feed_context` exposes flat `active_lot_count`. `POST /filters/view`,
`SseLotNew`, `SseStreamer`, `LotQueryService.count` — unchanged.

**Test scope (per [[architecture/09-test-strategy]]):** Layer-4 only — `GET /backfill/status`
returns `active_lot_count`; `GET /feed/count` returns M + OOB X. `feed.js`/`app.js` state machine,
toast, N-from-DOM = **smoke-only** (Playwright excluded by strategy).

## Amendment (2026-06-04, bd gektar-monitor-azc) — page-guard on §z15 re-sync handler

**Problem.** The §z15 `htmx:sseOpen` handler lives in `app.js` and `#sse-root` lives in
`base.html.jinja`, so the handler fired on **every** page — including `/settings`, where
`#feed-lot-count` does not exist. htmx 1.9.12 falls back to `document.body` when the
`target` selector resolves to null, so the `GET /feed/count` response
(`_feed_count_resync.html.jinja`) replaced the entire page content: user saw only
«36 лотов» instead of the settings page (reported during a running backfill, but the
mechanism is page-load/SSE-connect-bound, not backfill-bound; backfill only raises
reconnect frequency).

**Resolution.** Guard added in `app.js`: `htmx.ajax('GET', '/feed/count', …)` is issued
only if `document.getElementById('feed-lot-count')` is present. On non-feed pages the
re-sync is a silent no-op; feed-page behaviour (initial connect + reconnect after
`filter-changed` / `#feed-wrapper` outerHTML swap) is unchanged — the span is
server-rendered before any EventSource opens, so the guard cannot falsely skip.

**Invariant for future client-side `htmx.ajax` calls:** any programmatic call whose
target element exists only on some pages MUST verify the target's presence first —
htmx's null-target fallback swaps into `document.body` and destroys the page.

## Amendment (2026-06-05, bd gektar-monitor-gdo) — третий триггер `_poll()`: lot.new → debounced resync X

**Прод-баг.** Счётчик X («Всего в реестре», `#registry-count`) замораживался, когда лоты приходили
по SSE `lot.new` при живом соединении: `incrementLotCounters()` инкрементирует только `.js-lot-count`
(M), а поллинг `GET /backfill/status` (§6jg) стартует лишь если на initial fetch `coldStart=true`
или `status='running'`. Если страница открыта ДО старта backfill (idle, реестр непустой), переход
idle→running никогда не ловится — комментарий §6jg «catches idle→running→done» верен только для
cold-start. Путь «лоты пришли по SSE при живом соединении» обновления X не имел вовсе.

**Resolution.** В `feed.js` добавлен третий триггер `_poll()` (к initial fetch и интервал-таймеру):
слушатель `htmx:sseMessage` на `document.body` (тот же `e.detail.type === 'lot.new'`-контракт, что в
`app.js`), debounced trailing 400 мс → `_poll()`. Burst backfill-карточек коллапсирует в один запрос;
`_update()` при `status='running'` перезапускает интервал-таймер — лечит мёртвый idle→running.
X всегда берётся абсолютом из БД (`active_lot_count`), поэтому гонки с OOB-resync (§z15) и
интервал-поллингом безопасны: `_updateRegistry` не кеширует узел (`getElementById` на каждый вызов).
`incrementLotCounters()`, `app.js`, backend — без изменений. Слой smoke-only (§6jg test scope).

## Amendment (2026-06-05, bd gektar-monitor-gyn): SSE insertion target = section#feed-zone-list, сортировочное позиционирование

### Прод-баг

Оригинальное решение §2 делало `afterbegin` на `#feed`. Это вставляло карточку **вне `section.zone`** — заголовок «Список» терялся, порядок не соответствовал серверной сортировке `ORDER BY date_create DESC, id DESC`. F5 исправлял визуально.

### Новый контракт вставки

`sse-swap="lot.new"` перенесён с `#feed` на постоянно рендеримую `section#feed-zone-list` (`afterbegin`). JS-обработчик `htmx:sseMessage` синхронно перемещает вставленную карточку в корректную позицию сортировки:

- Ключ сортировки: `data-date-create` (naive ISO 8601 из `isoformat()`) + `data-lot-id`. Сравнение лексикографическое; `date_create=None` → пустая строка → карточка уходит в конец.
- **Backfill-карточки** (`data-backfill=1`) перемещаются в конец `section#feed-zone-list`, непосредственно перед `#load-more-trigger` — без изменения существующего поведения из §2.

**Пустая зона**: заголовок скрывается CSS-правилом `#feed-zone-list:not(:has(article.lot)) .zone__head` — без JS-логики.

### lot.status вынесен на отдельный listener

`lot.status` (JSON-событие, ранее routing-путь был мёртв) вынесен на отдельный `span#lot-status-listener` с `hx-swap="none"`. JS диспатчит входящий payload в `onLotStatusChange`. Известный баг diff.region (bd gektar-monitor-dsz) сохраняется без изменений.

### Pill «N новых»: дедупликация через MutationObserver

Инкремент счётчика «N новых» срабатывает через `MutationObserver` с дедупликацией по батчу: `afterbegin` + последующий `relocate` порождают две записи на один узел; дедупликация гарантирует однократный инкремент на карточку.

## See also

- [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] — per-connection view-filter
- [[decisions/ADR-065-feed-visibility-subject-membership|ADR-065]], [[decisions/ADR-066-sse-membership-filter|ADR-066]] — membership visibility (page-load + SSE); prerequisite for trustworthy DOM «shown» count
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — BackfillService design
- [[data-model/sse]] — `SseLotNew.is_backfill` field documentation
- [[glossary#backfill lot]] — term definition
