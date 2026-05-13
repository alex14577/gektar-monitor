# Архитектура `fis-monitor`

> Документ зафиксирован Software Architect ДО написания кода. Источник правды по решениям — [[decisions-log]]. Этот документ — слои, швы, точки расширения и обоснование композиции, на которых будет строиться код в `src/fis_monitor/`.

Принципы — SOLID, DI через конструктор, Protocols для всех внешних швов, high cohesion / low coupling, composition over inheritance, расширение через регистрацию реализации интерфейса (не модификация).

---

## 0. Принятые решения по открытым вопросам (после ревью)

Семь открытых вопросов раздела «Открытые вопросы» закрыты после ревью Code Reviewer / Backend Architect / Security Engineer / Database Optimizer. После **второго раунда** ревью добавлены ADR-014..018 (см. §11) и расширены §3.1 (LotRepository contract), §3.3 (SmtpHostPolicy, with_retry forwarding), §3.5 (EventBus.publish), §4.3.bis (two-phase shutdown), §7.6 (PII в diff-логе).

| # | Вопрос | Решение |
|---|---|---|
| 1 | Discovery нотификаторов | **Explicit registry**. Nuitka-onefile несовместим с entry_points; supply-chain контроль. |
| 2 | Result-тип в HttpClient | **Нет**. Двухконтурно: `UpstreamError(category=...)` exception для HTTP/Upstream; `NotifyResult` Result-pattern — **только** для Notifier. |
| 3 | God-Container | **Раздроблен** на `Infra`, `Services` (опц. `Lifecycle`) — см. §4.1. |
| 4 | SQLite concurrency | `busy_timeout=5000` + батчинг + `cycle_in_progress` как **SOFT-YIELD** флаг (enrichment проверяет → sleep 50мс, **не mutex**). Между батчами full_scan — sleep 50мс. **Retry SQLITE_BUSY с jitter обязателен на всех writers**. Unified writer-queue **не делаем**. «Единая очередь» из decisions-log трактуется как SQLite writer-lock на уровне WAL — см. ADR. |
| 5 | import-linter в CI | **Да**. Контракты (R3-M4): `domain` ∉ {sqlite3, infra, services, web, composition, fastapi, requests}; `services` ∉ {infra, web, composition, fastapi, sqlite3, requests}; `infra` ∉ {web, composition}; `web` ∉ {composition}. `composition` (= `composition.py` + `app.py`) — может импортировать из `domain | services | infra | web`. Конкретный фрагмент `.importlinter` см. в §11/ADR-006. |
| 6 | SSE persistence | **Не делаем в MVP**. БД — source of truth для `lot.new` (F5 восстановит). EventBus — двухконтурный: `normal` (drop OK, maxsize=100) и `critical` (`session.expired`, `cycle.error`, `smtp.failed`) — block-with-timeout 2с + force-unsubscribe slow consumer. Persistence в БД **нет**. |
| 7 | Тесты infra | **Гибрид**. `:memory:` — unit-тесты repo (CRUD/UPSERT/migrations/diff/idempotency). **tempfile WAL** (~5-10 тестов, `@pytest.mark.slow`) — concurrent writers, WAL-checkpoint, `VACUUM INTO`/backup, `wal_checkpoint(TRUNCATE)`. |

---

## 0.1 Изменения относительно `notifications.md`

`notifications.md` описывал `Notifier` как `ABC` с дефолтной реализацией `send_to_all` и retry-логикой. **Заменяем на `Protocol`** + retry-decorator. Причина: ABC с дефолтной реализацией = неявная зависимость наследника от мутаций базы, плохо комбинируется (наследование вместо композиции). Retry/logging — отдельная функция-декоратор `with_retry(notifier, attempts, backoff) -> Notifier` (структурно совместима через Protocol). См. §3.3.

Что изменилось:
- `class Notifier(ABC)` → `class Notifier(Protocol)`, поля — `ClassVar`.
- `send_to_all` — **снято с интерфейса**, выполняется в `NotifierDispatcher` (он же знает про idempotency через `NotificationsRepository`).
- Retry — `with_retry(SmtpEmailNotifier(...), attempts=3, backoff=...)`.

Файл `notifications.md` приводится в соответствие — см. правки там же.

---

## 1. C4 Level 2 — Container diagram

Приложение — **один процесс** `fis-monitor` (Windows-бинарь у клиента, ELF на Linux dev/хостинге). Внутри живут несколько долгоиграющих компонентов, разделённых по ответственности и потокам исполнения.

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
   (один файл,               + Playwright             override клиента)
   per-thread conn)          (headed login)
```

**CSRF / DNS-rebinding middleware** (см. ADR в §11):
- **Strict Host allow-list**: `127.0.0.1:8080`, `localhost:8080`. Любой иной Host → **421 Misdirected Request** (защита от DNS-rebinding: вредоносный сайт резолвит `attacker.example` → `127.0.0.1` и шлёт POST; Host-проверка отсекает).
- **Origin/Referer whitelist** (не «непустой»): `http://127.0.0.1:8080`, `http://localhost:8080`. Любой иной — 403.
- **X-CSRF-Token** (secure-cookie + header) для ВСЕХ state-changing методов. Это включает (R4-M3) `POST /auth/cancel` (отмена активного headed-login) — раньше упускалось из обзора, теперь явно: под глобальным CSRF middleware, никаких исключений. Rate-limit 1 req/s через `Idempotency-Key` (защита от спама cancel-кликами).

**Ключевые свойства:**

- **Один процесс, много потоков.** Никакого subprocess для Playwright (см. decisions-log). Никакого multiprocessing.
- **SQLite** — sync, **один коннект на поток** (`sqlite3.connect` не thread-safe для шаринга). Параллелизм writers разруливается `PRAGMA busy_timeout=5000`, единый Python-lock НЕ нужен.
- **EventBus** — единственный мост между sync-миром (background-таски) и async-миром (SSE generators в FastAPI). Реализация — `queue.Queue` + `loop.run_in_executor(None, q.get)`.
- **Lifespan owns lifecycle.** Composition root собирается в `lifespan(app)`, там же стартуются и останавливаются все потоки. Никаких `threading.Thread(daemon=True)` где попало.

---

## 2. Слои и направление зависимостей (DIP)

Четыре слоя. Стрелки указывают, на что слой **может** ссылаться (внутрь, к центру). Нарушение стрелок — запрет.

```
        ┌─────────────────────────────────────────┐
        │  Web (FastAPI routes, Jinja templates) │  ← тонкий слой
        │  - routes/*.py                          │     адаптеров HTTP
        │  - sse.py (async generators)            │     над use cases
        │  - csrf.py, onboarding_gate.py          │
        └──────────────────┬──────────────────────┘
                           │ зовёт
                           ▼
        ┌─────────────────────────────────────────┐
        │  Application / Use cases                │  ← оркестрация,
        │  - MonitorCycleService                  │     зависит ТОЛЬКО
        │  - EnrichmentService                    │     от Protocol'ов
        │  - NotifierDispatcher                   │
        │  - OnboardingService, LoginService, ... │
        └──────────────────┬──────────────────────┘
                           │ зависит от Protocol'ов
                           ▼
        ┌─────────────────────────────────────────┐
        │  Domain                                  │  ← чистые Pydantic
        │  - Lot, LotDTO, CycleResult, ...        │     модели из
        │  - доменные исключения                  │     data-model.md
        │  - Protocol-интерфейсы швов             │  ← интерфейсы
        │    (LotRepository, HttpClient, ...)     │     живут здесь
        └─────────────────────────────────────────┘
                           ▲
                           │ реализует
        ┌──────────────────┴──────────────────────┐
        │  Infrastructure adapters                 │  ← конкретные
        │  - SqliteLotRepository                  │     реализации
        │  - RequestsHttpClient                   │     швов, наружу
        │  - SmtpEmailNotifier                    │
        │  - PlaywrightLoginSession, ...          │
        └─────────────────────────────────────────┘
```

**Запреты (нарушают DIP / coupling):**

- Domain **не импортирует** ничего из application/infrastructure/web. Только stdlib + pydantic.
- Application **не импортирует** ни `sqlite3`, ни `requests`, ни `playwright`, ни `smtplib`. Только Protocol'ы из domain.
- Application **не импортирует** `fastapi.*`. Use case не знает, что его вызывает HTTP.
- Web **не пишет SQL и не вызывает requests напрямую.** Только через use case.
- Infrastructure **не зависит от Web** (адаптеры — для use cases, не для роутов).

**Где живёт что (модули):**

| Слой | Папка | Что внутри |
|---|---|---|
| Domain | `src/fis_monitor/domain/` | `models.py` (Pydantic), `interfaces.py` (Protocols), `errors.py` |
| Application | `src/fis_monitor/services/` | По одному файлу на use case |
| Infrastructure | `src/fis_monitor/infra/` | `sqlite/`, `http/`, `playwright/`, `smtp/`, `sse/`, `autostart/`, `clock.py`, `lock.py`, `config_source.py` |
| Web | `src/fis_monitor/web/` | `routes/`, `sse.py`, `csrf.py`, `onboarding_gate.py`, `templates/`, `static/` |
| Composition | `src/fis_monitor/app.py` + `container.py` | Сборка графа |

См. раздел 10 — что меняется относительно текущего `project-structure.md`.

---

## 3. Полный список Protocol-интерфейсов

Все швы — `typing.Protocol` с `@runtime_checkable` где нужно для тестов. **ABC не используем** ни для одного шва — `Notifier` тоже Protocol (см. §0.1 и ADR в §11). Общее поведение типа retry — отдельные функции-декораторы.

Все Protocol живут в `src/fis_monitor/domain/interfaces.py` (одно место — легко найти все швы системы).

> **Важно**: `ConnectionProvider` — **не** domain Protocol. Это infra-internal class (`ThreadLocalConnectionProvider` из `infra/sqlite/`), принимается репозиториями конкретным типом. `domain` не импортирует `sqlite3`. См. §3.5 и import-linter в §11.

> **Сводка по числу швов**: ~15 Protocol'ов (было «18» в первом черновике). Исключены: `ConnectionProvider` (infra-internal), `NotifierRegistry` (composition-internal). `SettingsRepository`/`SmtpCredentialsRepository` остаются раздельными Protocol'ами ради type-safety, хотя внутри — тонкие обёртки над `state` key/value (тоже отдельный KV-репо). Цель — оси расширения, а не количество.

### 3.1 Репозитории (persistence)

```python
class LotRepository(Protocol):
    def upsert(self, lot: Lot, *,
               tracked: Sequence[TrackedField]) -> LotUpsertResult: ...
    # Контракт upsert (атомарный, одна tx, BEGIN IMMEDIATE):
    #   1) BEGIN IMMEDIATE (захватить writer-lock сразу, иначе race window
    #      между SELECT old и UPDATE даёт фантомный old в history);
    #   2) SELECT old row ВНУТРИ tx (для was_new + diff);
    #   3) changes = compute_changes(old, lot, tracked)  ← вызов domain-функции
    #      внутри tx. compute_changes — чистая функция в domain/diff.py;
    #      repo импортирует её (это НЕ нарушает DIP — domain ≺ infra; infra
    #      имеет право вызывать чистые domain-функции как библиотечные);
    #   4) INSERT OR UPDATE строки в lots;
    #   5) для каждого FieldChange — INSERT в lots_history (old_value/new_value
    #      JSON-кодированы — см. §3.6 и schema.sql);
    #   6) _sync_geo ТОЛЬКО внутри tx, если old.lat/lon != lot.lat/lon ИЛИ was_new;
    #   7) COMMIT; возврат LotUpsertResult(was_new: bool, changes: list[FieldChange]).
    # ВАЖНО — что НЕ делает upsert:
    #   - НЕ нормализует поля и НЕ описывает diff-политику — она в domain/diff.py.
    #   - НЕ позволяет caller-у считать diff заранее: caller передаёт ТОЛЬКО
    #     список tracked-полей. Это закрывает R3-C2 — TOCTOU между get() и upsert().
    # SRP: domain — «как вычислять diff»; infra — «выполнить diff внутри tx и
    #      записать историю атомарно». Caller (use case) — «дать новый лот».
    #
    # SQL-injection защита: TrackedField — Literal[...] (см. data-model),
    # допустимые имена полей ограничены на уровне типа. Дополнительный runtime
    # whitelist ALLOWED_TRACKED_FIELDS — defence-in-depth в реализации.

    def get(self, lot_id: int) -> Lot | None: ...
    def list_active(self, *, limit: int, offset: int) -> list[Lot]: ...
    def get_last_known_id(self, region: int) -> int | None: ...
    def set_last_known_id(self, region: int, value: int) -> None: ...      # BEGIN IMMEDIATE
    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None: ...
    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None: ...  # BEGIN IMMEDIATE
    def needing_enrichment(self, limit: int) -> list[int]: ...
    # ВАЖНО: реализация fetch-ит в список и закрывает курсор перед обработкой
    # вызывающей стороной (не отдаёт открытый cursor). См. §7 WAL maintenance.
    #
    # ПУБЛИЧНОГО sync_geo НЕТ. R-tree синхронизируется ВНУТРИ upsert
    # (приватный _sync_geo). Если появится legitimate use case менять
    # координаты отдельно — добавить публичный update_geo(lot_id, lat, lon),
    # обёрнутый в BEGIN IMMEDIATE и зовущий _sync_geo. См. ADR-016 / N-M3.

class UserStateRepository(Protocol):
    def get(self, lot_id: int) -> LotUserState | None: ...
    def set_starred(self, lot_id: int, value: bool) -> None: ...
    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None: ...
    def set_note(self, lot_id: int, note: str | None) -> None: ...
    def mark_visited(self, at: datetime) -> None: ...
    def last_visit(self) -> datetime | None: ...

class SettingsRepository(Protocol):
    """key/value `state` table — onboarding, last_visit, session_expired флаги."""
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def get_onboarding(self) -> OnboardingState: ...
    def set_onboarding(self, st: OnboardingState) -> None: ...

class NotificationsRepository(Protocol):
    """Idempotency + state-machine отправок. PK (lot_id, channel, recipient).
    State: pending → sent | permanent_fail. См. ADR-019, notifications.md.
    Все методы — внутри BEGIN IMMEDIATE."""
    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool: ...
    # True если slot создан; False если уже был (любого status).
    def status_of(self, lot_id: int, channel: str, recipient: str
                  ) -> Literal['pending', 'sent', 'permanent_fail'] | None: ...
    def mark_attempt(self, lot_id: int, channel: str, recipient: str,
                     at: datetime) -> int | None: ...
    # R4-C4: возвращает новый attempt_no при успехе, ИЛИ None если запись
    # уже в финальном статусе (sent | permanent_fail) — race с конкурентным
    # consumer / recovery / cap_reached. Caller (_send_one) обязан пропустить
    # отправку при None. SQL:
    #   UPDATE notifications SET attempt_no = attempt_no + 1, last_attempt_at = ?
    #    WHERE lot_id=? AND channel=? AND recipient=? AND status='pending'
    #   RETURNING attempt_no;
    # changes()=0 → None. См. notifications.md.
    def mark_sent(self, lot_id: int, channel: str, recipient: str,
                  at: datetime) -> None: ...
    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None: ...
    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]: ...
    def list_recent(self, limit: int) -> list[NotificationRecord]: ...

class CyclesRepository(Protocol):
    def open(self, region: int, at: datetime) -> int: ...    # → cycle_id
    def close(self, cycle_id: int, result: CycleResult) -> None: ...
    def list_recent(self, limit: int) -> list[CycleResult]: ...

class SmtpCredentialsRepository(Protocol):
    def load(self) -> SmtpCredentials | None: ...
    def save(self, creds: SmtpCredentials) -> None: ...
    # Upsert singleton (id=1 enforced CHECK-ом в схеме). Семантика (R3-M6):
    #   BEGIN IMMEDIATE;
    #   INSERT OR REPLACE INTO smtp_credentials
    #     (id, smtp_user, smtp_password, smtp_host, use_default, updated_at)
    #     VALUES (1, ?, ?, ?, ?, ?);
    #   COMMIT;
    # Идемпотентно: повторный save() с теми же значениями — no-op-в-сути
    # (REPLACE удалит старую строку и вставит новую с тем же id).
```

**Реализации в MVP**: `SqliteLotRepository`, `SqliteUserStateRepository`, ... — все в `infra/sqlite/`. Используют **конкретный** `ThreadLocalConnectionProvider` (infra-internal, не Protocol — domain не знает о sqlite3).

**Инварианты `SqliteLotRepository`** (ADR-016, см. §11):
1. Все read-then-write операции (`upsert`, `mark_inactive`, `set_last_known_id`) открываются `BEGIN IMMEDIATE` — захват writer-lock до первого SELECT. Без этого — race window между SELECT old и UPDATE, и `SQLITE_BUSY` через busy_timeout в худшем случае.
2. `_sync_geo` — приватный метод, зовётся ТОЛЬКО из `upsert` в рамках той же tx. Из публичного Protocol удалён. **Поведение при изменениях lat/lon (R3-M8)**:
   - `was_new` И обе координаты не-NULL → `INSERT INTO lots_rtree`.
   - `was_new` И хотя бы одна NULL → no-op (R-tree не индексирует частичные координаты).
   - update, `(old.lat, old.lon) != (new.lat, new.lon)`:
     - обе новые не-NULL → `INSERT OR REPLACE INTO lots_rtree`.
     - хотя бы одна новая NULL → `DELETE FROM lots_rtree WHERE id = ?` (включая `value→NULL` и оба NULL).
   - update без изменения lat/lon → no-op.
   Integration-тест (см. §9) покрывает все 5 переходов: `NULL→value`, `value→NULL`, `value→value'`, no-change, was_new с NULL и без.
3. `ALLOWED_TRACKED_FIELDS: frozenset[str] = {"status", "area_sqm", "date_update", "auction", "is_active", "list_presence"}` — runtime whitelist при INSERT в `lots_history` (defence-in-depth поверх Literal-типа `FieldChange.field`). Unknown поле → `ValueError`. Identifier-инъекции в имя поля невозможны.
4. `check_same_thread=False` явно в `_configure(conn)` — мы гарантируем сами «один коннект на поток» через `threading.local`, sqlite3 проверку отключаем (иначе невозможно `close_all()` из shutdown-потока).
5. **WeakSet snapshot перед close (R3-minor)** — `close_all()` НЕ итерируется по `WeakSet` напрямую (под `threading.Lock` — допустимо, но при close-callback'е sqlite3 структуры удаляются → WeakSet может мутироваться, RuntimeError). Pattern:
   ```python
   with self._lock:
       snapshot = list(self._connections)  # копия списка живых ссылок
   for conn in snapshot:
       try: conn.close()
       except sqlite3.Error: pass  # already-closed — OK
   ```
   Документировать в docstring `_configure`/`close_all`.

**Diff-политика** (`domain/diff.py`):
```python
def normalize_for_diff(lot: Lot) -> Lot: ...
    # Приводит status к canonical casing, datetime — к UTC секундной точности,
    # пустые строки → None. Чистая функция, без зависимостей.

def compute_changes(old: Lot | None, new: Lot,
                    tracked: Sequence[Literal[...]]) -> list[FieldChange]: ...
    # Возвращает FieldChange для каждого поля, где
    # normalize_for_diff(old).field != normalize_for_diff(new).field.
    # old=None → пустой список (новая запись, история не пишется).
```

`MonitorCycleService` / `EnrichmentService` — caller НЕ делает отдельный `get()`:
```python
new = parsed.to_lot()
result = lot_repo.upsert(new, tracked=DEFAULT_TRACKED_FIELDS)
# result.was_new — для решения «уведомлять или нет»
# result.changes — для SSE-фрагментов "lot.status"
```

**Двойной SELECT устранён.** TOCTOU между caller-stage `get(id)` и `upsert()` закрыт:
oldrow SELECT и compute_changes идут внутри той же BEGIN IMMEDIATE tx, что и UPDATE.
См. R3-C2 в ревью / расширенный ADR-016.

```python
# infra/sqlite/connection.py — НЕ в domain
class ThreadLocalConnectionProvider:
    """Per-thread sqlite3.Connection. threading.local + WeakSet[Connection]
    под threading.Lock для close_all(). Применяет per-connection PRAGMA
    (см. §3.5 и schema.sql)."""
    def get(self) -> sqlite3.Connection: ...
    def close_all(self) -> None: ...
    def _configure(self, conn: sqlite3.Connection) -> None: ...
```

PRAGMA-разделение:
- **Persistent** (`schema.sql`): `journal_mode=WAL`, `auto_vacuum=INCREMENTAL`, `user_version=N`.
- **Per-connection** (`_configure`): `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=OFF`, `temp_store=MEMORY`, `cache_size=-20000`, `mmap_size=268435456`.

> **FIXME (R5 review — DB)**: `init_db()` ДОЛЖЕН делать pre-flight check `PRAGMA user_version`. Если БД существует и `user_version < CURRENT_VERSION` — либо запустить MigrationRunner, либо `raise MigrationRequired` с инструкцией. Без этого первый dev с старой v1-БД словит cryptic `OperationalError` на отсутствующих колонках. В greenfield MVP runtime impact нулевой (v1-БД не существует), но фикс обязателен перед первым релизом v2→v3.

**Точка расширения**: при переезде на хостинг (`MODE=server`) — `PostgresLotRepository` (новая реализация Protocol). Use case не меняется. ConnectionProvider в этом сценарии заменяется на pool — но это уже infra-деталь, в domain она не утекает.

### 3.2 HTTP и парсинг

```python
class HttpClient(Protocol):
    def get(self, url: str, *, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None) -> HttpResponse: ...

@dataclass(frozen=True)
class HttpResponse:
    status: int
    text: str
    headers: Mapping[str, str]
    final_url: str   # для детекта 302 на /login

class ListParser(Protocol):
    def parse(self, html: str) -> list[ParsedListRow]: ...

class DetailParser(Protocol):
    def parse(self, html: str) -> ParsedDetail: ...
```

**MVP-реализации**: `RequestsHttpClient` (sync, persistent `requests.Session` per-thread, cookies из Playwright `profile/`), `SelectolaxListParser`, `SelectolaxDetailParser`. `parser_version` — атрибут класса парсера.

**Инвариант парсера (R3-minor)**: для отсутствующих/пустых полей парсер кладёт `None`, **не** пустую строку `''`. Это критично для FTS-триггера `WHEN old.cadastral_no IS NOT new.cadastral_no` (см. schema.sql) — `IS NOT` корректно отличает `NULL` от `''`, но если парсер кладёт `''` для отсутствия — FTS будет переиндексироваться при каждом upsert (баг производительности). Также `compute_changes` нормализует `''` → `None` (см. `domain/diff.py::normalize_for_diff`) — defence-in-depth.

**Точка расширения**: новый сайт-донор → новая пара `XxxListParser`/`XxxDetailParser`. Use case `MonitorCycleService` принимает их через DI, ничего не знает о selectolax.

### 3.3 Уведомления (плагины)

`Notifier` — **Protocol**, не ABC. Общее поведение (retry, logging) — отдельные функции-декораторы; композиция, не наследование.

```python
class Notifier(Protocol):
    channel_id: ClassVar[str]              # "email", "browser", "telegram"
    display_name: ClassVar[str]
    config_schema: ClassVar[type[NotifierConfig]]
    recipient_label: ClassVar[str]

    def send(self, lot: LotDTO, recipient: str) -> NotifyResult: ...
    def test(self, recipient: str) -> NotifyResult: ...

@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    detail: str            # human-readable причина успеха/неудачи
    retryable: bool        # стоит ли повторять (network/5xx) vs терминальный (auth)

# Retry — decorator-функция, не метод базового класса.
# Structurally compatible: возвращает объект, удовлетворяющий Protocol Notifier.
# ОБЯЗАТЕЛЬНО проксирует ClassVar'ы оборачиваемого класса — без этого
# registry/dispatcher не смогут различать каналы по channel_id.
def with_retry(n: Notifier, *, attempts: int, backoff: Sequence[float]) -> Notifier:
    cls = type(n)
    class _Retry:
        channel_id      = cls.channel_id        # type: ClassVar[str]
        display_name    = cls.display_name
        config_schema   = cls.config_schema
        recipient_label = cls.recipient_label
        def send(self, lot, recipient): ...     # retry-loop по NotifyResult.retryable
        def test(self, recipient): return n.test(recipient)  # без retry
    return _Retry()
```

> **N-M4 — retry в Dispatcher**: для production-graph `with_retry` НЕ используется; retry-логика живёт в `NotifierDispatcher` (см. §4.2 Layer 4 и notifications.md). `with_retry` остаётся как функциональная утилита для unit-тестов одиночного notifier-а. Причина: Dispatcher видит `NotificationsRepository` → может `reserve`/`mark_attempt`/`mark_sent` поверх рестартов (durable state machine, ADR-019); decorator работает только in-memory.
>
> **R3-M2 — stop_event-aware sleep**: retry-loop в Dispatcher между попытками делает `if self.stop_event.wait(delay): return` вместо `time.sleep(delay)` — иначе shutdown зависает на полном backoff (8+ секунд × attempts). При возврате status остаётся `pending` — recovery на след. старте через `list_pending_older_than`.

`NotifierRegistry` — **не Protocol**, а конкретный класс в composition root (`infra/notifiers/registry.py::ExplicitNotifierRegistry`). Внешним кодом он не подменяется; в тестах подменяются сами Notifier'ы. Из списка domain-Protocol'ов вынесен.

`NotifierDispatcher` (services) использует `NotificationsRepository` для idempotency и проходит по `registry.enabled()`. Логика «отправить всем получателям» живёт **только** там (вынесена из бывшего `Notifier.send_to_all`).

**SMTP-валидация — разделение domain vs infra** (ADR-015):

Pydantic-модель `SmtpCredentials` (domain) делает **только формат-валидацию** — синтаксически корректные IP/hostname, тип значений. **Policy-валидация** (что считать «безопасным host'ом для нашей среды») живёт в `infra/smtp/host_policy.py::SmtpHostPolicy` — она знает про DNS resolve и инфраструктурные ограничения. Domain не должна знать про cloud metadata или local DNS.

Точки применения policy:
1. **`SettingsService.set_smtp_credentials(creds)`** — на write при сохранении из UI (быстрый отказ; `resolve_and_check`-результат не сохраняется, только validation).
2. **`SmtpEmailNotifier.send()` ПЕРЕД connect** — `resolve_and_check()` возвращает `ResolvedSmtpEndpoint`, далее connect идёт **по IP** (а не по hostname), TLS-cert validation — по оригинальному hostname через SNI. Это закрывает TOCTOU (R3-C4): без pin'а на resolved IP `smtplib.SMTP(host).connect()` делает повторный `getaddrinfo()`, и атакующий с DNS-MITM мог бы вернуть RFC1918/loopback IP между двумя resolve-ами.

```python
@dataclass(frozen=True)
class ResolvedSmtpEndpoint:
    ip: str                  # resolved IPv4 или IPv6 literal
    family: socket.AddressFamily
    port: int
    original_host: str       # для SNI и TLS-cert validation

class SmtpHostPolicy(Protocol):
    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint: ...
    # Делает getaddrinfo ОДИН РАЗ, валидирует ВСЕ возвращённые адреса
    # (A/AAAA) против blocklist. Возвращает первый прошедший адрес.
    # Бросает SmtpHostPolicyError если хотя бы один адрес fail (fail-closed).
```

`SmtpEmailNotifier.send()` использует `endpoint.ip` для `smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=...)`, далее `smtp.ehlo(endpoint.original_host)`. STARTTLS делается **вручную** (см. ADR-021, R4-C2 — `smtplib.SMTP.starttls()` передаёт `self._host = endpoint.ip` как `server_hostname` → TLS cert verify валится против IP-литерала; вызов `context.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)` напрямую — корректное решение).

```python
# infra/smtp/email_notifier.py::SmtpEmailNotifier.send()
endpoint = self.host_policy.resolve_and_check(self.creds.smtp_host, self.creds.smtp_port)
# NB (R4-M2): resolve_and_check (включая socket.getaddrinfo, до 5с) — ВНЕ любой БД-tx.
# В SettingsService.set_smtp_credentials() и SmtpTestService.test_send() порядок:
#   1) Pydantic формат-валидация (мгновенно)
#   2) host_policy.resolve_and_check() (DNS, до 5с) — НЕ под tx
#   3) BEGIN IMMEDIATE; INSERT OR REPLACE smtp_credentials; COMMIT (короткая tx)
# Держать writer-lock пока DNS резолвится — недопустимо (блокирует cycle/enrichment).

smtp = smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=connect_timeout)
smtp.ehlo(endpoint.original_host)

if self.creds.use_starttls:
    # CRITICAL (R4-C2, ADR-021): smtplib.starttls() передаёт self._host (= IP) как
    # server_hostname → cert verify валится против IP-литерала.
    # Поэтому STARTTLS делаем вручную с правильным server_hostname.
    code, _ = smtp.docmd("STARTTLS")
    if code != 220:
        raise SmtpStarttlsError(code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    smtp.sock = ctx.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)
    smtp.file = None
    smtp.ehlo(endpoint.original_host)   # обязательный повторный EHLO после TLS
    # > **Note (R5 review — косметика)**: параметр `ehlo()` — это идентификация *клиента*
    # > (EHLO-name), не SNI сервера. Корректнее `smtp.ehlo(socket.getfqdn() or
    # > 'fis-monitor.local')`. Текущее `endpoint.original_host` — это имя сервера,
    # > MTA-валидация EHLO-name нестрогая, не критично. Реализовать при первом
    # > написании кода.

smtp.login(self.creds.smtp_user, self.creds.smtp_password.get_secret_value())

# R4-C5: at-least-once. Детерминированный Message-ID — MTA дедупликация.
message_bytes = self.build_message(lot, recipient)
# build_message() выставляет Message-ID:
#   <{lot_id}.{channel_id}.{sha256(recipient)[:16]}@fis-monitor.local>
# (RFC 5322 §3.6.4). recipient hashed против появления email в логах MTA.

smtp.sendmail(from_addr, [recipient], message_bytes)
smtp.quit()
```

DNS-rebinding закрыт. TLS-cert valid через SNI. MITM невозможен. At-least-once дубль (crash между «250 OK» и `mark_sent` COMMIT) — митигирован детерминированным Message-ID (см. notifications.md → «Семантика доставки» + ADR-019 ext R4-C5).

Что делает policy при resolve_and_check:
- Парсит host. Если IP literal — проверка ниже.
- Если hostname — `addrs = socket.getaddrinfo(host, port, family=AF_UNSPEC)`; для **каждого** A/AAAA результата применяет правила. Если **хоть один** адрес fail — fail (защита от DNS rebinding и multi-record).
- **IPv4-mapped IPv6** (`::ffff:a.b.c.d`) → `ipaddress.ip_address(h).ipv4_mapped` распаковать и проверять как IPv4.
- Универсальное правило через `ipaddress.ip_address(resolved)`:
  - `.is_private` (RFC1918 + RFC4193 `fc00::/7`)
  - `.is_loopback` (127/8 + ::1)
  - `.is_link_local` (169.254/16 + `fe80::/10`)
  - `.is_multicast` (224/4 + `ff00::/8`)
  - `.is_reserved`
  - `.is_unspecified` (`0.0.0.0`, `::`)
- **Отдельным правилом** (выше is_link_local): cloud-metadata `169.254.169.254` (AWS/Azure/GCP/Yandex.Cloud), `fd00:ec2::254` — fail с детализированным сообщением.
- **Broadcast** `255.255.255.255` → fail.
- **TLD-blocklist** для hostname (RFC 6761/2606 + local conventions): `*.lan`, `*.local`, `*.internal`, `*.corp`, `*.home`, `*.localdomain`, `*.test`, `*.example`, `*.invalid`, `*.localhost`.
- Edge cases: `"0"`, `"localhost"`, integer-IP — отвергаются ДО resolve.

Pydantic-модель в domain:
- `host: str` — формат-валидатор: непустой, не содержит CR/LF/space, длина ≤ 253, syntactically valid IP или RFC1035 hostname.
- `password: pydantic.SecretStr` — обязательный инвариант. `__repr__`/`__str__` → `'***'`. Логи и diagnostic.zip не утекают (ADR-017).
- `recipients[*]`: RFC email + запрет `@localhost`, `@*.local`, IP-literal. `len(recipients) ≤ 10`.
- Connect timeout 10с, send timeout 20с (см. §4.3.bis — инвариант `network_timeouts ≤ grace_timeout - 5s`).

Полный список ADR — §11 (ADR-015, ADR-017).

**MVP**: `SmtpEmailNotifier`, `BrowserSseNotifier` (кладёт событие в EventBus, реальный push — браузерный JS через Notification API), опционально `HeartbeatNotifier` (по расписанию). Регистрация — **explicit registry** в composition root (см. раздел 6 — обоснование).

**Точка расширения**: `TelegramNotifier` (v2), `WebhookNotifier`, `NtfyNotifier` — добавляются как новый класс + одна строчка `registry.register(...)`.

### 3.4 Auth / login

```python
class LoginSession(Protocol):
    """Headed-login через Playwright. Никаких других ответственностей."""
    def open_headed_login(self, *, deadline: float) -> LoginOutcome: ...
    # Блокирующий вызов: открывает окно, ждёт редирект на ФИС, закрывает.
    # deadline — wall-clock secs до hard-timeout (см. R3-C3). По истечении —
    # LoginOutcome(success=False, error="timeout"). Внутри page.wait_for_url
    # ставится timeout=deadline*1000.

    def cancel(self) -> None: ...
    # Thread-safe внешний останов. Вызывает browser.close() извне worker-thread:
    # активный page.wait_for_url развернётся с TargetClosedError, job завершится
    # с LoginOutcome(success=False, error="cancelled"). Безопасно звать когда
    # job неактивен (no-op). Используется LoginService.cancel_active_job()
    # из shutdown-hook и из UI «отменить login».

@dataclass(frozen=True)
class LoginOutcome:
    success: bool
    cookies_updated: bool
    error: str | None      # "timeout" | "cancelled" | "playwright:..." | None

class SessionProbe(Protocol):
    """Проверка валидности cookies без headed-окна."""
    def check(self) -> SessionStatus: ...

class SessionStatus(Enum):
    ACTIVE = "active"
    EXPIRING = "expiring"     # < 10 минут до истечения
    EXPIRED = "expired"
```

**MVP-реализации**: `PlaywrightLoginSession` (использует `sync_playwright()` в выделенном `ThreadPoolExecutor`, persistent context = `profile/`), `HttpSessionProbe` (HEAD на `/cabinet/profile`, ловит 302).

> **Инвариант `LoginSession`** (зафиксирован в docstring Protocol-а и в integration-тесте):
> реализация ОБЯЗАНА регистрировать `context.route()` с host-whitelist
> (`xn--80aaggvgieoeoa2bo7l.xn--p1ai`, `esia.gosuslugi.ru`) и блокировать все
> остальные запросы (`route.abort()`). См. [[decisions-log]] → Security & operations.
> Тест: `tests/integration/test_login_host_whitelist.py` — открывает страницу
> с `<img src="https://evil.example/...">`, проверяет что запрос abort-нут.

### 3.5 Системные швы (для тестируемости)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...           # aware datetime в UTC
    def monotonic(self) -> float: ...

class Locker(Protocol):
    """Single-instance lock.
    Инвариант: реализация ОБЯЗАНА использовать OS-level lock
    (fcntl.flock на Linux, msvcrt.locking на Windows) с O_NOFOLLOW|O_EXCL.
    PID в файле — только для info ('кто держит'), не для арбитража."""
    def acquire(self) -> LockHandle: ...     # raises AlreadyRunningError(pid=...)
    def release(self, handle: LockHandle) -> None: ...

class ConfigSource(Protocol):
    """Поток конфига с hot-reload."""
    def current(self) -> Settings: ...
    def subscribe(self, cb: Callable[[Settings], None]) -> ConfigSubscription: ...

class AutostartManager(Protocol):
    def is_enabled(self) -> bool: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...

class EventBus(Protocol):
    """sync→async мост для SSE. Один метод publish — приоритет на самом
    событии (OCP + SRP: добавление нового типа события не меняет EventBus)."""
    def publish(self, event: SseEvent) -> None: ...
    # Читает event.priority (ClassVar Literal["normal", "critical"]).
    # normal:   put_nowait, drop-from-tail при maxsize=100 на subscriber-queue.
    # critical: blocking put(timeout=2.0). При timeout — force-unsubscribe
    #           slow consumer + warn-лог + persist last-critical в `state`
    #           таблицу по ПЕР-TYPE ключу (R3-C5):
    #             - last_critical_event:session  (SseSessionExpired)
    #             - last_critical_event:cycle    (SseCycleError)
    #             - last_critical_event:smtp     (SseSmtpFailed)
    #           TTL 1 час каждый. Per-type slots — иначе быстрая пачка событий
    #           разных типов теряет предыдущие при single-slot.
    #
    #           Payload-whitelist (R3-C5): persist'ятся ТОЛЬКО поля из
    #           SsePayloadSchema.for_event(type) — БЕЗ stacktrace,
    #           exception_repr, recipient, smtp_response.
    #
    #           logger.warning при force-unsubscribe тоже редактируется
    #           через тот же whitelist (redactor pipeline).

    def subscribe(self) -> EventSubscription[SseEvent]: ...
    # context manager, per-subscriber queue.Queue, drop-from-tail.
    # Watchdog: если очередь >50 — маркер slow, после 3 публикаций со slow —
    # force-unsubscribe + close.
    # На новой подписке (включая reconnect после force-unsubscribe) bus
    # доливает ВСЕ pending per-type `last_critical_event:*` slots в TTL —
    # slow consumer после reconnect не пропустит ни одно критичное событие
    # любого типа.
```

Priority живёт на типе события (ClassVar):
```python
class SseSessionExpired(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
class SseCycleError(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
class SseSmtpFailed(BaseModel):
    priority: ClassVar[Literal["critical"]] = "critical"
class SseLotNew(BaseModel):
    priority: ClassVar[Literal["normal"]] = "normal"
class SseLotStatus(BaseModel):
    priority: ClassVar[Literal["normal"]] = "normal"
```

> `EventSubscription` (события EventBus) и `ConfigSubscription` (callback на config-reload) — **разные имена**, чтобы не путать.

**Реализации MVP**: `SystemClock`, `FileLocker` (PID + `psutil.pid_exists`), `WatchdogConfigSource` (читает `config.json`, watchdog Observer триггерит reload), `WindowsAutostart` (Task Scheduler через `schtasks`), `LinuxAutostart` (XDG Autostart), `ThreadEventBus`.

**OnboardingService — отдельный документ.** State-machine, transitions, guards, контракт `OnboardingService.can_advance(from, to) -> bool` и middleware `onboarding_gate` — в [[onboarding]]. Здесь только указатель: server-side enforcement, middleware редиректит на **последний валидный step** (не на query-param). См. ADR-018.

**Зачем именно тут швы:**
- `Clock` — тесты «лот старше 10 минут» без `time.sleep`.
- `Locker` — тесты single-instance без файловой системы.
- `ConfigSource` — тесты hot-reload без watchdog Observer.
- `AutostartManager` — кросс-платформенный выбор без `if sys.platform`. macOS-реализация добавляется без изменения use case.
- `EventBus` — изоляция SSE от sync-логики; в тестах — in-memory bus.

### 3.6 Сводная таблица

| Интерфейс | Где | MVP-реализация | Будущее расширение | Зачем шов |
|---|---|---|---|---|
| `LotRepository` | `domain/interfaces.py` | `SqliteLotRepository` | `PostgresLotRepository` (MODE=server) | swap БД, тесты с in-memory |
| `UserStateRepository` | // | `SqliteUserStateRepository` | // | // |
| `SettingsRepository` | // | `SqliteSettingsRepository` | // | // |
| `NotificationsRepository` | // | `SqliteNotificationsRepository` | // | // |
| `CyclesRepository` | // | `SqliteCyclesRepository` | // | // |
| `SmtpCredentialsRepository` | // | `SqliteSmtpCredentialsRepository` | keyring-реализация если поменяется threat model | // |
| ~~`ConnectionProvider`~~ | infra-internal | `ThreadLocalConnectionProvider` | — | НЕ Protocol; domain не знает о sqlite3 |
| `HttpClient` | // | `RequestsHttpClient` | `HttpxHttpClient` если нужен HTTP/2; mock для тестов | сетевой шов |
| `ListParser` | // | `SelectolaxListParser` | `SelectolaxV2ListParser` (parser_version=2) | смена сайта |
| `DetailParser` | // | `SelectolaxDetailParser` | // | // |
| `Notifier` | // | `SmtpEmailNotifier`, `BrowserSseNotifier`, `HeartbeatNotifier` | `TelegramNotifier`, `WebhookNotifier`, `NtfyNotifier` | плагины |
| ~~`NotifierRegistry`~~ | composition-internal | `ExplicitNotifierRegistry` | — | НЕ Protocol; в тестах подменяются Notifier'ы |
| `LoginSession` | // | `PlaywrightLoginSession` | fallback на ручной импорт cookies | swap логин-механики |
| `SessionProbe` | // | `HttpSessionProbe` | — | unit-тесты session-monitor |
| `Clock` | // | `SystemClock` | `FakeClock` в тестах | tests, time-skew |
| `Locker` | // | `FileLocker` | — | tests, swap impl |
| `ConfigSource` | // | `WatchdogConfigSource` | `EnvConfigSource` (MODE=server) | hot-reload |
| `AutostartManager` | // | `WindowsAutostart`, `LinuxAutostart` (stub) | `MacOsAutostart` (LaunchAgent) | кросс-платформа |
| `EventBus` | // | `ThreadEventBus` (queue.Queue) | `RedisEventBus` (MODE=server multi-worker) | sync→async мост |

Полный список — **~15 Protocol'ов** в domain (после удаления `ConnectionProvider` и `NotifierRegistry`). Это много, но каждый — отдельная ось расширения/тестирования. Свернуть в меньшее количество — потерять SRP.

#### 3.6.1 Data-model: разделение DTO (forward-compat)

Канонические DTO (определены в `data-model.md`):
- **`LotPublicDTO`** — лот без user-state. Поля: id/cadastral_no/area_sqm/.../is_active/freshness/tier/age_seconds. Безопасно публиковать через **EventBus** (никаких отметок текущего пользователя в multi-tab fan-out).
- **`LotUserDTO`** — `LotPublicDTO` + `LotUserState` (starred/submitted/note). Запрашивается отдельным GET `/api/lots/{id}/user-state` либо включается в server-rendered HTML на главной странице (one-shot).
- **`LotUpsertResult`** — `was_new: bool`, `changes: list[FieldChange]`.
- **`FieldChange`** — `field: Literal[<allowed>], old_value: Any, new_value: Any` (см. data-model.md). `old_value`/`new_value` сериализуются в БД через `json.dumps(..., ensure_ascii=False)` — N-M9.

Решение принято для forward-compat с multi-user v3 (хостинг): SSE-fan-out на сервере не должен знать про user, иначе одна вкладка увидит чужие starred/note.

#### 3.6.2 ParseError — разделение категорий

```python
class ParseBugError(DomainError): ...
    # Контракт сломан: парсер ожидал поле, селектор не нашёл. БАГ.
    # Поднимается в use case → cycle.error, exponential backoff.
class ParserVersionMismatch(DomainError): ...
    # Старая запись с parser_version=N, реальный парсер уже N+1.
    # НЕ ошибка цикла — триггер lazy reparse migration.
```

EnrichmentService при чтении `lot_html_archive` ловит `ParserVersionMismatch` → перепарсивает HTML текущим парсером → upsert лота. Cycle не падает.

---

## 4. Composition root

**Без сторонних DI-контейнеров.** `dependency-injector` слишком тяжёл для ~15 швов, `inject` использует декораторы (магия). Делаем явный Container class — ~150 строк, всё ручками, всё типизировано.

### 4.1 Структура (раздроблено, не God-Container)

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

### 4.2 Сборка — топологическая сортировка зависимостей

Граф зависимостей строится в порядке **топологической сортировки**. Нумерация Layer 0..4 ниже — текущий снимок для читателей, **не инвариант**. Цикл в зависимостях = ошибка, проверяется `import-linter` в CI (ADR-006).

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
    # рестартов (см. notifications.md).
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

### 4.3 Подключение к FastAPI

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

### 4.3.bis ThreadSupervisor + two-phase shutdown (ADR-014)

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
    #           #   после каждого fetched лота. См. §7.1.
```

**Network-timeouts ≤ grace_timeout - 5s — обязательный инвариант** (ADR-014).
Дефолты:
- `grace_timeout = 35s` (HTTP read + поправка 10s).
- HTTP `timeout=(10, 25)` — `(connect=10, read=25)`. `RequestsHttpClient` НЕ имеет неограниченных reads.
- SMTP: connect=10s + send=20s + close=5s = **≤30s суммарно**. Не делаем `SMTP.send_message()` без timeout — это бесконечный socket.recv в smtplib.
- Playwright: navigation_timeout=20s, action_timeout=10s.

Без этого инварианта `supervisor.shutdown(timeout=10)` гарантированно срывал бы в WARN при любом длинном запросе — баг second-round review.

**`cycle_in_progress` флаг** (N-M8, ADR-005 расширен):
- `threading.Event` (in-memory shared объект), внедрённый DI в `MonitorCycleService` (set/clear) и `EnrichmentService` (read + `sleep(50ms)`).
- НЕ персистится в БД. Потеря на рестарте — OK, это soft-yield координация, а не durable flag.
- Поле в `Infra`: `cycle_progress_signal: threading.Event`. `set()` в начале цикла, `clear()` в `finally` блока цикла.

### 4.4 Lifespan

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
        # Three-phase shutdown (ADR-014, R3-C3).
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

> **Cloud-sync detection** (`warn_if_in_cloud_sync`, ADR-010): большой `logger.warning` + баннер в UI если `data_dir.resolve()` попадает в один из паттернов. Используем `os.path.realpath()` (резолв symlinks/junction points) и расширенный список: `OneDrive*`, `Dropbox*`, `Yandex.Disk*`/`YandexDisk*`, `%USERPROFILE%\Documents`, `Google Drive` / `GoogleDrive*`, `iCloudDrive`, `rclone`, `pCloud`, `MEGA`, `Sync.com`, `Box`. SQLite-WAL и облачная синхронизация = коррапт.
>
> **R4-M7 — audit.jsonl fail-closed в cloud-sync.** Если `warn_if_in_cloud_sync` сматчил cloud-sync паттерн, `audit.jsonl` writer заменяется на **no-op** (никаких open()-w, никаких записей). Причина: `audit.jsonl` содержит полный PII config-diff (recipients, smtp.host) — попадание в облачную копию = утечка ВНЕ controlled-ACL зоны (sync-агент стримит на сервер cloud-провайдера, копии у каждого устройства). Trade-off: теряется audit-trail config-изменений (но `app.jsonl` со счётчиками всё ещё пишется). UI-баннер при этом расширяется: «Audit-лог отключён из-за cloud-sync data_dir, переместите данные на локальный путь для включения». Документировано в runbook.

---

## 5. Точки расширения (Open/Closed)

| Расширение | Что добавляешь | Где регистрируешь | Что НЕ трогаешь |
|---|---|---|---|
| Новый канал уведомлений (Telegram, ntfy, ...) | Класс реализует `Notifier` + `NotifierConfig` | `composition.py`: `registry.register(...)` | `NotifierDispatcher`, use cases, БД |
| Новый сайт-донор (другой регион, другая структура) | `XxxListParser`/`XxxDetailParser` + новый `MonitorCycleService` или параметризация существующего | `composition.py` или `multi-cycle service` | `LotRepository`, БД, web-слой |
| Новая платформа автозапуска (macOS) | `MacOsAutostart(AutostartManager)` | `build_autostart()` диспатч по `sys.platform` | use cases, контейнер |
| Новая стратегия сортировки/раннего выхода | `EarlyExitStrategy(Protocol)` — выделить из `MonitorCycleService` | `composition.py`: передать в `MonitorCycleService` | парсер, repo |
| Хостинг (PostgreSQL вместо SQLite) | `PostgresXxxRepository` для каждого repo | переключение в `build_container` по `MODE` | все use cases |
| Шифрование секретов (если threat model поменяется) | `EncryptedSmtpCredentialsRepository` (decorator над Sqlite-реализацией) | composition | все use cases, остальные репы |
| L2 verification стратегия | `RemovalVerifier(Protocol)`, реализации `ActiveVerifier`/`PassiveVerifier` | `composition.py` → `FullScanService` | repo, cycle |
| Каталог / поиск (v2) | новый use case `CatalogQueryService`, тащит из `LotRepository` + FTS | `web/routes/catalog.py` + `composition` | mirror-схема, monitor cycle |

**Ключевая идея OCP:** если для добавления фичи нужно править существующий use case или domain-модель — это знак, что нужен новый Protocol.

---

## 6. Plugin discovery для Notifiers — explicit registry

### Альтернативы

| Подход | Плюсы | Минусы |
|---|---|---|
| **Explicit registry** (composition root вызывает `registry.register(...)`) | прозрачно, типизировано, не зависит от файловой структуры, легко отключить канал в коде | при добавлении канала надо тронуть `composition.py` (1 строка) |
| **Entry points** (`pyproject.toml [project.entry-points]`) | плагины из сторонних пакетов | для onefile-бинаря (Nuitka) entry_points не работают штатно (или работают через костыли); магия |
| **Auto-discover по папке** (`pkgutil.iter_modules(notifiers_pkg)`) | «добавил файл — работает» | плохо контролируется порядок инициализации, скрытые зависимости, ломается в Nuitka |

### Решение

**Explicit registry** для MVP. Обоснование:

1. **Nuitka onefile** — entry_points и auto-discover требуют `__file__`-обхода, который в onefile неконсистентен. Explicit регистрация — гарантированно работает.
2. **Все плагины в MVP — наши**, не из внешних пакетов. «Плагин-маркетплейс» не нужен.
3. **Тестируемость** — в тестах подменяется `registry.register(FakeNotifier)`. Auto-discover требует моков на pkgutil.
4. **Однострочное добавление** — не overhead.

Если в v3+ появится сторонние плагины (например, клиент пишет свой webhook на Python) — переходим на entry_points с fallback на explicit. Сейчас — overengineering.

**Этот пункт — кандидат в ADR (см. раздел 11).**

---

## 7. Конкурентность и потокобезопасность

### 7.1 Кто с чем шарит память

| Ресурс | Кто пишет | Кто читает | Защита |
|---|---|---|---|
| `state.db` (SQLite) | monitor-cycle, enrichment, full-scan, web-handlers, notifier | те же | per-thread connection + `busy_timeout=5000` + **retry SQLITE_BUSY с jitter обязателен на всех writers** + **batch commit по 50 строк** в full_scan со `sleep(50ms)` между батчами |
| `Settings` (актуальный конфиг) | `WatchdogConfigSource.reload()` (1 поток) | все use cases | `Settings` — **immutable Pydantic BaseModel(frozen=True)**, swap по ссылке. **Обязательный паттерн**: `s = config_source.current()` ОДИН раз в начале workflow-шага, далее использовать локальную `s`. Reload применяется к следующему циклу. |
| `EventBus` subscribers | sync producers (cycle, notifier, session_monitor) | async SSE generators (FastAPI event loop) | `queue.Queue` thread-safe, fan-out — один Queue на подписчика. Normal: drop. Critical: block 2с + force-unsubscribe. |
| `last_known_id` cache | monitor-cycle | monitor-cycle | в БД, чтение перед каждым циклом |
| `session_expired` flag | session_monitor | monitor-cycle, enrichment, full_scan (проверяют на входе) | в БД (key `state.session_expired`), atomic |
| `cycle_in_progress` флаг | monitor-cycle (set/clear) | enrichment (проверяет) | **SOFT-YIELD** через `threading.Event` (in-memory, инжектирован DI как `Infra.cycle_progress_signal`). Enrichment видит `is_set()` → `sleep(50ms)`, **не mutex**. Потеря на рестарте OK (флаг — soft-yield, не для durability). НЕ персистится в БД. |
| Playwright `profile/` | login_session (на деманд) | RequestsHttpClient (читает cookies при следующем запросе) | mutex `profile_lock` + **single-flight** на headed-login: вторая попытка возвращает «уже идёт» |
| Notifier queue (in-memory) | monitor-cycle (продюсер) | NotifierDispatcher consumer | `queue.Queue`; persistence нотификаций — только в БД через `notifications` (idempotency) |

### 7.2 Приоритеты задач (общая очередь? нет)

Decisions-log говорит «приоритет: monitor > enrichment > full_scan». Это **не очередь** — это политика конкуренции за SQLite-writer-lock. **«Единая очередь» из decisions-log трактуется как SQLite writer-lock на уровне WAL** (см. ADR в §11). Никакого централизованного Python writer-thread не делаем.

Реализуется через раздельные потоки и комбинацию:

- **`busy_timeout=5000`** на каждом коннекте + **retry SQLITE_BUSY с jitter** (`time.sleep(random.uniform(0.01, 0.05) * (2**attempt))`, max 5 попыток) — обязателен на ВСЕХ writers (cycle, enrichment, full_scan, web-handlers, notifier).
- **`cycle_in_progress` SOFT-YIELD флаг**: enrichment проверяет перед каждой записью → если установлен, `sleep(50ms)` и повторно проверяет. Это **не mutex**, не блокирует, не вызывает priority inversion при сбое cycle.
- **Батчинг full_scan**: коммит по 50 строк + `sleep(50ms)` между батчами — отпускает write-lock.

**Альтернатива (единый writer-thread) — отвергнута**: всё упирается в один поток, добавляет сложности (queue, протокол результата), прибыли мало.

### 7.2.bis SQLite maintenance

- **WAL checkpoint**: раз в час maintenance-таска делает `PRAGMA wal_checkpoint(RESTART)`. **Не TRUNCATE** — RESTART успешно работает при наличии активных читателей (дочекпоинтит до позиции текущего reader-а и блокирует новых писателей пока reader не отпустит), а TRUNCATE при readers фактически no-op. На самотёк-checkpoint между maintenance-окнами полагаемся через `wal_autocheckpoint=1000` (PASSIVE).
- **Incremental vacuum**: раз в сутки `PRAGMA incremental_vacuum` (требует `auto_vacuum=INCREMENTAL` в schema, см. ADR-007). Без этого DB-файл растёт без переиспользования free-pages.
- **Курсоры**: все cursor'ы в repo — `with conn.execute(...) as cur` или явный `cur.close()`. Длинные итерации (`needing_enrichment(limit=N)`) — fetch в список, закрытие курсора перед обработкой.
- **`lots_history` retention**: 1 год. Индекс `idx_history_changed_at`.
- **`cycles` retention**: 90 дней.
- **`notifications` retention (R3-M7)**: `permanent_fail` старше 90 дней удаляются.
- **Chunked DELETE с sleep (R3-M7)** — ОБЯЗАТЕЛЬНЫЙ паттерн для всех maintenance-DELETE (history, cycles, notifications). Один большой `DELETE WHERE changed_at < ...` блокирует writer-lock на десятки секунд при росте таблицы; busy_timeout у конкурирующих writers исчерпывается. Pattern:
  ```python
  while True:
      with conn:  # auto-commit
          cur = conn.execute(
              "DELETE FROM lots_history WHERE rowid IN ("
              "  SELECT rowid FROM lots_history WHERE changed_at < ? LIMIT 1000)",
              (cutoff,),
          )
          if cur.rowcount == 0:
              break
      if stop_event.wait(0.1):
          return
  ```
  100ms sleep между чанками отпускает write-lock для cycle/enrichment/notifier. 1000 строк/chunk даёт ~50-100ms работы — компромисс между throughput и latency для конкурирующих writers.
- **`list_presence` в `lots_history`**: НЕ писать каждый цикл; писать только при `is_active 1→0` / `0→1` (после переоценки 2-х циклов отсутствия — см. removal-detection).
- **`lot_html_archive` retention** (R4-Minor): **в MVP не чистим**. Рост ~30 МБ/год (gzip HTML) при ~5к лотов в год приемлем для local-installation. Если когда-нибудь объём станет проблемой — добавим retention в maintenance (например, удалять архив для лотов где `inactive_since < now - 1y`).

### 7.3 SSE fan-out (sync → async, 1 → N)

```
[monitor-cycle thread]
  └─► event_bus.publish(SseLotNew(...))
            │
            ▼
   ┌──────────────────────────┐
   │ ThreadEventBus           │  держит set[Queue]
   │  - subscribers: list     │  под threading.Lock
   │  - publish():            │
   │     for q in subscribers:│
   │       q.put_nowait(evt)  │  ← non-blocking, drop при переполнении
   └─────┬────────┬───────────┘
         │        │       (по одной очереди на вкладку)
         ▼        ▼
   [SSE gen #1] [SSE gen #2]   ← async generators в FastAPI
        │            │
        │ await loop.run_in_executor(None, q.get, timeout=15)
        │ if timeout → yield ping
        │ else      → yield event
        ▼            ▼
     Tab #1       Tab #2
```

**Решения (priority на событии, см. §3.5):**
- **`event.priority == "normal"`** (`SseLotNew`, UI-нотификации): `put_nowait` на subscriber-queue с `maxsize=100`. При переполнении — **drop-from-tail** (старые UX-события можно потерять, БД source of truth).
- **`event.priority == "critical"`** (`SseSessionExpired`, `SseCycleError`, `SseSmtpFailed`): blocking `put(timeout=2.0)`. При timeout — force-unsubscribe slow consumer + `logger.warning` (через payload-redactor по `SsePayloadSchema`) + **persist last critical** в таблицу `state` по **per-type ключам** (R3-C5): `last_critical_event:session`, `last_critical_event:cycle`, `last_critical_event:smtp` — value = JSON только из whitelist-полей, TTL 1 час каждый. На reconnect новая подписка доливает ВСЕ pending слоты в TTL. Per-type slots: пачка из session.expired + cycle.error за окно TTL не теряет предыдущее событие (как при single-slot). Whitelist полей: см. `data-model.md::SsePayloadSchema`.
- **Watchdog на slow consumer**: если очередь подписчика > 50 — маркер `slow`. После 3 публикаций со slow — force-unsubscribe + `subscription.close()`.
- **Subscription** — context-manager: при дисконнекте удаляет свой Queue из набора.
- **SSE-generator** в роуте: `await asyncio.wait_for(loop.run_in_executor(sse_executor, q.get), timeout=15)` → keep-alive ping при timeout. В `finally` ГАРАНТИРОВАННО `subscription.unsubscribe()`.

**SSE security:**
- Принудительная проверка `Origin === http://127.0.0.1:8080` или `http://localhost:8080`; без Origin → 403. EventSource всегда same-origin Origin шлёт, поэтому это безопасно.
- Onboarding-gate middleware покрывает `/sse/*`.
- Никакого `Access-Control-Allow-Origin: *` нигде, ни в одном роуте.
- Integration-тест: `tests/integration/test_sse_security.py` — Origin: null → 403, Origin: evil.com → 403, без Origin → 403.

### 7.4 Immutable DTO

Все Pydantic-модели передаются между потоками — **`model_config = ConfigDict(frozen=True)`** на `Lot`, `LotDTO`, `Settings`, `CycleResult`, всех SSE-событиях. Никаких mutable shared structures.

### 7.5 Threading + Playwright (выделенные executor'ы)

| Executor | max_workers | Назначение | Почему отдельный |
|---|---|---|---|
| `pw-login` | 1 | Headed-login Playwright. Один долгоживущий `sync_playwright()` instance (cold-start ~1.5с, переиспользуется между попытками). | Не делится с anyio threadpool. Sync Playwright API не thread-safe. |
| `sse-wait` | 64 | `q.get()` в SSE-generator. | Не делится с FastAPI handler'ами — медленные подписчики не съедают handler-пул. |
| `enrich` | 10 | EnrichmentService параллельные fetch. | Изолированный bound с use case'ом. |
| FastAPI default | uvicorn-default | sync HTTP handlers (`def`). | Standard. |

**Single-flight + hard-deadline + cancel** на headed-login (ADR-014 ext, R3-C3):
- `LoginService` хранит `current_job: LoginJob | None` под `threading.Lock`. Если есть current — handler `/auth/login` возвращает существующий `job_id`. Иначе создаёт новый job и submits в `pw_executor`.
- **Hard deadline** — `open_headed_login(deadline=300.0)` (5 минут). По истечении worker возвращает `LoginOutcome(success=False, error="timeout")`. Без этого пользователь, закрывший вкладку без логина, оставляет навсегда висящий headed-Chromium → блок при shutdown.
- **`LoginService.cancel_active_job()` публичный**: thread-safe, идемпотентный. Вызывает `current_job.session.cancel()` (под `threading.Lock`) — `browser.close()` извне worker-thread развернёт `page.wait_for_url` с `TargetClosedError`. Доступен из:
  1. UI: HTMX-кнопка «Отменить вход» в модалке прогресса (после 30с появления).
  2. Shutdown phase 1.5 (см. §4.4 lifespan).

**Прогресс login** идёт через SSE: handler `POST /auth/login` возвращает `202 Accepted` + `{job_id, sse_url: "/sse/login/{job_id}"}`. События: `login.starting`, `login.window_open`, `login.completed{success: bool, error?: str}`.

### 7.6 Hot-reload config (WatchdogConfigSource)

- Подписка на **директорию** `data_dir`, фильтр по basename `config.json` (не на сам файл — atomic save через temp+rename даёт `Created/Moved`, а не `Modified`).
- Обрабатывать `created | modified | moved` одинаково.
- **Debounce 300мс**: коалесцируем серию событий (текстовые редакторы пишут пачкой).
- Pipeline: atomic read → Pydantic validate → **swap `_current` только при успехе**. Невалидное → `logger.warning` + старый `Settings` живёт, никакого frozen-app.
- **Diff-лог на INFO** (`app.jsonl`) — ТОЛЬКО счётчики и булы (no PII):
  - `recipients: count 2 → 3` (не сами адреса)
  - `regions changed: {1} → {1, 2}` (домен валидируется — это enum)
  - `smtp.host changed: true` (БЕЗ значения)
  - `interval_minutes: 15 → 5` (числовой scalar — это OK, не PII)
- **Полные значения config-diff** — отдельный append-only `audit.jsonl`, **физически исключённый** из `DiagnosticsService` (как `smtp_credentials`). Файл собирается ТОЛЬКО для on-disk audit, не для отправки клиенту. См. ADR-012, секция «audit.jsonl isolation».
- **ACL на `config.json`** (runbook/installer-чеклист): Windows — `Users: read, %USERNAME%: full`; Linux — `chmod 600`. Если кто-то с правами админа пишет в config — это вне нашей threat model.

---

## 8. Стратегия ошибок и Result-типы

### Двухконтурная модель

**Контур 1: внутренние ошибки (баги, инварианты) — exception.**
- `DomainError` базовый, подклассы: `SchemaAnomalyError`, `InvariantViolationError`.
- Ловятся в `MonitorCycleService.run_forever()` → пишутся в `cycles.error`, цикл переходит в exponential backoff.
- НЕ перехватываются в use case'ах поштучно — `except Exception: pass` запрещён.

**Контур 2: ожидаемые failure modes (network, SMTP, парсинг) — `Result`-тип.**
- Для `Notifier.send()` уже задано в `notifications.md`: `NotifyResult(ok, detail, retryable)`.
- Расширяем на `HttpClient`? **Нет.** `requests`-ошибки — exception (`requests.RequestException` → ловится в адаптере и поднимается как `UpstreamError` с категорией: `Network`, `Http4xx`, `Http5xx`, `RedirectToLogin`).
- Для парсера — разделение (§3.6.2): **`ParseBugError`** (контракт сломан — баг, поднимается в cycle.error) vs **`ParserVersionMismatch`** (lazy reparse, НЕ ошибка цикла). Universal `ParseError` deprecated в пользу двух подкатегорий.

**Почему так:**
- Result везде → код шумный, каждый use case разворачивает Result-цепочку. Python — не Rust, нет `?`-оператора.
- Exception везде → теряется явная семантика «это нормальный сценарий, retry в другом канале» vs «это баг, не игнорируй».
- Граница — нотификации: один канал упал → остальные идут. Тут Result даёт чёткую структуру для retry-логики (по `retryable` флагу) и записи в `notifications.detail`.

### Категории UpstreamError

```python
class UpstreamError(Exception):
    category: Literal["network", "http_5xx", "http_4xx", "redirect_login", "timeout"]
```

`MonitorCycleService` смотрит на `category`:
- `redirect_login` → поднять `session_expired`, no-op до релогина.
- `http_5xx`, `timeout`, `network` → exponential backoff, не пишем в `last_seen_at` при full_scan.
- `http_4xx` (кроме 401/403) → log, retry следующего цикла.

**Кандидат в ADR**: «Exception для внутренних, Result для нотификаторов» (раздел 11).

---

## 9. Тестовая стратегия по слоям

### Layer 1 — Domain (Pydantic-модели и Protocol-сигнатуры)

- **Unit:** валидация Pydantic — границы (interval_minutes 0..60), default'ы, frozen=True.
- **Fixtures:** примеры `Lot`, `Settings` в `tests/fixtures/dto/`.
- **Сеть/БД:** нет.

### Layer 2 — Application services (use cases)

- **Unit, чисто моки.** Каждый use case инжектируется фейковыми Protocol-реализациями:
  - `FakeClock` — сдвиг времени для теста «эскалация в 60 секунд».
  - `InMemoryLotRepository`, `InMemoryNotificationsRepository`.
  - `FakeHttpClient` — отдаёт HTML-фикстуры из `tests/fixtures/`.
  - `FakeNotifier` — пишет в список вместо отправки.
  - `FakeEventBus` — собирает published events.
- **Покрытие:** алгоритм early-exit, id_schema_anomaly, idempotency notifier, removal-detection logic, `compute_changes()` (diff-политика для всех типов полей включая `None`/datetime/JSON).
- **Сеть/БД:** нет, абсолютно.

**Инвариант R-tree consistency** (integration-тест в Layer 3): после любого write в `lots` с не-NULL `lat`/`lon` — `COUNT(*)` для (lot_id) в `lots_rtree` должен быть строго 1. Если `lat`/`lon` стали NULL — 0. Тест прогоняется на каждой `upsert`-операции в `SqliteLotRepository` (для обеспечения что `_sync_geo` действительно вызывается внутри tx). См. N-M3.

### Layer 3 — Infrastructure (адаптеры)

- **Integration:** реальная SQLite (`:memory:` или tempfile) + `SqliteLotRepository`, проверка SQL, индексов, миграций.
- **Parser:** `SelectolaxListParser` на датированных HTML-фикстурах (`tests/fixtures/cabinet-free-lot-2026-05-12.html`). Регрессия = точное совпадение полей.
- **HTTP:** `RequestsHttpClient` через `responses` / `requests-mock` — без реальной сети.
- **Notifier (Email):** **`aiosmtpd`** in-process SMTP — реальный send через `smtplib` на localhost.
- **Playwright:** не тестируется автоматически (headed-логин), только smoke-script `tools/smoke_login.py` для ручной проверки.

### Layer 4 — Web (FastAPI routes + SSE)

- **Integration:** `TestClient` + контейнер с **fake-infra**. CSRF, onboarding-gate, корректность Jinja-фрагментов для HTMX-роутов.
- **SSE:** `TestClient.stream()` + публикация в `FakeEventBus`, проверка что фрагмент HTML соответствует контракту из `claude-design/README.md`.

### Layer 5 — End-to-end (smoke)

- **Один тест:** lifespan up → подменить `HttpClient` на fixture-mode → выполнить 1 цикл → проверить что лот в БД, event в bus, нотификация в `notifications`.
- Запускается локально и в CI, **без сети и без Playwright**.

### Что НЕ мокируем

- SQLite в integration-тестах (in-memory достаточно быстра).
- Pydantic (это часть domain).
- selectolax (это часть парсера, не внешний шов — у нас есть конкретный контракт «парсить HTML»).

### Что **всегда** мокируем

- Сеть, время, файловую систему (через `Locker`, `ConfigSource`), Playwright, SMTP (через `aiosmtpd` или прямой мок Notifier'а).

---

## 10. Расхождения с текущим `project-structure.md`

Текущий `project-structure.md` — **рабочая гипотеза**. Предлагаю следующие корректировки. Не реструктурирую ради реструктуризации; меняю только то, где видны SOLID-нарушения или нечёткие границы слоёв.

### 10.1 Выделить `domain/`

**Сейчас:** `data_model.py` рядом с `app.py` — Pydantic-модели смешаны с точкой входа. Protocol'ов вообще нет — они в задаче этого ревью.

**Предлагаю:**
```
src/fis_monitor/
  domain/
    models.py         # все Pydantic из data-model.md
    interfaces.py     # все ~15 Protocol'ов
    errors.py         # DomainError, UpstreamError, ParseError
```

Domain — отдельный пакет, не зависит ни от чего, кроме stdlib+pydantic. Это критично для DIP.

### 10.2 Выделить `services/` (application layer)

**Сейчас:** `monitor/cycle.py`, `enrichment/worker.py`, `notifiers/...send()` — use cases размазаны по подсистемам.

**Предлагаю:**
```
src/fis_monitor/
  services/
    monitor_cycle.py       # MonitorCycleService
    enrichment.py          # EnrichmentService
    full_scan.py           # FullScanService
    notifier_dispatcher.py # NotifierDispatcher
    onboarding.py
    login.py
    session_monitor.py
    smtp_test.py
    lot_query.py           # read-model для UI
```

Один use case = один файл. Все принимают Protocol-зависимости в `__init__`.

### 10.3 Перенести `monitor/parser_*.py` и `notifiers/email.py` в `infra/`

**Сейчас:** парсер живёт в `monitor/`, нотификаторы в `notifiers/`. Это адаптеры (реализации Protocol'ов), их место в `infra/`.

**Предлагаю:**
```
src/fis_monitor/
  infra/
    sqlite/
      connection.py     # ThreadLocalConnectionProvider
      lot_repo.py
      user_state_repo.py
      ... остальные репы
      migrations/
      schema.sql
    http/
      requests_client.py
      session_probe.py
    parsing/
      list_parser.py    # SelectolaxListParser
      detail_parser.py
    playwright/
      login_session.py
    smtp/
      email_notifier.py
    sse/
      event_bus.py
      browser_notifier.py
    notifiers/
      heartbeat.py
      registry.py       # ExplicitNotifierRegistry
    autostart/
      __init__.py       # фабрика build_autostart()
      windows.py
      linux.py
    clock.py
    lock.py             # FileLocker
    config_source.py    # WatchdogConfigSource
```

### 10.4 Композиция

**Сейчас:** Композиция предполагается в `app.py`. Это нормально для маленького проекта, но при 18 швах файл раздуется.

**Предлагаю:**
```
src/fis_monitor/
  container.py       # @dataclass Container (типы)
  composition.py     # build_container(settings, data_dir) → Container
  app.py             # FastAPI + lifespan, тонкий
```

### 10.5 Web

**Сейчас:** `web/routes/lots.py, settings.py, auth.py, notifications.py, diagnostics.py` — OK.

**Дополнительно:**
- `web/onboarding_gate.py` — middleware из decisions-log (redirect на `/onboarding?step=1`).
- `web/deps.py` — `Depends()`-фабрики над Container.
- `web/templates/` и `web/static/` — взять из `claude-design/`.

### 10.6 Итоговое дерево

```
src/fis_monitor/
  __init__.py
  app.py                    # FastAPI + lifespan
  container.py              # @dataclass Container
  composition.py            # build_container()

  domain/
    models.py
    interfaces.py
    errors.py

  services/
    monitor_cycle.py
    enrichment.py
    full_scan.py
    notifier_dispatcher.py
    onboarding.py
    login.py
    session_monitor.py
    smtp_test.py
    lot_query.py

  infra/
    sqlite/{connection,lot_repo,...,migrations/,schema.sql}
    http/{requests_client,session_probe}
    parsing/{list_parser,detail_parser}
    playwright/login_session
    smtp/email_notifier
    sse/{event_bus,browser_notifier}
    notifiers/{heartbeat,registry}
    autostart/{__init__,windows,linux}
    clock.py
    lock.py
    config_source.py
    thread_supervisor.py
    paths.py                # platformdirs обёртка (зависит от platformdirs)

  web/
    deps.py
    csrf.py
    onboarding_gate.py
    sse.py
    routes/{lots,settings,auth,notifications,diagnostics,onboarding,cycle,filters,history}.py
    templates/...           # из claude-design/
    static/...              # из claude-design/

  utils/
    logging.py
    timezone.py

tests/
  fixtures/
  unit/                     # domain + services (fake protocols)
  integration/              # infra + web (in-memory sqlite, TestClient)
  smoke/                    # end-to-end один цикл
```

Изменение **не радикальное** — те же файлы, перетасованы по чётким слоям. `paths.py` живёт в `infra/paths.py` (единое место — зависит от `platformdirs`, внешней библиотеки).

---

## 10.7 Diagnostic.zip — explicit allow-list + redactor

`DiagnosticsService` (`services/diagnostics.py`) собирает диагностический архив. Threat model: пользователь шлёт zip разработчику для разбора инцидента — не должно протечь ничего секретного.

**Allow-list таблиц для экспорта** (всё остальное физически не открывается):
- `lots` — публичные данные.
- `cycles` — техническая телеметрия.
- `notifications(lot_id, channel, sent_at)` — **БЕЗ `recipient`** (это PII).
- `state` — фильтр: только ключи `monitor_paused`, `last_full_scan_at`, `onboarded`, `onboarding_step`; явно исключить любые ключи с подстроками `password|secret|token`.

**`smtp_credentials` физически не открывать** (даже для маскирования) — DB cursor вообще не касается этой таблицы.

**Redactor для логов** (на этапе сборки zip, не runtime):
- regex на: `Cookie:.*`, `Authorization:.*`, `?code=...`, `?state=...` (OAuth-параметры), СНИЛС (`\d{3}-\d{3}-\d{3} \d{2}`), паспорт (`\d{4} \d{6}`), ИНН (`\d{10,12}`), email-адреса в логах.
- Заменять на `<redacted:cookie>`, `<redacted:snils>` и т.д.

**MANIFEST.txt** в zip — список включённых файлов + версия app + commit-hash.

**Schema-snapshot fail-closed (R3-M5).** В коде `DiagnosticsService` живёт константа:
```python
DIAGNOSTIC_SCHEMA_V1 = {
    "lots": frozenset({"id", "cadastral_no", "area_sqm", "region", "municipality",
                       "land_category", "permitted_use", "ogv", "status",
                       "date_create", "date_update", "lat", "lon", "has_boundaries",
                       "parser_version", "first_seen", "last_seen",
                       "detail_fetched_at", "enrichment_status", "enrichment_retries",
                       "last_seen_at", "last_status", "last_status_at",
                       "is_active", "inactive_reason", "inactive_since",
                       "inactive_confirmed_at"}),
    "cycles": frozenset({"id", "region", "started_at", "finished_at", "status",
                         "lots_fetched", "new_lots", "error", "id_schema_check"}),
    "notifications": frozenset({"lot_id", "channel", "sent_at"}),
    # ВНИМАНИЕ: status/attempt_no/last_attempt_at — НЕ в whitelist (могут содержать
    # PII через side-channels). recipient — НЕ в whitelist (это PII по определению).
    "state": frozenset({"key", "value", "updated_at"}),  # фильтрация по ключам в коде
}
```
Перед сборкой zip Diagnostics сравнивает фактическую схему через `PRAGMA table_info(<table>)` со snapshot'ом. Если в реальной таблице **больше** колонок, чем в whitelist (например, после миграции добавили `last_login_ip`), bundle **НЕ собирается** — Diagnostics поднимает `SchemaDriftError("table=<...>, new=<set>, update DIAGNOSTIC_SCHEMA_V1")`. Fail-closed: лучше падающий diagnostic, чем тихая утечка новой колонки. При добавлении колонки разработчик ОБЯЗАН явно обновить DIAGNOSTIC_SCHEMA_V1, оценив PII-риски новой колонки.

**R4-M10 — generic UI message при SchemaDriftError.** Детали (имя таблицы, имя новой колонки) идут только в `app.jsonl`:
```python
try:
    self._validate_schema(DIAGNOSTIC_SCHEMA_V1)
except SchemaDriftError as e:
    logger.error("diagnostic.schema_drift", details=str(e))   # ПОЛНОЕ — в лог
    raise DiagnosticUnavailable(                              # ОБЩЕЕ — в UI
        "Diagnostic export disabled, contact support"
    )
```
Причина: имя колонки в UI может намекать на чувствительные поля (например `last_login_ip`, `payment_token`) — раскрывать через generic UI публично нежелательно. Разработчик получает диагноз через `app.jsonl` (это не PII — это техническая телеметрия).

**R4-Minor — CI-тест schema-snapshot.** Дополнительно к runtime fail (DiagnosticUnavailable) — `tests/integration/test_diagnostics_schema_no_drift.py`: pytest сверяет `DIAGNOSTIC_SCHEMA_V1` с реальной свежесозданной БД (после `schema.sql` + миграций). Drift → CI red. Это ловит drift на этапе review, а не у пользователя в проде.

---

## 10.8 Backup-стратегия — user-state only

После ревью DBA: **бэкапим только user-state**, не весь `state.db`. Mirror восстановим с сайта.

**`USER_STATE_TABLES`** — явный список:
- `lot_user_state` (starred / submitted / note)
- `notifications` (idempotency-журнал)
- `smtp_credentials` (логин/пароль)
- `state` (KV — onboarding, last_visit, dnd_until)

**Алгоритм** (`BackupService.backup_user_state(dst_path)`):
1. Открыть НОВУЮ пустую БД по `dst_path`.
2. Применить DDL только для `USER_STATE_TABLES`.
3. Из текущей `state.db` сделать `SELECT *` каждой таблицы и пагинировать через `cur.fetchmany(1000)` — для каждой пачки `executemany INSERT` в новую БД. Это держит память ограниченной даже при росте `notifications` за год.
4. Закрыть, atomic rename.

**Размер**: ~1 МБ. **Ротация**: 7 дней. **Имя**: `userstate-YYYY-MM-DD.sqlite` в `data_dir/backups/`.

**Mirror НЕ бэкапим** — `lots`, `lots_history`, `lot_html_archive`, `cycles`, FTS, R-tree. Они восстанавливаются полным переразбором (есть HTML-архив) либо новым прогоном.

> **Альтернатива** (рассмотренная и отвергнутая): `VACUUM INTO 'backup.db'` всего `state.db`. Проще, но (а) бэкап раздувается до десятков МБ через год; (б) при восстановлении из бэкапа на новой машине mirror «застывает» — лучше пусть новый клиент разберёт сайт заново.

---

## 10.9 HTTP-логи — fields-whitelist

`requests.jsonl` пишет ТОЛЬКО разрешённые поля:
- `method`, `url_path` (без query), `status`, `duration_ms`, `bytes`, `parser_version`.

**Никогда**: `Cookie`, `Authorization`, `Set-Cookie`, request/response body.

**Query**: пишется только для whitelist-путей (`/cabinet/free-lot` со списочной выборкой `?page=N` — но без OAuth-параметров). Для логин-роутов query замаскирована как `?<redacted>`.

UTC ISO-время через `Clock.now().isoformat()`. **Никаких `DEFAULT CURRENT_TIMESTAMP`** в SQL — это инвариант: время в БД пишет код через `Clock` (тестируемость).

---

## 11. ADR-темы для `decisions-log.md`

Кандидаты на запись отдельными ADR-блоками поверх существующих решений. Каждое — короткий context/decision/consequences.

1. **ADR: Composition root — самописный Container, разделённый на `Infra`/`Services`.** Без `dependency-injector`.
2. **ADR: Все швы — `typing.Protocol`. ABC не используем.** Включая `Notifier`. Retry/logging — функции-декораторы (композиция, не наследование). См. §0.1.
3. **ADR: Plugin discovery — explicit registry, не entry_points.** Nuitka-onefile несовместим с entry_points; supply-chain контроль.
4. **ADR: Error strategy — Exception (`UpstreamError(category)`, `DomainError`) для всего, `NotifyResult` Result-pattern — только для Notifier.** Двухконтурно.
5. **ADR: Concurrency model — раздельные потоки + per-thread SQLite-conn + retry SQLITE_BUSY + soft-yield `cycle_in_progress`.** «Единая очередь» из decisions-log = SQLite writer-lock на уровне WAL, не Python writer-thread.
6. **ADR: Domain/Services/Infra/Web layering — закреплён `import-linter` в CI.** Контракты: см. §0 пункт 5.
7. **ADR: Immutable Pydantic DTO между потоками (`frozen=True`).** + Settings-snapshot паттерн.
8. **ADR: Per-connection PRAGMA vs persistent.** persistent — в `schema.sql` (`journal_mode`, `auto_vacuum`, `user_version`); per-connection — в `_configure` (`busy_timeout`, `synchronous`, `temp_store`, `cache_size`, `mmap_size`).
9. **ADR: Notifier Protocol vs ABC.** Замена ABC на Protocol + `with_retry()` decorator. См. §0.1.
10. **ADR: EventBus двухконтурный — normal (drop OK) + critical (block-with-timeout + force-unsubscribe).** Без persistence в БД.
11. **ADR: Backup стратегия — user-state only (`USER_STATE_TABLES`).** Mirror не бэкапим.
12. **ADR: Data_dir location policy.** Cloud-sync (OneDrive/Dropbox/Yandex/`%USERPROFILE%\Documents`) → warning + UI-баннер. SQLite-WAL и облачная синхронизация = коррапт.
13. **ADR: DNS-rebinding защита — strict Host allow-list (421 Misdirected Request).** Origin/Referer whitelist, не «непустой».
14. **ADR: Diagnostic.zip — explicit table allow-list + log-redactor.** `smtp_credentials` физически не открывать.
15. **ADR: Locker через OS-lock (fcntl/msvcrt), PID — info-only.**
16. **ADR-014: Two-phase shutdown policy.** Phase 1 graceful (`stop_event` + join `grace_timeout=35s`), Phase 2 forceful (`cancel_futures=True`, dangling threads daemon). Network timeouts ≤ grace_timeout - 5s — обязательный инвариант. См. §4.3.bis.
17. **ADR-015: SMTP host validation — IP/DNS rules + resolve-recheck.** Разделение domain vs infra: Pydantic — формат-валидатор; `SmtpHostPolicy` (infra) — policy с `socket.getaddrinfo` recheck в `SmtpEmailNotifier.send()`. Закрывает TOCTOU. См. §3.3.
18. **ADR-016: Repository invariants — `BEGIN IMMEDIATE` + identifier whitelist + приватный `_sync_geo`.** Все read-then-write используют BEGIN IMMEDIATE; `ALLOWED_TRACKED_FIELDS` whitelist; `_sync_geo` приватен и зовётся только из `upsert`. См. §3.1.
19. **ADR-017: Secrets handling — `SecretStr` + crash-dump exclusion.** `SmtpCredentials.password: pydantic.SecretStr`. DiagnosticsService исключает `*.dmp`, `core.*`, `Werfault*`, `CrashDumps/`. См. §3.3.
20. **ADR-018: Onboarding FSM server-enforced.** State-machine с явными transitions и guards; middleware `onboarding_gate` редиректит на последний валидный step. См. `docs/onboarding.md`.
21. **ADR-019: Notification state machine (R3-C1).** Таблица `notifications` расширена `status` / `attempt_no` / `last_attempt_at` (`sent_at` стал nullable). PK `(lot_id, channel, recipient)`. Контракт `NotificationsRepository`: `reserve` → `mark_attempt` → `mark_sent | mark_permanent_fail`. Recovery `list_pending_older_than()` после рестарта. Каждый метод — отдельная короткая tx. Расширения R4: `mark_attempt -> int | None` (R4-C4 race), `list_pending_older_than` включает `last_attempt_at IS NULL` (R4-C3), at-least-once + Message-ID (R4-C5), hard-cap `MAX_TOTAL_ATTEMPTS=10` (R4-M6), migration v1→v2 (R4-M8). См. `notifications.md` + `db/schema.sql` + ADR-019 в decisions-log.
22. **ADR-020: SMTP host/port SSOT = state.db (R4-C1).** `smtp_host`/`smtp_port` хранятся в state.db::smtp_credentials, НЕ в config.json. Когезия со smtp_user/smtp_password (один атомарный апдейт = одна tx), защита от config-write-vector. Pydantic Settings больше не содержит SMTP-секретов. См. ADR-020 в decisions-log.
23. **ADR-021: Manual STARTTLS — обход smtplib server_hostname bug при connect-by-IP (R4-C2).** `smtplib.SMTP.starttls(context)` передаёт `self._host = endpoint.ip` как `server_hostname` → TLS cert verify валится против IP-литерала. Решение: вручную `ctx.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)` после `STARTTLS` через `smtp.docmd`. См. ADR-021 в decisions-log.

---

## Открытые вопросы

Все 7 пунктов закрыты (см. §0). Открытыми остаются исключительно прикладные вопросы из decisions-log.md (5 вопросов «живой проверки» для L2 verification) — это не архитектурные.

---

## См. также

- [[decisions-log]] — все зафиксированные решения (источник правды), ADR-001..018
- [[onboarding]] — server-side onboarding FSM (ADR-018)
- [[project-structure.md]] — текущая раскладка (этот документ предлагает уточнения)
- [[data-model.md]] — Pydantic-модели domain-слоя
- [[notifications]] — плагин-архитектура каналов
- [[monitoring-plan]] — поток данных, потоки исполнения
- [[db/schema|db/schema.sql]] — схема БД
- [[runbook]] — failure modes (учтены в дизайне)
