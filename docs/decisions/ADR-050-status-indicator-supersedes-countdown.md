# ADR-050 — Status indicator: pulse-dot replaces countdown (supersedes ADR-048)

**Status:** Accepted  
**Date:** 2026-05-18  
**Issue:** bd gektar_monitor-hiq3  
**Supersedes:** [[decisions/ADR-048-header-countdown-absolute-next-fire-at|ADR-048]]

---

## Context

ADR-048 introduced an absolute UTC timestamp (`next_fire_at`) on `SseStatus` to make
the client-side countdown swap-safe.  The fix was technically correct — the countdown
no longer resets on SSE reconnect — but the resulting UX has a different problem:

1. **Countdown is noise, not signal.**  Showing "Проверка через 0:47" communicates
   nothing actionable.  The user cares whether a check *is happening right now*, not
   how many seconds until the next one.

2. **Binary state is sufficient.**  The monitor cycle has two observable states:
   *idle* (waiting for the next scheduled run) and *checking* (fetching + parsing).
   A pulse-dot communicates this cleanly without a ticking number.

3. **The countdown creates a maintenance surface.**  `next_fire_at` must survive SSE
   swaps, reconnects, and page refreshes.  The `next_fire_at_iso` property, the
   `data-next-check-at` attribute, and the `setInterval` JS block are all coupling
   points between the server clock and the client DOM.  The pulse-dot removes all of them.

4. **`SseCycleStarted` is the natural event source.**  The event bus already emits
   `SseCycleDone`; adding a symmetric `SseCycleStarted` gives the client a reliable
   signal without polling or timer arithmetic.

## Decision

**Replace the countdown with a pulse-dot driven by SSE events.**

### Server changes

1. Add `SseCycleStarted(timestamp: datetime, cycle_id: int)` to `domain/models.py`
   as a new member of the `SseEvent` union.  SSE name: `cycle.started`.

2. Publish `SseCycleStarted` at the very start of
   `MonitorCycleService.run_cycle`, before any I/O, as a best-effort publish
   (exceptions logged, not propagated).

3. Remove from `SseStatus`:
   - `next_cycle_mmss: str` field
   - `next_fire_at: datetime | None` field
   - `next_fire_at_iso` computed property

4. Remove the corresponding calculations from `MonitorCycleService._publish_status`
   (the `timedelta` import is also removed as it is no longer needed).

5. Remove `next_cycle_mmss` and `next_fire_at_iso` from the initial-render
   `SimpleNamespace` in `web/monitor_vm.py::build_monitor_vm`.

### Client changes

6. `_header_status.html.jinja`: replace the `<span data-countdown ...>` block with:
   ```html
   <span class="check-status" data-state="idle">
     <span class="check-dot" aria-hidden="true"></span>
     <span class="check-label">Жду</span>
   </span>
   ```

7. `static/app.js`: remove the `setInterval` countdown block (previously lines 188–209).
   Add an `htmx:sseMessage` listener:
   - `cycle.started` → `data-state="checking"` on `.check-status`
   - `cycle.done`    → `data-state="idle"` on `.check-status`

8. `static/app.css`: add `.check-status`, `.check-dot` with `@keyframes pulse` animation.
   The `data-state="checking"` selector drives the animation and label text change.

## Alternatives Considered

**A — Keep the countdown from ADR-048, fix UX issues separately**  
The swap-safety fix in ADR-048 was correct.  Keeping it avoids churn.  Rejected
because the countdown communicates nothing actionable and the maintenance surface
(server clock → DOM attribute → JS tick) is disproportionate to the value delivered.

**B — Polling (`setInterval` + `/api/status` fetch)**  
Avoids a new SSE event type.  Rejected: introduces a second real-time channel
alongside SSE, violates ADR-025 single-endpoint principle, and adds server load.

**C — Use existing `SseCycleDone` only (no `SseCycleStarted`)**  
The dot would switch to "idle" at cycle end but never to "checking" at cycle start —
the visual feedback for "a check is in progress" would be lost.  Rejected.

## Consequences

- **Positive:** Countdown noise eliminated.  The widget shows only two states that
  the user can act on: idle (nothing happening) and checking (happening now).
- **Positive:** `next_fire_at`, `next_cycle_mmss`, `next_fire_at_iso`, `setInterval`
  removed — six files simplified, no client-side timer arithmetic.
- **Positive:** `SseCycleStarted` + `SseCycleDone` are a symmetric pair (SRP);
  each carries `timestamp` and `cycle_id` for observability.
- **Neutral:** `SseStatus` still carries `interval_minutes` and `state` — these
  remain useful for the traffic-light dot and session-expiry chip.
- **Negative (minor):** Existing serialised `SseStatus` JSON in test fixtures must
  be updated to remove the stripped fields.  Handled by updating
  `tests/unit/services/test_monitor_cycle_next_fire_at.py` and
  `tests/unit/web/test_monitor_vm.py`.
- **Negative (none):** The SSE stream contract (ADR-025) is additive-only:
  `cycle.started` is a new event name, clients that do not handle it ignore it safely.

## Links

- [[decisions/ADR-048-header-countdown-absolute-next-fire-at|ADR-048]] — superseded by this decision
- [[decisions/ADR-025-sse-single-endpoint|ADR-025]] — SSE single endpoint (replay-slot)
- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — bundle size budget (no babel)
- [[web/ui-architecture]] — header/status-indicator section updated in hiq3
- bd r82m — original countdown fix (ADR-048)
- bd hiq3 — this refactor task
