# ADR-027 — Silent Cookie Refresh (headless silent_refresh)

**Status**: Accepted  
**Date**: 2026-05-15  
**Deciders**: Backend Architect  
**Tags**: authentication, playwright, session-management

---

## Context

The monitoring service relies on cookies obtained via a headed Playwright login
session (ADR-021).  Sessions expire after a finite window.  When a session is
about to expire (detected by `SessionProbe.check()` returning `EXPIRING`), the
user should see a "session expiring soon" banner, but ideally re-authentication
should happen without requiring interactive re-login.

The site (`надальнийвосток.рф`) uses Gosuslugi OAuth with session cookies.  If
the cookie is still valid but close to expiry, navigating to `/cabinet/` (which
requires authentication) refreshes the session server-side — without any
interactive login form.  This can be done in a headless Playwright context, re-
using the existing profile directory where cookies are stored.

### Problem
- A full headed `open_headed_login()` requires user interaction.
- Re-login overhead is ~60 s; silent refresh overhead is ~5–10 s.
- Without silent refresh, the user is interrupted for re-login too frequently.

---

## Decision

Add `PlaywrightLoginSession.silent_refresh(deadline=float)` that:

1. **Acquires the shared `_lock`** (same lock as `open_headed_login`) to prevent
   concurrent Playwright sessions.
2. Launches `chromium.launch_persistent_context` with **`headless=True`** — no
   visible browser window.
3. Navigates to `_LOGIN_START_URL` (`/cabinet/`), then waits for the URL to match
   `_LOGIN_SUCCESS_URL_GLOB` using `page.wait_for_url(timeout=remaining_ms)`.
4. On success → cookies in the profile directory are refreshed → returns
   `LoginOutcome(success=True, cookies_updated=True)`.
5. On `wait_for_url` timeout → URL did not match (session truly expired, needs
   manual re-login) → returns `LoginOutcome(success=False, error="needs_manual_login")`.
6. All mapped Playwright exceptions use the same `_map_exception` helper as the
   headed flow (PII-safe, closed `LoginErrorHint` enum).
7. Deadline is a monotonic absolute timestamp; `remaining_ms` is computed as
   `(deadline - start) * 1000` (same as `_run_login` after P0-1 fix).

### Silent-refresh deadline
Default deadline: `clock.monotonic() + 30.0` (30 s).  Much shorter than the
headed-login 300 s deadline, since silent refresh is expected to complete within
a few seconds.

---

## Alternatives Considered

| Alternative | Reason Rejected |
|---|---|
| Re-use existing `open_headed_login()` | Requires user interaction; UX disruption |
| Plain HTTP cookie refresh via `requests` | Site OAuth flow is not a simple HTTP cookie exchange; Playwright is needed |
| Schedule background cron-style refresh | Over-engineered for MVP; probe-triggered is simpler |

---

## Consequences

### Positive
- Session lifetime extended transparently when cookies are still partially valid.
- No user interaction required for the common case.
- Shares `_lock` and profile directory with `open_headed_login` → no concurrent
  sessions, no profile corruption.

### Negative
- Requires Playwright runtime even for refresh (no lightweight HTTP-only path).
- Silent refresh failure still requires a manual re-login fallback.
- `BusyError` raised if `open_headed_login()` is in progress when refresh is
  triggered — caller must handle retry.

---

## References

- `src/fis_monitor/infra/playwright/login.py` — implementation
- ADR-021: manual STARTTLS / connect-by-IP (same layer)
- `docs/data-model/settings.md §SmtpCredentials`
