# ADR-046 — SessionMonitor: combined probe-and-publish in a single class

**Status:** Accepted
**Date:** 2026-05-17
**Task:** gektar_monitor-a4t.9

---

## Context

The `SessionMonitor` service needs to (a) probe the target site via HTTP GET `/cabinet/` to detect session expiry, and (b) publish `SseSessionExpired` on the critical `EventBus` channel when expiry is detected.

During implementation brainstorm, the architect raised an SRP concern: probing (infra concern) and publishing an event (domain concern) are two distinct responsibilities. The canonical SRP split would be:

- `SessionProbe` (Layer 2, infra adapter) — performs the HTTP probe and returns `SessionStatus`.
- `SessionMonitor` (Layer 3, service) — polls `SessionProbe` and publishes events based on the result.

## Decision

**Implement as a single combined `SessionMonitor` class** in `src/fis_monitor/services/session_monitor.py`.

The `check()` method performs the HTTP probe AND publishes `SseSessionExpired` on `EXPIRED` (or unexpected-status fail-safe).

The orchestrator accepted the coupling explicitly because:
1. The `bd`-acceptance for `a4t.9` prescribes `SessionMonitor.check() -> SessionStatus` as the deliverable unit with combined behaviour.
2. The feature is small (one method, ~30 LOC). The overhead of defining a separate `SessionProbe` Protocol + implementation + wiring exceeds the benefit at current scale.
3. There is currently only one consumer of the probe result (`SessionMonitor` itself). SRP split is beneficial when two or more consumers read the probe independently.

## Alternatives

### Split: `SessionProbe` (infra) + `SessionMonitor` (service loop)

- **Pro:** strict SRP, cleaner layer boundaries, reusable probe for diagnostics or health endpoint.
- **Con:** premature abstraction at this stage; requires new Protocol + concrete class + composition wiring for zero functional gain today.
- **Status:** deferred. A follow-up bd-task exists to perform this refactor if a second consumer of the probe result materialises.

## Consequences

- `SessionMonitor` has two injected infra dependencies: `HttpClient` and `EventBus`. This is intentional and documented.
- `container.py` field `services.session_monitor: SessionMonitor` (Layer 3 type annotation) is correct for the combined class.
- The `_NotImplementedSessionMonitor` stub (which exposed `run_forever()`) is replaced entirely. `run_forever` / periodic polling is a separate concern tracked in a follow-up bd-task.
- Future split: if a second consumer of the raw `SessionStatus` probe result appears, extract `SessionProbe` (infra, Layer 2) and reduce `SessionMonitor` to a thin scheduler wrapper.

## References

- [[architecture/03-protocols]] §3.4 SessionStatus
- [[glossary#SessionMonitor]]
- [[decisions/ADR-004-composition-root|ADR-004]] (composition root pattern)
