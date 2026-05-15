# ADR-034 — Cookie Bridge: Playwright → requests.Session (CookieStore Protocol)

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: authentication, playwright, http, session-management, cookies

---

## Context

`PlaywrightLoginSession` uses `launch_persistent_context(var/profile/)` and
stores cookies in a Chrome profile on disk.  `RequestsHttpClient` (used by
`MonitorCycleService` and `FullScanService`) wraps a `requests.Session` that is
constructed empty in `composition.py` — no cookies are loaded from the Playwright
profile.  As a result:

- Every scraping request is unauthenticated.
- The site (`xn--80aaggvgieoeoa2bo7l.xn--p1ai`) redirects to `esia.gosuslugi.ru/login`.
- The response is HTTP 200 with a login page, not the lot-list DOM.
- `SelectolaxListParser.parse()` raised `ParseBugError` (misleading) because
  `<tbody>` was absent.  24 consecutive cycles = 0 lots in DB.

Additionally, the parser was raising `ParseBugError` for session-expiry redirects
instead of a dedicated error, so callers had no way to distinguish DOM changes
from auth failures.

---

## Decision

### A: CookieStore Protocol (chosen)

1. **`CookieStore` Protocol** (in `domain/interfaces.py`) with one method:
   ```python
   def store(self, cookies: list[dict[str, object]]) -> None: ...
   ```
2. **`RequestsCookieStore`** (`infra/http/cookie_bridge.py`) — translates
   Playwright cookie dicts into `requests.cookies.RequestsCookieJar` entries
   via `requests.cookies.create_cookie` + `session.cookies.set_cookie`.
3. **`PlaywrightLoginSession`** accepts `cookie_store: CookieStore | None = None`.
   After every successful `open_headed_login()` and `silent_refresh()`, calls
   `context.cookies()` (before `browser.close()`) and passes the result to
   `cookie_store.store(...)`.
4. **Composition root** wires `RequestsCookieStore(http_session)` as the
   `cookie_store` arg to `PlaywrightLoginSession`.  Both share the same
   `requests.Session` instance.

### B: Direct injection (rejected)

Pass `requests.Session` directly into `PlaywrightLoginSession` and call
`session.cookies.set_cookie(...)` inline.  Rejected because:
- Creates a direct Playwright → requests coupling in the infra layer.
- `PlaywrightLoginSession` would import `requests`, violating the layering where
  Playwright and HTTP are independent infra sub-packages.
- Harder to test (must mock a real `requests.Session`).
- No seam for future implementations (e.g. `aiohttp` session, custom store).

### SessionExpiredError detection

`SelectolaxListParser.parse()` now checks the `<title>` tag for ESIA markers
(`"esia.gosuslugi.ru"`, `"Портал государственных услуг"`, `"Госуслуги"`) before
the `<tbody>` check.  On match → `SessionExpiredError` (not `ParseBugError`).

Detection is title-based (not full-body) because normal lot-list pages include
`esia.gosuslugi.ru` as a registration link in the navigation header.

`MonitorCycleService` and `FullScanService` catch `SessionExpiredError` at
`WARNING` level (not `ERROR`) so alert-flood is avoided during session expiry.

---

## Alternatives Considered

| Alternative | Reason Rejected |
|---|---|
| Load cookies from profile dir directly at startup | Brittle file-format coupling; profile cookie format is Chromium-internal |
| Shared `CookieJar` object (not Protocol) | Harder to test; couples infra layers; no clean Protocol seam |
| Retry HTTP requests with re-login | Complex flow; race conditions in concurrent cycles |

---

## Consequences

### Positive
- `requests.Session` is populated with valid session cookies immediately after
  every successful `open_headed_login()` or `silent_refresh()`.
- `MonitorCycleService` / `FullScanService` no longer see auth-redirects as
  `ParseBugError` — `SessionExpiredError` gives callers a clean recovery signal.
- `PlaywrightLoginSession` remains decoupled from `requests` (Protocol seam).
- `RequestsCookieStore` is independently testable with a plain `requests.Session`.

### Negative
- One extra interface (`CookieStore`) to maintain.
- `cookie_store=None` default means the bridge is opt-in — composition must wire
  it explicitly (done in `composition.py`).

---

## References

- `src/fis_monitor/domain/interfaces.py::CookieStore`
- `src/fis_monitor/infra/http/cookie_bridge.py::RequestsCookieStore`
- `src/fis_monitor/infra/playwright/login.py::PlaywrightLoginSession._export_cookies`
- `src/fis_monitor/infra/parsers/list_parser.py::SelectolaxListParser.parse`
- ADR-027 (silent cookie refresh)
- ADR-001 (Protocol not ABC)
