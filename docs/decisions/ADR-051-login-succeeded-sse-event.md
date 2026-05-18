# ADR-051 — `login.succeeded` SSE event for post-login UI recovery

**Status:** Accepted  
**Date:** 2026-05-18  
**Issue:** bd gektar_monitor-fplb

---

## Context

After a successful headed Playwright login the UI was left in a stale state until
the next monitor cycle completed (or the user manually pressed "Проверить сейчас"):

1. `#header-status` auth-chip still showed "Сессия истекла, нужен вход" (`state="error"`).
2. `#cycle-result` displayed "Проверка завершена с ошибкой" — a fragment replayed from
   the pre-login `SseCycleDone(status="error")` event stored in `ThreadEventBus._last_normal`
   (the replay-slot that re-delivers the last normal event of each type on SSE reconnect).

Root cause: `composition._trigger_backfill_on_login` launched backfill but published no
SSE events, so the stale replayed fragments were never overwritten.

---

## Decision

Introduce a new normal-priority SSE domain event **`SseLoginSucceeded`** (`event: "login.succeeded"`).

The event is published from the composition root `on_login_success` callback — NOT from
`LoginService` — to preserve low-coupling / DI invariant (LoginService must not own an
`EventBus` dependency).  Two events are published unconditionally on every successful
headed login, before the onboarding/regions/supervisor backfill guards:

1. **`SseStatus(state="active", ...)`** — overwrites the stale `_last_normal["status"]`
   replay slot and updates the `#header-status` auth-chip immediately.
2. **`SseLoginSucceeded`** — its HTML fragment uses `hx-swap-oob` to clear `#cycle-result`
   (drops the pre-login cycle error message) and reset `#session-expired-modal` to `hidden`.

The callback logic is extracted into a module-level factory
`make_login_success_callback(*, clock, config_source, lot_repo, event_bus, onboarding,
backfill, supervisor_cell)` for testability (SRP — wiring separate from callback logic).

---

## Alternatives Considered

### (b) Extend `SseStatus` with a `session_state` field

Add `session_state: Literal["active", "expired"]` to `SseStatus` so the single `status`
event drives both the auth-chip and the cycle-result clearing via JS logic in the template.

Rejected: entangles two orthogonal concerns in a single event; requires template JS to
conditionally wipe `#cycle-result` based on a field value — low cohesion.

### (c) Trigger a full monitor cycle on login success

On login success, schedule a cycle immediately so a fresh `SseCycleDone(status="ok")`
naturally overwrites the stale error fragment.

Rejected: couples UI recovery to backfill latency (cycle takes 10-60 s); during that
window the UI still shows the error.  Also masks the root problem rather than fixing it.

### (d) Clear `_last_normal` replay slot on login

Purge the bus's last-normal map when login succeeds so reconnecting clients get no
stale replay.

Rejected: the `_last_normal` mechanism is correct and useful for all other event types;
changing it for one edge case would be a non-local side effect. Publishing a fresh event
is the natural bus-native fix.

---

## Consequences

- New domain type `SseLoginSucceeded` added to `domain/models.py` and `SseEvent` union.
- New encoder branch in `web/sse_encoder._encode_login_succeeded` + template
  `partials/_login_succeeded.html.jinja`.
- New SSE listener `#login-succeeded-listener` in `base.html.jinja`.
- `make_login_success_callback` factory extracted from `build_container` — improves
  testability; `build_container` delegates to it.
- Tests: `tests/unit/composition/test_login_callback.py` (5 invariants, Layer 2/3) +
  `tests/unit/web/test_sse_encoder.py` extended with 2 encoder invariants (Layer 4).

### Replay-TTL and stale-overwrite-race fix (fix-round 1)

`SseLoginSucceeded` has `priority="normal"` which places it in
`ThreadEventBus._last_normal["login.succeeded"]` (see `infra/sse/bus.py`).
The 30s TTL window (ADR-008 design — long enough to bridge a tab reload or
SSE reconnect) creates a **stale-overwrite race**: if a fresh `SseCycleDone(ok)`
is published during the 30s window after login, an SSE reconnect would replay
events in insertion order — `cycle.done(ok)` first, then `login.succeeded` OOB-wipe
second — erasing the fresh cycle result.

**Fix**: after publishing `SseLoginSucceeded`, the callback immediately calls
`event_bus.evict_normal_replay("login.succeeded")` (guarded by
`isinstance(event_bus, ThreadEventBus)`). This is an extension method on the
concrete impl — same pattern as `last_critical()` — not part of the `EventBus`
Protocol. The eviction is idempotent and thread-safe (acquires `self._lock`).

After the fix:

- `SseLoginSucceeded` is delivered to **live subscribers** (those connected at
  the moment of publish) — the OOB-wipe reaches the current browser tab.
- **Replay on reconnect is suppressed** — the slot is evicted before any future
  subscriber's `subscribe()` replay scan can pick it up.
- Both flash-race and stale-overwrite-race are eliminated.

See `[[architecture/07-concurrency]]` §7.3 for the dual-circuit routing rules
and `infra/sse/bus.py` for `_last_normal` slot lifecycle details.

---

## References

- [[decisions/ADR-025-sse-single-endpoint|ADR-025]] — SSE routing: единственный роут `/events`
- [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] — EventBus dual-circuit, `_last_normal` replay
- [[architecture/03-protocols]] — Protocol definitions, low-coupling rules
- [[web/api-reference]] — SSE events table (updated)
