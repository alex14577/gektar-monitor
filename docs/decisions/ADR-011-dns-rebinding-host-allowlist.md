# ADR-011: DNS-rebinding защита — strict Host allow-list

**Context.** Bind на 127.0.0.1 не защищает от DNS-rebinding: вредоносный сайт резолвит `attacker.example` → `127.0.0.1`, далее браузер шлёт POST на наш endpoint с правильным Cookie.

**Decision.** Middleware:
- **Host header**: только `127.0.0.1:8080` или `localhost:8080`. Иное → **421 Misdirected Request**.
- **Origin/Referer**: whitelist `http://127.0.0.1:8080`, `http://localhost:8080`. Иное → 403. НЕ «непустой» — точное совпадение.

**Consequences.** Защита от DNS-rebinding на уровне приложения. EventSource (SSE) всегда шлёт same-origin Origin — не ломается. CSRF + Host allow-list = двойной контур.

См. также: [[decisions-log]], [[architecture/01-container-diagram]], [[web/authentication]].
