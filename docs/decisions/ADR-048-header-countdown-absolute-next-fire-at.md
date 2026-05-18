# ADR-048 — Header countdown: absolute `next_fire_at` timestamp as client-side timer SSOT

**Status:** Accepted  
**Date:** 2026-05-18  
**Issue:** bd r82m (P1 регрессия: header countdown «Проверка через 1:00» не тикает)

---

## Context

The `#header-status` widget shows a countdown "Проверка через MM:SS" that should tick
down to the next monitor cycle. After bd 47uh introduced `SseStatus`, the countdown
appeared frozen:

1. `_publish_status` published `next_cycle_mmss = "{interval}:00"` — always the full
   interval, never the real remaining time.
2. The SSE replay-slot (ADR-025) re-sends `SseStatus` on every client connect/reconnect,
   causing htmx to swap `#header-status` with fresh HTML. The swap replaced the live
   `<span>` with a new one whose `data-remaining` (the previous fix, commit 4e96a7a) was
   absent, resetting the visible timer to the full interval.

Root cause: the relative `next_cycle_mmss` string is stateless — it cannot survive a DOM
swap because it carries no absolute time reference. Each swap resets the countdown.

## Decision

**The server becomes the SSOT for the next-fire time via an absolute UTC timestamp.**

1. `SseStatus` gains a new optional field `next_fire_at: datetime | None` (UTC).  
   `_publish_status` sets it to `clock.now() + timedelta(minutes=interval)` (interval > 0)
   or `None` (continuous mode, `interval_minutes == 0`).

2. A computed property `next_fire_at_iso: str` on `SseStatus` renders the timestamp as
   `"YYYY-MM-DDTHH:MM:SSZ"` (Z-suffix for unambiguous UTC parsing via `Date.parse()`).

3. The Jinja partial `_header_status.html.jinja` renders the `<span>` as:
   ```html
   <span data-countdown data-next-check-at="{{ monitor.next_fire_at_iso }}">…</span>
   ```
   The `data-countdown` attribute remains as the JS selector hook (no value needed).

4. The JS countdown reads `data-next-check-at` each tick and computes:
   ```js
   remaining = Math.max(0, Math.round((Date.parse(nextAtStr) - Date.now()) / 1000))
   ```
   This is **swap-safe by construction**: after every SSE swap the new span carries the
   real remaining time in its attribute; the JS computes correctly from the next tick.
   If `data-next-check-at` is absent or empty the tick is a no-op (graceful degrade for
   initial render before the first cycle).

5. `build_monitor_vm` adds `next_fire_at_iso = ""` to the initial-render SimpleNamespace
   (empty = no countdown before first cycle — consistent with existing `next_cycle_mmss`
   behaviour).

## Alternatives Considered

**A — Keep `data-remaining` relative counter, fix reset on swap**  
The 4e96a7a fix tried this. It failed because every SSE swap (including the replay-slot
connect) resets the attribute. Fixing the replay-slot would require server-side clock
state, so we end up at option B anyway — with extra complexity.

**B — Server tracks per-connection elapsed time**  
Heavyweight, requires per-SSE-subscriber state, incompatible with the single-endpoint
fan-out model (ADR-025). Rejected.

**C — Stop the SSE replay-slot from emitting `SseStatus`**  
Tempting, but the replay-slot exists to populate the widget immediately on reconnect
(without waiting for the next cycle). Removing it would cause a blank widget on page
load. Rejected.

## Consequences

- **Positive:** Countdown is swap-safe and reconnect-safe. Server clock is the single
  source of truth — no client-side epoch drift.
- **Positive:** `data-remaining` client state is removed; the DOM stays clean.
- **Positive:** Graceful degrade: empty `data-next-check-at` → JS no-op, no crash.
- **Neutral:** `next_fire_at` is approximate — it is `clock.now() + interval` right after
  a cycle, not adjusted for actual cycle duration variance. For a 1-minute interval this
  is accurate to within a few seconds, which is good enough for the UI chip.
- **Neutral:** `next_cycle_mmss` is kept in `SseStatus` (used in `aria-label` initial
  text on page load). It remains a display hint, not a timer source.
- **Negative (none):** `extra="forbid"` on `SseStatus` means any new serialization code
  that passes `SseStatus` to a strict JSON-only consumer must handle the new `next_fire_at`
  field. Currently no such consumer exists — the encoder renders to HTML only.

## Links

- [[glossary#SseStatus]]
- [[decisions/ADR-025-sse-single-endpoint|ADR-025]] — SSE single endpoint (replay-slot)
- bd 47uh — original SseStatus introduction
- bd r82m — this fix
