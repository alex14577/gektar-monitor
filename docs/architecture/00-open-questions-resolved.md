# 0. Принятые решения по открытым вопросам (после ревью)

Семь открытых вопросов раздела «Открытые вопросы» закрыты после ревью Code Reviewer / Backend Architect / Security Engineer / Database Optimizer. После **второго раунда** ревью добавлены [[decisions/ADR-014-two-phase-shutdown|ADR-014]]..[[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]] и расширены [[architecture/03-protocols]] §3.1 (LotRepository contract), §3.3 (SmtpHostPolicy, with_retry forwarding), §3.5 (EventBus.publish), [[architecture/04-composition-root]] §4.3.bis (two-phase shutdown), [[architecture/07-concurrency]] §7.6 (PII в diff-логе).

| # | Вопрос | Решение |
|---|---|---|
| 1 | Discovery нотификаторов | **Explicit registry**. Nuitka-onefile несовместим с entry_points; supply-chain контроль. |
| 2 | Result-тип в HttpClient | **Нет**. Двухконтурно: `UpstreamError(category=...)` exception для HTTP/Upstream; `NotifyResult` Result-pattern — **только** для Notifier. |
| 3 | God-Container | **Раздроблен** на `Infra`, `Services` (опц. `Lifecycle`) — см. [[architecture/04-composition-root]] §4.1. |
| 4 | SQLite concurrency | `busy_timeout=5000` + батчинг + `cycle_in_progress` как **SOFT-YIELD** флаг (enrichment проверяет → sleep 50мс, **не mutex**). Между батчами full_scan — sleep 50мс. **Retry SQLITE_BUSY с jitter обязателен на всех writers**. Unified writer-queue **не делаем**. «Единая очередь» из decisions-log трактуется как SQLite writer-lock на уровне WAL — см. ADR. |
| 5 | import-linter в CI | **Да**. Контракты (R3-M4): `domain` ∉ {sqlite3, infra, services, web, composition, fastapi, requests}; `services` ∉ {infra, web, composition, fastapi, sqlite3, requests}; `infra` ∉ {web, composition}; `web` ∉ {composition}. `composition` (= `composition.py` + `app.py`) — может импортировать из `domain | services | infra | web`. Конкретный фрагмент `.importlinter` см. в [[decisions/ADR-006-import-linter-ci|ADR-006]]. |
| 6 | SSE persistence | **Не делаем в MVP**. БД — source of truth для `lot.new` (F5 восстановит). EventBus — двухконтурный: `normal` (drop OK, maxsize=100) и `critical` (`session.expired`, `cycle.error`, `smtp.failed`) — block-with-timeout 2с + force-unsubscribe slow consumer. Persistence в БД **нет**. |
| 7 | Тесты infra | **Гибрид**. `:memory:` — unit-тесты repo (CRUD/UPSERT/migrations/diff/idempotency). **tempfile WAL** (~5-10 тестов, `@pytest.mark.slow`) — concurrent writers, WAL-checkpoint, `VACUUM INTO`/backup, `wal_checkpoint(TRUNCATE)`. |

---

## 0.1 Изменения относительно `notifications.md`

`notifications.md` описывал `Notifier` как `ABC` с дефолтной реализацией `send_to_all` и retry-логикой. **Заменяем на `Protocol`** + retry-decorator. Причина: ABC с дефолтной реализацией = неявная зависимость наследника от мутаций базы, плохо комбинируется (наследование вместо композиции). Retry/logging — отдельная функция-декоратор `with_retry(notifier, attempts, backoff) -> Notifier` (структурно совместима через Protocol). См. [[architecture/03-protocols]] §3.3.

Что изменилось:
- `class Notifier(ABC)` → `class Notifier(Protocol)`, поля — `ClassVar`.
- `send_to_all` — **снято с интерфейса**, выполняется в `NotifierDispatcher` (он же знает про idempotency через `NotificationsRepository`).
- Retry — `with_retry(SmtpEmailNotifier(...), attempts=3, backoff=...)`.

Файл [[notifications]] приводится в соответствие — см. правки там же.
