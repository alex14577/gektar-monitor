# ADR-030 — SseLotNew dedup: Dispatcher SSOT for SSE channel

**Date:** 2026-05-15
**Status:** Accepted
**Context:** bd gektar_monitor-b54 (SSE dedup fix)

---

## Context

`MonitorCycleService._run_cycle_inner()` contained two code paths that each
published a `SseLotNew` event for every newly-discovered lot:

1. **Direct publish** — `self._event_bus.publish(SseLotNew(lot=public_dto, ...))`
   immediately after `upsert_result.was_new = True`.
2. **Dispatcher path** — `self._notifier_dispatcher.dispatch(public_dto)` → calls
   `BrowserSseNotifier.send()` → `self._bus.publish(SseLotNew(...))`.

Both paths ran inside the same `if self._filter_matcher.matches(...)` guard, so
every new lot that passed the filter emitted **two** `lot.new` SSE events on the
browser. Connected tabs rendered each auction card twice per cycle.

## Decision

Remove the direct `event_bus.publish(SseLotNew(...))` call from
`MonitorCycleService`. The SSE channel is served exclusively through:

```
MonitorCycleService
  → NotifierDispatcher.dispatch(public_dto)
    → BrowserSseNotifier.send(lot, recipient)
      → EventBus.publish(SseLotNew(...))
```

`BrowserSseNotifier` remains the **sole publisher** of `SseLotNew` events.
`MonitorCycleService` has no direct dependency on `SseLotNew` and does not import
that model.

The filter gate (`FilterMatcher.matches`) is applied **before** `dispatch()` in
`MonitorCycleService`, so filtered-out lots are still suppressed at the service
layer — `BrowserSseNotifier` is never called for them.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Remove publish from BrowserSseNotifier, keep it in MonitorCycleService** | Breaks the Notifier Protocol contract — `BrowserSseNotifier.send()` would be a no-op, making the notifier meaningless. Violates Single Responsibility: MonitorCycleService must not know about SSE event types. |
| **Idempotency on the receive side (deduplicate by lot_id in JS)** | Hides a server-side design flaw with client complexity. Does not fix the redundant network traffic. Fragile if event structure changes. |
| **Feature-flag the direct publish path** | Unnecessary complexity; the direct path has no unique value — BrowserSseNotifier already covers it completely. |

## Consequences

**Positive:**
- `MonitorCycleService` no longer imports or constructs `SseLotNew` — the
  dependency is eliminated (cleaner layer separation, ADR-008).
- Each new lot produces exactly **one** `SseLotNew` event on the bus per cycle.
- `BrowserSseNotifier` is authoritative for the `browser` channel (consistent
  with `SmtpEmailNotifier` being authoritative for `email`).
- Filter gate remains in `MonitorCycleService` before `dispatch()` — no
  change to filtering semantics.
- Test coverage: `test_monitor_cycle.py` and `test_monitor_cycle_with_filter.py`
  assert `len([e for e in bus.published if isinstance(e, SseLotNew)]) == 0`
  when using `FakeNotifierDispatcher`, confirming the direct-publish path is gone.

**Negative / trade-offs:**
- None. The removed code path was purely redundant.

## Scope note

`SseLotStatus` events (status-change notifications) are still published directly
from `MonitorCycleService` via `event_bus.publish(SseLotStatus(...))`. That is a
different event type with no corresponding `Notifier` implementation — it goes
directly to the bus by design and is unaffected by this ADR.
