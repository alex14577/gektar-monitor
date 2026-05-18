# ADR-049 — Browser Notification Activation Flow

**Status:** Accepted
**Date:** 2026-05-18
**Task:** bd gektar_monitor-sv97

## Context

`BrowserSseNotifier` (registered in composition.py) correctly publishes `lot.new` SSE events.
The htmx-sse extension inserts the HTML fragment into `#feed` on `sse-swap="lot.new"` — this
worked. However, the JS side had two blockers:

1. `Notification.requestPermission()` was never called → `Notification.permission` stayed
   `'default'` → `maybeBrowserNotify` silently returned.
2. `onLotNew` existed but was never wired to the SSE stream; the `// SSE stub` block was
   commented out. htmx DOM-swap happened, but the JS handler was never invoked.

## Decision

**Permission activation — one-shot `click` listener on `document.body`.**

Rationale: Permissions Policy requires a user gesture before
`Notification.requestPermission()` may be called. A dedicated "Enable notifications" button
would require template changes and a UI state machine (show/hide based on current permission).
The one-shot body-click approach piggybacks on the first natural interaction (any click), is
invisible to the user (unless permission is granted → toast appears), and needs zero HTML
changes. Denied/granted states are sticky in the browser so the listener is a no-op on return
visits. Simple, low-coupling.

**SSE wiring — `htmx:sseMessage` on `document.body`.**

The htmx-sse extension (vendored, [[decisions/ADR-029-htmx-sse-vendor|ADR-029]]) fires a `htmx:sseMessage` CustomEvent on
`document.body` for every incoming SSE message, AFTER performing its own sse-swap. We
listen for `event.detail.type === 'lot.new'` and look up `feed.firstElementChild` — the node
just inserted by htmx — to read notification data without parsing HTML strings.

**Data extraction — `data-title` and `data-area` attributes on `<article>`.**

The SSE payload is an HTML fragment (`_lot_poster.html.jinja`), not JSON ([[decisions/ADR-025-sse-single-endpoint|ADR-025]], [[decisions/ADR-030-html-fragment-sse-payload|ADR-030]]).
Rather than parsing the HTML string in JS, two data attributes were added to the `<article>`
root element rendered by the template:
- `data-title` = `"{{ lot.region }}{% if lot.district %}, {{ lot.district }}{% endif %}"`
- `data-area`  = `"{{ lot.area_ha }}"`

The `onLotNew` function was extended to accept either a plain object (legacy path) or an
HTMLElement, extracting fields from `dataset` in the latter case.

## Alternatives Considered

| Alternative | Rejected because |
|---|---|
| Dedicated "Enable notifications" button in header | Requires template changes + JS state; higher coupling |
| Parse `event.detail.data` HTML string in JS | Fragile (depends on template internals); `data-*` is a stable contract |
| Auto-request on page load | Blocked by Permissions Policy — browsers silently deny; bad UX |
| Separate EventSource (bypass htmx-sse) | Duplicates connection; violates [[decisions/ADR-025-sse-single-endpoint|ADR-025]] single-endpoint |

## Consequences

- `Notification.requestPermission()` is called on the first qualifying user click if permission
  is `'default'`. DND-preset button clicks are excluded (not a meaningful "I want notifications"
  gesture). A preview toast appears first to explain the upcoming system dialog. On `'granted'`
  a confirmation toast follows. On `'denied'` the `maybeBrowserNotify` denied-path shows a
  one-time toast (stored in localStorage) explaining how to re-enable in browser settings.
  On subsequent page loads the listener is not attached (permission already `'granted'` or
  `'denied'`).
- `onLotNew` is called for every `lot.new` SSE event, triggering sound, browser notification,
  and aria-live announcement — consistent with the intent documented in `docs/notifications.md`.
- The existing htmx sse-swap `#feed` insertion continues to work unchanged (ADR invariant).
- `_lot_poster.html.jinja` now carries `data-title` and `data-area` as a stable JS/template
  contract. Changes to those attributes must be coordinated with app.js.
