# 4. Composition root

**Без сторонних DI-контейнеров.** `dependency-injector` слишком тяжёл для ~15 швов, `inject` использует декораторы (магия). Делаем явный Container class — ~150 строк, всё ручками, всё типизировано.

## 4.1 Структура (раздроблено, не God-Container)

После ревью Container раздроблен на два sub-датакласса (плюс опц. `Lifecycle` под supervisor-handles). Цель — high cohesion внутри группы, видимая граница «инфра vs прикладной слой», нет God-объекта. Оба `repr=False` (security — против утечки secrets в crash-логи через `__repr__`).

```python
# src/fis_monitor/container.py
from dataclasses import dataclass

@dataclass(frozen=True, repr=False)
class Infra:
    """Системные швы, репозитории, инфра-адаптеры. Не меняется в runtime.

    NB: «Layer 0..2» внутри — текущий снимок зависимостей, не догма.
    При добавлении нового шва: новое поле — в конец dataclass, инициализация —
    в правильном топологическом месте build_container(). Расхождение порядков
    не critical (frozen=True всё равно даёт immutability).
    R3-minor: Infra сейчас ~17 полей, нет автоматической проверки порядка
    топосортировки в build_container — это ответственность ревьюера. При
    cyclic-dep будет TypeError на конструировании; import-linter не покрывает."""
    # Layer 0 — системные швы без зависимостей
    clock: Clock
    event_bus: EventBus
    conn_provider: ThreadLocalConnectionProvider   # КОНКРЕТНЫЙ класс (не Protocol)
    locker: Locker
    config_source: ConfigSource
    cycle_progress_signal: threading.Event         # N-M8: soft-yield координатор

    # Layer 1 — репозитории (зависят от conn_provider)
    lot_repo: LotRepository
    user_state_repo: UserStateRepository
    settings_repo: SettingsRepository
    notif_repo: NotificationsRepository
    cycles_repo: CyclesRepository
    smtp_creds_repo: SmtpCredentialsRepository

    # Layer 2 — инфра-адаптеры
    http_client: HttpClient
    list_parser: ListParser
    detail_parser: DetailParser
    login_session: LoginSession
    session_probe: SessionProbe
    autostart: AutostartManager
    smtp_host_policy: SmtpHostPolicy               # N-C3: infra-policy для host-валидации

@dataclass(frozen=True, repr=False)
class Services:
    """Use cases. Зависят только от Protocol'ов из Infra."""
    notifier_dispatcher: NotifierDispatcher
    monitor_cycle: MonitorCycleService
    enrichment: EnrichmentService
    full_scan: FullScanService
    onboarding: OnboardingService
    login: LoginService
    settings_service: SettingsService              # R3-M9: write-side для config + smtp_credentials
    smtp_test: SmtpTestService                     # R3-M9: одноразовая отправка тест-письма
    session_monitor: SessionMonitor
    diagnostics: DiagnosticsService
    lot_query: LotQueryService                     # read-side для web

@dataclass(repr=False)   # не frozen — supervisor-handles могут ребиндиться
class Container:
    infra: Infra
    services: Services
```

`NotifierRegistry` сюда **не входит** — это composition-internal вещь, она нужна только в `build_container` для конструирования `NotifierDispatcher`, дальше живёт внутри Dispatcher'а.

## 4.2 Сборка — топологическая сортировка зависимостей

Граф зависимостей строится в порядке **топологической сортировки**. Нумерация Layer 0..4 ниже — текущий снимок для читателей, **не инвариант**. Цикл в зависимостях = ошибка, проверяется `import-linter` в CI ([[decisions/ADR-006-import-linter-ci|ADR-006]]).

**Запрет**: обратные ссылки между равными уровнями и любые «вверх по слою». Если зависимость требует ссылки на use case из своего слоя — это знак неправильной декомпозиции.

```python
# src/fis_monitor/composition.py
def build_container(settings: Settings, data_dir: Path) -> Container:
    # ── Layer 0: системные швы без зависимостей ─────────────────────────────
    clock = SystemClock()
    event_bus = ThreadEventBus()
    conn_provider = ThreadLocalConnectionProvider(db_path=data_dir / "state.db")
    locker = FileLocker(path=data_dir / "app.lock")  # OS-lock внутри
    config_source = WatchdogConfigSource(path=data_dir / "config.json")

    # ── Layer 1: репозитории (deps только на Layer 0) ───────────────────────
    lot_repo = SqliteLotRepository(conn_provider, clock)
    user_state_repo = SqliteUserStateRepository(conn_provider, clock)
    settings_repo = SqliteSettingsRepository(conn_provider, clock)
    notif_repo = SqliteNotificationsRepository(conn_provider, clock)
    cycles_repo = SqliteCyclesRepository(conn_provider, clock)
    smtp_creds_repo = SqliteSmtpCredentialsRepository(conn_provider, clock)

    # ── Layer 2: инфра-адаптеры HTTP / Parser / Login (deps на Layer 0) ─────
    http_client = RequestsHttpClient(
        cookies_dir=data_dir / "profile", timeout=30.0, clock=clock,
    )
    list_parser = SelectolaxListParser()
    detail_parser = SelectolaxDetailParser()
    login_session = PlaywrightLoginSession(profile_dir=data_dir / "profile")
    session_probe = HttpSessionProbe(http=http_client)
    autostart = build_autostart()                     # выбор по sys.platform
    # R4-M12: SmtpHostPolicy — чистая логика, без deps (использует socket.getaddrinfo
    # на каждый вызов; никакого state). Создаётся в Layer 2 рядом с smtp/.
    smtp_host_policy = DefaultSmtpHostPolicy()

    infra = Infra(
        clock=clock, event_bus=event_bus, conn_provider=conn_provider,
        locker=locker, config_source=config_source,
        lot_repo=lot_repo, user_state_repo=user_state_repo,
        settings_repo=settings_repo, notif_repo=notif_repo,
        cycles_repo=cycles_repo, smtp_creds_repo=smtp_creds_repo,
        http_client=http_client, list_parser=list_parser,
        detail_parser=detail_parser, login_session=login_session,
        session_probe=session_probe, autostart=autostart,
        smtp_host_policy=smtp_host_policy,
    )

    # ── Layer 3: Notifiers + registry (собирается ДО dispatcher) ────────────
    # NB: в production-graph БЕЗ with_retry — retry-логика в Dispatcher
    # (N-M4: видит NotificationsRepository → durable retry поверх рестартов).
    registry = ExplicitNotifierRegistry()
    registry.register(SmtpEmailNotifier(
        smtp_creds_repo=smtp_creds_repo, config_source=config_source,
        clock=clock, host_policy=smtp_host_policy,
    ))
    registry.register(BrowserSseNotifier(event_bus=event_bus))
    registry.register(HeartbeatNotifier(lot_repo=lot_repo, clock=clock))

    # ── Layer 4: use cases ──────────────────────────────────────────────────
    # Dispatcher строится первым — его зависят monitor_cycle/full_scan.
    # Retry-policy внутри: attempts=3, backoff=(2,4,8) с jitter, NotifyResult.
    # retryable → новая попытка; mark_attempt() ПЕРЕД send → idempotent поверх
    # рестартов (см. [[notifications]]).
    notifier_dispatcher = NotifierDispatcher(
        registry=registry, notif_repo=notif_repo,
        config_source=config_source, clock=clock, event_bus=event_bus,
        retry_attempts=3, retry_backoff=(2.0, 4.0, 8.0),
    )
    monitor_cycle = MonitorCycleService(
        http=http_client, list_parser=list_parser,
        lot_repo=lot_repo, cycles_repo=cycles_repo,
        notifier_dispatcher=notifier_dispatcher,
        config_source=config_source, clock=clock, event_bus=event_bus,
    )
    enrichment = EnrichmentService(
        http=http_client, detail_parser=detail_parser, lot_repo=lot_repo,
        config_source=config_source, clock=clock, event_bus=event_bus,
    )
    full_scan = FullScanService(...)
    onboarding = OnboardingService(settings_repo=settings_repo, smtp_creds_repo=smtp_creds_repo, ...)
    login = LoginService(login_session=login_session, event_bus=event_bus, ...)
    settings_service = SettingsService(
        config_source=config_source, settings_repo=settings_repo,
        smtp_creds_repo=smtp_creds_repo, smtp_host_policy=smtp_host_policy, clock=clock,
    )
    smtp_test = SmtpTestService(
        smtp_creds_repo=smtp_creds_repo, smtp_host_policy=smtp_host_policy,
        config_source=config_source, clock=clock, settings_repo=settings_repo,
    )
    session_monitor = SessionMonitor(session_probe=session_probe, event_bus=event_bus, clock=clock)
    diagnostics = DiagnosticsService(data_dir=data_dir, conn_provider=conn_provider)
    lot_query = LotQueryService(lot_repo=lot_repo, user_state_repo=user_state_repo)

    services = Services(
        notifier_dispatcher=notifier_dispatcher,
        monitor_cycle=monitor_cycle, enrichment=enrichment, full_scan=full_scan,
        onboarding=onboarding, login=login,
        settings_service=settings_service, smtp_test=smtp_test,
        session_monitor=session_monitor, diagnostics=diagnostics, lot_query=lot_query,
    )
    return Container(infra=infra, services=services)
```

## 4.3 Подключение к FastAPI

FastAPI `Depends()` — провайдер, читающий из контейнера, который лежит в `app.state.container`:

```python
# src/fis_monitor/web/deps.py
from fastapi import Request

def get_container(request: Request) -> Container:
    return request.app.state.container

def get_lot_query(c: Container = Depends(get_container)) -> LotQueryService:
    return c.services.lot_query

def get_onboarding(c: Container = Depends(get_container)) -> OnboardingService:
    return c.services.onboarding

# ... по одному провайдеру на use case
```

Роут зависит **только от use case**, не от контейнера:

```python
# src/fis_monitor/web/routes/lots.py
@router.get("/")
def feed(
    request: Request,
    lots: LotQueryService = Depends(get_lot_query),
):
    dto = lots.recent_feed(limit=50)
    return templates.TemplateResponse("feed.html.jinja", {...})
```

## 4.3.bis ThreadSupervisor + two-phase shutdown ([[decisions/ADR-014-two-phase-shutdown|ADR-014]])

```python
# src/fis_monitor/infra/thread_supervisor.py
class ThreadSupervisor:
    """Список Thread'ов + общий stop_event + two-phase shutdown."""
    def start(self, name: str, target: Callable[[threading.Event], None]) -> None: ...
    def shutdown(self, grace_timeout: float = 35.0) -> ShutdownReport: ...
    # Two-phase shutdown:
    #   Phase 1 (graceful, grace_timeout):
    #     1) stop_event.set()
    #     2) join каждого потока с deadline = now + grace_timeout
    #   Phase 2 (forceful, при истечении grace):
    #     3) для каждого pending thread — WARN с именем + stack-trace
    #        (через faulthandler.dump_traceback по thread.ident);
    #     4) executor'ы (enrichment_pool, pw_executor, sse_executor) →
    #        shutdown(wait=False, cancel_futures=True);
    #     5) dangling threads помечены daemon=True при start() — Python
    #        прибьёт их при interpreter exit.
    # ShutdownReport — для лога и тестов: clean: bool, pending: list[str].

    # Под капотом: общий threading.Event, передаётся в target. Use case-ы:
    #   def run_forever(self, stop_event: threading.Event) -> None:
    #       while not stop_event.wait(self._next_delay):
    #           if stop_event.is_set(): return
    #           # ... обязательная проверка ВНУТРИ итерации
    #           # — между батчами full_scan, между группами enrichment,
    #           #   после каждого fetched лота. См. [[architecture/07-concurrency]] §7.1.
```

**Network-timeouts ≤ grace_timeout - 5s — обязательный инвариант** ([[decisions/ADR-014-two-phase-shutdown|ADR-014]]).
Дефолты:
- `grace_timeout = 35s` (HTTP read + поправка 10s).
- HTTP `timeout=(10, 25)` — `(connect=10, read=25)`. `RequestsHttpClient` НЕ имеет неограниченных reads.
- SMTP: connect=10s + send=20s + close=5s = **≤30s суммарно**. Не делаем `SMTP.send_message()` без timeout — это бесконечный socket.recv в smtplib.
- Playwright: navigation_timeout=20s, action_timeout=10s.

Без этого инварианта `supervisor.shutdown(timeout=10)` гарантированно срывал бы в WARN при любом длинном запросе — баг second-round review.

**`cycle_in_progress` флаг** (N-M8, [[decisions/ADR-005-concurrency-soft-yield-retry-busy|ADR-005]] расширен):
- `threading.Event` (in-memory shared объект), внедрённый DI в `MonitorCycleService` (set/clear) и `EnrichmentService` (read + `sleep(50ms)`).
- НЕ персистится в БД. Потеря на рестарте — OK, это soft-yield координация, а не durable flag.
- Поле в `Infra`: `cycle_progress_signal: threading.Event`. `set()` в начале цикла, `clear()` в `finally` блока цикла.

## 4.4 Lifespan

```python
# src/fis_monitor/app.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_initial_settings()
    data_dir = resolve_data_dir()           # platformdirs
    warn_if_in_cloud_sync(data_dir)         # OneDrive/Dropbox/Yandex/%USERPROFILE%\Documents

    lock_handle = FileLocker(data_dir / "app.lock").acquire()  # OS-lock + EX

    container = build_container(settings, data_dir)
    app.state.container = container

    # ── Выделенные executor'ы (НЕ дефолтный anyio threadpool) ──────────────
    # Playwright headed-login: один долгоживущий Playwright() instance.
    pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw-login")
    container.services.login.bind_executor(pw_executor)
    # SSE q.get — отдельный пул, не делится с FastAPI handler'ами.
    sse_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sse-wait")
    app.state.sse_executor = sse_executor
    # Enrichment.
    enrichment_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="enrich")
    container.services.enrichment.bind_executor(enrichment_pool)

    supervisor = ThreadSupervisor()
    supervisor.start("monitor-cycle",   container.services.monitor_cycle.run_forever)
    supervisor.start("full-scan",       container.services.full_scan.run_forever)
    supervisor.start("session-monitor", container.services.session_monitor.run_forever)
    supervisor.start("notifier",        container.services.notifier_dispatcher.consumer_loop)

    config_subscription = container.infra.config_source.subscribe(
        container.services.monitor_cycle.on_config_reload
    )

    app.state.supervisor = supervisor
    app.state.enrichment_pool = enrichment_pool
    app.state.pw_executor = pw_executor

    try:
        yield
    finally:
        # Three-phase shutdown ([[decisions/ADR-014-two-phase-shutdown|ADR-014]], R3-C3).
        # R4-M4: КАЖДАЯ фаза в своём try/except — lock_handle.release() ГАРАНТИРОВАН
        # в самом-внешнем finally. Если phase 1 кинет неожиданное исключение —
        # phase 1.5/2 всё равно выполнятся, и lock не залипнет.

        # Phase 1: graceful (stop_event + join 35s) для обычных потоков
        # БЕЗ pw_executor (Playwright headed-login не реагирует на stop_event —
        # блокирующий C-extension wait_for_url). UI должен показывать
        # «закройте окно браузера для остановки» если headed-login активен.
        try:
            report = supervisor.shutdown(grace_timeout=35.0)
            if not report.clean:
                logger.warning("dangling threads at shutdown: %s", report.pending)
        except Exception:
            logger.exception("phase 1 shutdown failed")

        # Phase 1.5 (R3-C3): отменяем активный headed-login извне worker-thread.
        # LoginService.cancel_active_job() звонит browser.close() — page.wait_for_url
        # внутри pw-login worker'а развернётся с TargetClosedError за ~2-3 секунды,
        # job завершится корректно.
        try:
            container.services.login.cancel_active_job()
        except Exception:
            logger.exception("login cancel failed")

        # R4-M1: pw_executor.shutdown(wait=True) может зависнуть при zombie
        # Chromium-процессе (browser.close() ушёл в C-extension и не вернулся).
        # ThreadPoolExecutor.shutdown() сам timeout не принимает — оборачиваем
        # вручную в Thread+join(5.0). При истечении timeout — лог warning,
        # Chromium останется zombify до interpreter exit (приемлемо для shutdown).
        try:
            shutdown_thread = threading.Thread(
                target=lambda: pw_executor.shutdown(wait=True, cancel_futures=True),
                daemon=True, name="pw-shutdown",
            )
            shutdown_thread.start()
            shutdown_thread.join(timeout=5.0)
            if shutdown_thread.is_alive():
                logger.warning(
                    "pw_executor.shutdown timed out; Chromium may zombify "
                    "until interpreter exit"
                )
        except Exception:
            logger.exception("pw_executor shutdown failed")

        # Phase 2: forceful — остальные executor'ы.
        try:
            enrichment_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("enrichment shutdown failed")
        try:
            sse_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("sse_executor shutdown failed")
        try:
            config_subscription.unsubscribe()
        except Exception:
            logger.exception("config unsubscribe failed")
        # close_all коннектов — ТОЛЬКО после phase 2, иначе writers упадут
        # с SQLITE_MISUSE при попытке докоммитить.
        try:
            container.infra.conn_provider.close_all()
        except Exception:
            logger.exception("conn close failed")

        # CRITICAL: lock release — самый внешний try/except. Если ничего другого
        # не отработало, lock-файл должен освободиться (иначе при следующем
        # старте процесс зависнет на «Already running» с мёртвым PID).
        try:
            lock_handle.release()
        except Exception:
            logger.exception("lock release failed — manual cleanup may be required")
```

> **Cloud-sync detection** (`warn_if_in_cloud_sync`, [[decisions/ADR-010-data-dir-location-policy|ADR-010]]): большой `logger.warning` + баннер в UI если `data_dir.resolve()` попадает в один из паттернов. Используем `os.path.realpath()` (резолв symlinks/junction points) и расширенный список: `OneDrive*`, `Dropbox*`, `Yandex.Disk*`/`YandexDisk*`, `%USERPROFILE%\Documents`, `Google Drive` / `GoogleDrive*`, `iCloudDrive`, `rclone`, `pCloud`, `MEGA`, `Sync.com`, `Box`. SQLite-WAL и облачная синхронизация = коррапт.
>
> **R4-M7 — audit.jsonl fail-closed в cloud-sync.** Если `warn_if_in_cloud_sync` сматчил cloud-sync паттерн, `audit.jsonl` writer заменяется на **no-op** (никаких open()-w, никаких записей). Причина: `audit.jsonl` содержит полный PII config-diff (recipients, smtp.host) — попадание в облачную копию = утечка ВНЕ controlled-ACL зоны (sync-агент стримит на сервер cloud-провайдера, копии у каждого устройства). Trade-off: теряется audit-trail config-изменений (но `app.jsonl` со счётчиками всё ещё пишется). UI-баннер при этом расширяется: «Audit-лог отключён из-за cloud-sync data_dir, переместите данные на локальный путь для включения». Документировано в [[ops/runbook]].
