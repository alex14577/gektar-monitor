# 1. C4 Level 2 — Container diagram

Приложение — **один процесс** `fis-monitor` (Windows-бинарь у пользователя, ELF на Linux dev/хостинге). Внутри живут несколько долгоиграющих компонентов, разделённых по ответственности и потокам исполнения.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Процесс fis-monitor (uvicorn worker = 1)                                    │
│                                                                              │
│  ┌─────────────────────────────┐    ┌─────────────────────────────────────┐ │
│  │  FastAPI app (HTTP)         │    │  Lifespan / Composition root        │ │
│  │  - sync def handlers        │    │  - читает config.json               │ │
│  │  - bind 127.0.0.1:8080      │◀──▶│  - открывает state.db (per-thread)  │ │
│  │  - CSRF middleware          │    │  - собирает граф зависимостей       │ │
│  │  - Onboarding-gate mw       │    │  - стартует фоновые компоненты      │ │
│  │  - SSE endpoints (async)    │    │  - на shutdown — корректное off    │ │
│  └──────┬──────────────────────┘    └────────────┬────────────────────────┘ │
│         │ Depends() ─► фабрики                   │ создаёт                  │
│         ▼                                         ▼                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Application services (use cases) — sync, чистая логика              │   │
│  │  MonitorCycleService · EnrichmentService · FullScanService           │   │
│  │  NotifierDispatcher  · OnboardingService · LoginService              │   │
│  │  SmtpTestService     · SessionMonitor    · LotQueryService           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         │ зависят только от Protocol'ов                                      │
│         ▼                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Infrastructure adapters                                              │   │
│  │  SqliteLotRepository · RequestsHttpClient · SelectolaxListParser     │   │
│  │  SmtpEmailNotifier   · BrowserSseNotifier · PlaywrightLoginSession   │   │
│  │  SystemClock · FileLocker · WatchdogConfigSource · ThreadEventBus    │   │
│  │  WindowsAutostart · LinuxAutostart                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ─── Долгоиграющие потоки (стартуются в lifespan) ────────────────────────  │
│                                                                              │
│  [T-cycle]      MonitorCycleService.run_forever()   — 1 thread              │
│  [T-fullscan]   FullScanService.run_forever()       — 1 thread (sched)      │
│  [T-enrich]     ThreadPoolExecutor max_workers=10   — enrichment            │
│  [T-l2]         ThreadPoolExecutor max_workers=5    — L2 verification       │
│  [T-notify]     NotifierDispatcher.consumer_loop()  — 1 thread + queue      │
│  [T-watch]      watchdog Observer                   — file-watch config     │
│  [T-session]    SessionMonitor.run_forever()        — 1 thread (heartbeat)  │
│  [T-pw-login]   FastAPI threadpool (on demand)      — Playwright headed     │
│  [T-http*]      FastAPI threadpool (sync handlers)  — uvicorn default       │
│                                                                              │
│  EventBus (thread-safe, queue.Queue) ── мост sync producers → async SSE     │
│  ─► fan-out на N подписчиков (вкладки) через per-subscriber Queue           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
        │                            │                          │
        ▼                            ▼                          ▼
   SQLite (WAL)              ФИС/ЕСИА HTTP             SMTP-сервер
   state.db                  через requests           (бот-ящик или
   (один файл,               + Playwright             override пользователя)
   per-thread conn)          (headed login)
```

**CSRF / DNS-rebinding middleware** (см. [[decisions/ADR-011-dns-rebinding-host-allowlist|ADR-011]]):
- **Strict Host allow-list**: `127.0.0.1:8080`, `localhost:8080`. Любой иной Host → **421 Misdirected Request** (защита от DNS-rebinding: вредоносный сайт резолвит `attacker.example` → `127.0.0.1` и шлёт POST; Host-проверка отсекает).
- **Origin/Referer whitelist** (не «непустой»): `http://127.0.0.1:8080`, `http://localhost:8080`. Любой иной — 403.
- **X-CSRF-Token** (secure-cookie + header) для ВСЕХ state-changing методов. Это включает (R4-M3) `POST /auth/cancel` (отмена активного headed-login) — раньше упускалось из обзора, теперь явно: под глобальным CSRF middleware, никаких исключений. Rate-limit 1 req/s через `Idempotency-Key` (защита от спама cancel-кликами).

**Ключевые свойства:**

- **Один процесс, много потоков.** Никакого subprocess для Playwright (см. [[decisions-log]]). Никакого multiprocessing.
- **SQLite** — sync, **один коннект на поток** (`sqlite3.connect` не thread-safe для шаринга). Параллелизм writers разруливается `PRAGMA busy_timeout=5000`, единый Python-lock НЕ нужен.
- **EventBus** — единственный мост между sync-миром (background-таски) и async-миром (SSE generators в FastAPI). Реализация — `queue.Queue` + `loop.run_in_executor(None, q.get)`.
- **Lifespan owns lifecycle.** Composition root собирается в `lifespan(app)`, там же стартуются и останавливаются все потоки. Никаких `threading.Thread(daemon=True)` где попало.
