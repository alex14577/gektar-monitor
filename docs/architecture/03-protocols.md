# 3. Полный список Protocol-интерфейсов

Все швы — `typing.Protocol` с `@runtime_checkable` где нужно для тестов. **ABC не используем** ни для одного шва — `Notifier` тоже Protocol (см. [[architecture/00-open-questions-resolved]] §0.1 и [[decisions/ADR-001-notifier-protocol-not-abc|ADR-001]]). Общее поведение типа retry — отдельные функции-декораторы.

Все Protocol живут в `src/fis_monitor/domain/interfaces.py` (одно место — легко найти все швы системы).

> **Важно**: `ConnectionProvider` — **не** domain Protocol. Это infra-internal class (`ThreadLocalConnectionProvider` из `infra/sqlite/`), принимается репозиториями конкретным типом. `domain` не импортирует `sqlite3`. См. §3.5 и [[decisions/ADR-006-import-linter-ci|ADR-006]].

> **Сводка по числу швов**: ~15 Protocol'ов (было «18» в первом черновике). Исключены: `ConnectionProvider` (infra-internal), `NotifierRegistry` (composition-internal). `SettingsRepository`/`SmtpCredentialsRepository` остаются раздельными Protocol'ами ради type-safety, хотя внутри — тонкие обёртки над `state` key/value (тоже отдельный KV-репо). Цель — оси расширения, а не количество.

## 3.1 Репозитории (persistence)

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
    # вызывающей стороной (не отдаёт открытый cursor). См. [[architecture/07-concurrency]] WAL maintenance.
    #
    # ПУБЛИЧНОГО sync_geo НЕТ. R-tree синхронизируется ВНУТРИ upsert
    # (приватный _sync_geo). Если появится legitimate use case менять
    # координаты отдельно — добавить публичный update_geo(lot_id, lat, lon),
    # обёрнутый в BEGIN IMMEDIATE и зовущий _sync_geo. См. [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]] / N-M3.

class UserStateRepository(Protocol):
    def get(self, lot_id: int) -> LotUserState | None: ...
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
    State: pending → sent | permanent_fail. См. [[decisions/ADR-019-notification-state-machine|ADR-019]], [[notifications]].
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
    # changes()=0 → None. См. [[notifications]].
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

**Инварианты `SqliteLotRepository`** ([[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]]):
1. Все read-then-write операции (`upsert`, `mark_inactive`, `set_last_known_id`) открываются `BEGIN IMMEDIATE` — захват writer-lock до первого SELECT. Без этого — race window между SELECT old и UPDATE, и `SQLITE_BUSY` через busy_timeout в худшем случае.
2. `_sync_geo` — приватный метод, зовётся ТОЛЬКО из `upsert` в рамках той же tx. Из публичного Protocol удалён. **Поведение при изменениях lat/lon (R3-M8)**:
   - `was_new` И обе координаты не-NULL → `INSERT INTO lots_rtree`.
   - `was_new` И хотя бы одна NULL → no-op (R-tree не индексирует частичные координаты).
   - update, `(old.lat, old.lon) != (new.lat, new.lon)`:
     - обе новые не-NULL → `INSERT OR REPLACE INTO lots_rtree`.
     - хотя бы одна новая NULL → `DELETE FROM lots_rtree WHERE id = ?` (включая `value→NULL` и оба NULL).
   - update без изменения lat/lon → no-op.
   Integration-тест (см. [[architecture/09-test-strategy]]) покрывает все 5 переходов: `NULL→value`, `value→NULL`, `value→value'`, no-change, was_new с NULL и без.
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
См. R3-C2 в ревью / расширенный [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]].

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

> **Fixed in akv.2**: `init_db()` в `infra/sqlite/init_db.py` делает pre-flight check `PRAGMA user_version`. Алгоритм: fresh DB (user_version=0, нет таблиц) → `executescript(schema_sql)`; up-to-date → no-op; newer → `RuntimeError`; older → `migration_runner(conn, from, to)` или `raise MigrationRequired(from_version, to_version)`. `MigrationRequired` — `DomainError` без путей (PII-safe). Тип runner — `Callable[[sqlite3.Connection, int, int], None] | None`. Конкретный `MigrationRunner` — в akv.3.

**Точка расширения**: при переезде на хостинг (`MODE=server`) — `PostgresLotRepository` (новая реализация Protocol). Use case не меняется. ConnectionProvider в этом сценарии заменяется на pool — но это уже infra-деталь, в domain она не утекает.

## 3.2 HTTP и парсинг

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

## 3.3 Уведомления (плагины)

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

> **N-M4 — retry в Dispatcher**: для production-graph `with_retry` НЕ используется; retry-логика живёт в `NotifierDispatcher` (см. [[architecture/04-composition-root]] Layer 4 и [[notifications]]). `with_retry` остаётся как функциональная утилита для unit-тестов одиночного notifier-а. Причина: Dispatcher видит `NotificationsRepository` → может `reserve`/`mark_attempt`/`mark_sent` поверх рестартов (durable state machine, [[decisions/ADR-019-notification-state-machine|ADR-019]]); decorator работает только in-memory.
>
> **R3-M2 — stop_event-aware sleep**: retry-loop в Dispatcher между попытками делает `if self.stop_event.wait(delay): return` вместо `time.sleep(delay)` — иначе shutdown зависает на полном backoff (8+ секунд × attempts). При возврате status остаётся `pending` — recovery на след. старте через `list_pending_older_than`.

`NotifierRegistry` — **не Protocol**, а конкретный класс в composition root (`infra/notifiers/registry.py::ExplicitNotifierRegistry`). Внешним кодом он не подменяется; в тестах подменяются сами Notifier'ы. Из списка domain-Protocol'ов вынесен.

`NotifierDispatcher` (services) использует `NotificationsRepository` для idempotency и проходит по `registry.enabled()`. Логика «отправить всем получателям» живёт **только** там (вынесена из бывшего `Notifier.send_to_all`).

**SMTP-валидация — разделение domain vs infra** ([[decisions/ADR-015-smtp-host-validation|ADR-015]]):

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

`SmtpEmailNotifier._deliver()` определяет TLS-режим из порта (`port == 465` → implicit TLS, иначе STARTTLS) и делегирует в соответствующий приватный метод. Поле `use_starttls` в `SmtpCredentials` отсутствует — режим derive on-the-fly (см. [[decisions/ADR-021-manual-starttls-connect-by-ip|ADR-021]]). Оба пути используют connect-by-IP + правильный SNI через `ctx.wrap_socket(server_hostname=endpoint.original_host)`.

```python
# infra/smtp/email_notifier.py::SmtpEmailNotifier._deliver()
endpoint = self._host_policy.resolve_and_check(creds.smtp_host, creds.smtp_port)
# NB (R4-M2): resolve_and_check (включая socket.getaddrinfo, до 5с) — ВНЕ любой БД-tx.
# В SettingsService.set_smtp_credentials() и SmtpTestService.test_send() порядок:
#   1) Pydantic формат-валидация (мгновенно)
#   2) host_policy.resolve_and_check() (DNS, до 5с) — НЕ под tx
#   3) BEGIN IMMEDIATE; INSERT OR REPLACE smtp_credentials; COMMIT (короткая tx)
# Держать writer-lock пока DNS резолвится — недопустимо (блокирует cycle/enrichment).

implicit_tls = (endpoint.port == 465)

# --- STARTTLS path (port 587) ---
# smtp = smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=connect_timeout)
# smtp.ehlo(endpoint.original_host)
# code, _ = smtp.docmd("STARTTLS")
# if code != 220:
#     raise _StarttlsRefused(code)
# ctx = ssl.create_default_context()
# ctx.check_hostname = True
# smtp.sock = ctx.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)
# smtp.file = None   # invalidate cached file-wrapper (ADR-021)
# smtp.ehlo(endpoint.original_host)   # обязательный повторный EHLO после TLS

# --- Implicit TLS path (port 465, amendment 2026-05-16) ---
# ctx = ssl.create_default_context()
# ctx.check_hostname = True
# raw_sock = socket.create_connection((endpoint.ip, endpoint.port), timeout=connect_timeout)
# try:
#     tls_sock = ctx.wrap_socket(raw_sock, server_hostname=endpoint.original_host)
# except BaseException:
#     raw_sock.close()   # FD cleanup — raw_sock не owned TLS-обёрткой при исключении
#     raise
# smtp = smtplib.SMTP(timeout=connect_timeout)   # host='' → нет auto-connect
# smtp.sock = tls_sock
# smtp.getreply()   # читает 220-banner
# smtp.ehlo(endpoint.original_host)

# Единый error-mapping (один try-блок для обоих paths):
# ssl.SSLError precedes OSError (SSLError наследует OSError)
# smtplib.SMTPServerDisconnected precedes OSError (тоже наследует OSError)
smtp.login(creds.smtp_user, creds.smtp_password.get_secret_value())

# R4-C5: at-least-once. Детерминированный Message-ID — MTA дедупликация.
# Message-ID: <{lot_id}.{channel_id}.{sha256(recipient)[:16]}@fis-monitor.local>
# (RFC 5322 §3.6.4). recipient hashed против появления email в логах MTA.
smtp.send_message(msg)
```

DNS-rebinding закрыт. TLS-cert valid через SNI. MITM невозможен. At-least-once дубль (crash между «250 OK» и `mark_sent` COMMIT) — митигирован детерминированным Message-ID (см. [[notifications]] → «Семантика доставки» + [[decisions/ADR-019-notification-state-machine|ADR-019]] ext R4-C5).

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
- `password: pydantic.SecretStr` — обязательный инвариант. `__repr__`/`__str__` → `'***'`. Логи и diagnostic.zip не утекают ([[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]]).
- `recipients[*]`: RFC email + запрет `@localhost`, `@*.local`, IP-literal. `len(recipients) ≤ 10`.
- Connect timeout 10с, send timeout 20с (см. [[architecture/04-composition-root]] §4.3.bis — инвариант `network_timeouts ≤ grace_timeout - 5s`).

См. [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]].

**MVP**: `SmtpEmailNotifier`, `BrowserSseNotifier` (кладёт событие в EventBus, реальный push — браузерный JS через Notification API), опционально `HeartbeatNotifier` (по расписанию). Регистрация — **explicit registry** в composition root (см. [[architecture/06-notifier-registry]] — обоснование).

**Точка расширения**: `TelegramNotifier` (v2), `WebhookNotifier`, `NtfyNotifier` — добавляются как новый класс + одна строчка `registry.register(...)`.

## 3.4 Auth / login

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

> **Инвариант навигации `PlaywrightLoginSession`**: после `launch_persistent_context()` и до `wait_for_url(_LOGIN_SUCCESS_URL_GLOB)` реализация ОБЯЗАНА вызвать `page.goto(_LOGIN_START_URL, wait_until="domcontentloaded")` на target-URL гектара (`/cabinet/`). Без этого вкладка останется на `about:blank` и `wait_for_url` истечёт по `deadline`. Любая ошибка `goto()` маппится через `_map_exception` в `LoginOutcome.error` — failure-fast, без молчаливого таймаута. Стартовый URL = `https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/` (гектар, не ЕСИА напрямую): redirect-chain пройдёт через ЕСИА и вернётся, проставив **обе** группы cookies — гектар-side (для монитора) и ЕСИА-side (для сессии).

> **Инвариант host-whitelist `LoginSession`** (зафиксирован в docstring Protocol-а и в integration-тесте):
> реализация ОБЯЗАНА регистрировать `context.route()` с host-whitelist (target hosts + полный OAuth-chain Госуслуг) и блокировать все остальные запросы (`route.abort()`). Whitelist entries поддерживают два режима:
> - **exact-match**: `xn--80aaggvgieoeoa2bo7l.xn--p1ai`, `gosuslugi.ru` — точное совпадение hostname.
> - **suffix-match** (entry начинается с `.`): `.gosuslugi.ru` — матчит любой subdomain (`esia.`, `id.`, `lk.`, `pos.`, `static.` …).
> Bare apex `gosuslugi.ru` НЕ покрывает subdomains — это сделано намеренно, чтобы конфигурационная опечатка не расширяла политику.
> Атака с hostname `evil-gosuslugi.ru.attacker.com` блокируется (urlparse.hostname → полный хост, `endswith(".gosuslugi.ru")` → False).
> См. [[decisions-log]] → Security & operations.
> Тест: `tests/integration/test_login_host_whitelist.py` — открывает страницу
> с `<img src="https://evil.example/...">`, проверяет что запрос abort-нут.

## 3.5 Системные швы (для тестируемости)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...           # aware datetime в UTC
    def monotonic(self) -> float: ...

class Locker(Protocol):
    """Single-instance lock.
    Инвариант: реализация ОБЯЗАНА использовать OS-level lock
    (fcntl.flock на Linux, msvcrt.locking на Windows) с O_NOFOLLOW.
    O_EXCL намеренно НЕ используется — мешал бы re-acquire stale-lock после краша.
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
class SseCycleDone(BaseModel):
    # Terminal UI signal — published exactly once per `run_cycle` invocation
    # (happy path + every `_close_with_*` helper + session-expired branch).
    # Carries cycle_id, status (ok|error), lots_fetched, new_lots, duration_ms.
    # Consumed by `#cycle-done-listener` in base.html.jinja to clear the
    # "Идёт проверка" spinner injected by POST /cycle/run. Direct publish from
    # `MonitorCycleService` (same precedent as SseLotStatus; ADR-030 does not
    # apply — no recipient, no `notifications` row).
    priority: ClassVar[Literal["normal"]] = "normal"
```

> `EventSubscription` (события EventBus) и `ConfigSubscription` (callback на config-reload) — **разные имена**, чтобы не путать.

**Реализации MVP**: `SystemClock`, `FileLocker` (OS-level `fcntl.flock` / `msvcrt.locking`, PID info-only — ADR-013), `WatchdogConfigSource` (читает `config.json`, watchdog Observer триггерит reload), `WindowsAutostart` (Task Scheduler через `schtasks`), `LinuxAutostart` (XDG Autostart), `ThreadEventBus`.

**OnboardingService — отдельный документ.** State-machine, transitions, guards, контракт `OnboardingService.can_advance(from, to) -> bool` и middleware `onboarding_gate` — в [[onboarding]]. Здесь только указатель: server-side enforcement, middleware редиректит на **последний валидный step** (не на query-param). См. [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]].

**Зачем именно тут швы:**
- `Clock` — тесты «лот старше 10 минут» без `time.sleep`.
- `Locker` — тесты single-instance без файловой системы.
- `ConfigSource` — тесты hot-reload без watchdog Observer.
- `AutostartManager` — кросс-платформенный выбор без `if sys.platform`. macOS-реализация добавляется без изменения use case.
- `EventBus` — изоляция SSE от sync-логики; в тестах — in-memory bus.

## 3.6 Сводная таблица

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

### 3.6.1 Data-model: разделение DTO (forward-compat)

Канонические DTO (определены в [[data-model/lot]]):
- **`LotPublicDTO`** — лот без user-state. Поля: id/cadastral_no/area_sqm/.../is_active/freshness/tier/age_seconds. Безопасно публиковать через **EventBus** (никаких отметок текущего пользователя в multi-tab fan-out).
- **`LotUserDTO`** — `LotPublicDTO` + `LotUserState` (submitted/note). Запрашивается отдельным GET `/api/lots/{id}/user-state` либо включается в server-rendered HTML на главной странице (one-shot).
- **`LotUpsertResult`** — `was_new: bool`, `changes: list[FieldChange]`.
- **`FieldChange`** — `field: Literal[<allowed>], old_value: Any, new_value: Any` (см. [[data-model/lot]]). `old_value`/`new_value` сериализуются в БД через `json.dumps(..., ensure_ascii=False)` — N-M9.

Решение принято для forward-compat с multi-user v3 (хостинг): SSE-fan-out на сервере не должен знать про user, иначе одна вкладка увидит чужие note/submitted-state.

### 3.6.2 ParseError — разделение категорий

```python
class ParseBugError(DomainError): ...
    # Контракт сломан: парсер ожидал поле, селектор не нашёл. БАГ.
    # Поднимается в use case → cycle.error, exponential backoff.
class ParserVersionMismatch(DomainError): ...
    # Старая запись с parser_version=N, реальный парсер уже N+1.
    # НЕ ошибка цикла — триггер lazy reparse migration.
```

EnrichmentService при чтении `lot_html_archive` ловит `ParserVersionMismatch` → перепарсивает HTML текущим парсером → upsert лота. Cycle не падает.
