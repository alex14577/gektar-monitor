# ADR-011: DNS-rebinding защита — strict Host allow-list

**Context.** Bind на 127.0.0.1 не защищает от DNS-rebinding: вредоносный сайт резолвит `attacker.example` → `127.0.0.1`, далее браузер шлёт POST на наш endpoint с правильным Cookie.

**Decision.** Middleware `CsrfHostOriginMiddleware` (pure-ASGI, см. [[glossary#CsrfHostOriginMiddleware]]):
- **Host header**: только `127.0.0.1:<port>` или `localhost:<port>`. Иное → **421 Misdirected Request**.
- **Origin**: whitelist `http://127.0.0.1:<port>`, `http://localhost:<port>`. Иное → **421 Misdirected Request** (унифицировано с Host-mismatch, см. ниже). Strict-Origin only — Referer fallback не используется.
- Применяется только к **state-changing methods**: POST/PUT/PATCH/DELETE. GET/HEAD/OPTIONS пропускаются.
- Port параметризован: фабрика `loopback_csrf_config(port)` возвращает `(host_allowlist, origin_whitelist)` frozensets — SSOT для всех точек сборки.

**Унификация status code (правка vs первая редакция ADR).** Изначально планировалось 421 для Host-mismatch и 403 для Origin-mismatch. Унифицировали оба до **421 Misdirected Request**:
- Единый failure mode = единая операционная процедура (debug/alerting/logs).
- 421 семантически точнее для обоих случаев: запрос попал на host, не совпадающий с тем, для которого предназначался.
- 403 был бы оправдан если бы Origin-проверка опиралась на cookie/токен (authentication context). Здесь — чисто whitelist, никакой identity не валидируется.

**Referer не используется.** Современные браузеры шлют `Origin` на state-changing запросы (включая SSE). Strict-Origin-only безопаснее, чем Origin-OR-Referer (атакующий не контролирует Origin, но Referer может быть подделан/опущен через Referrer-Policy).

**Consequences.** Защита от DNS-rebinding на уровне приложения. EventSource (SSE) всегда шлёт same-origin Origin — не ломается. CSRF + Host allow-list = двойной контур. Все mismatch-ответы — 421; нет неоднозначности «когда 403, когда 421».

**Расширение для non-loopback бинда (ADR-043).** Для dev-сценария WSL→Windows без localhost-forwarding allowlist расширяется через `csrf_config_for_bind(host, port)` — добавляются адреса сетевых интерфейсов машины. Дефолт и все security-инварианты сохраняются. See [[decisions/ADR-043-non-loopback-bind-for-wsl|ADR-043]].

См. также: [[decisions-log]], [[architecture/01-container-diagram]], [[web/authentication]].

## Addendum — Reflected absolute URLs in GET responses (2026-05-17, bd 9u7)

**Problem.** Starlette's `url_for()` constructs absolute URLs from the request's
untrusted `Host` header. The safe-method bypass in this ADR intentionally lets
GET requests through without Host validation; a spoofed `Host: evil.com` therefore
reflected into static-asset `src`/`href` attributes
(`http://evil.com/static/auth.js`). Loopback-only deployment limits exposure,
but a compromised local process can still craft the header.

**Resolution.** All `url_for('static', path=...)` calls in `base.html.jinja`
replaced with root-relative path literals (`/static/...`). Relative paths
resolve against the browser's current origin and carry no host component, so
the reflection vector disappears at the source. No middleware change required.

**Rejected alternatives.**
- *Extend Host validation to GET* — breaks legitimate reverse-proxy and
  health-check scenarios; a scope change requiring its own ADR.
- *Document as known limitation* — defers the risk without eliminating it;
  Variant B costs nothing and closes the gap.

**Residual surface.** `url_for()` in route-level Python (redirects' `Location`
headers) still pulls from the request — but the target names there
(`onboarding_step`, `login_page`, ...) are server-controlled, not
user-controlled values, so no reflection occurs.
