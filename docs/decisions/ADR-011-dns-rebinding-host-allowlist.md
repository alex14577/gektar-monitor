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

См. также: [[decisions-log]], [[architecture/01-container-diagram]], [[web/authentication]].
