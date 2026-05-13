# 9. Тестовая стратегия по слоям

## Layer 1 — Domain (Pydantic-модели и Protocol-сигнатуры)

- **Unit:** валидация Pydantic — границы (interval_minutes 0..60), default'ы, frozen=True.
- **Fixtures:** примеры `Lot`, `Settings` в `tests/fixtures/dto/`.
- **Сеть/БД:** нет.

## Layer 2 — Application services (use cases)

- **Unit, чисто моки.** Каждый use case инжектируется фейковыми Protocol-реализациями:
  - `FakeClock` — сдвиг времени для теста «эскалация в 60 секунд».
  - `InMemoryLotRepository`, `InMemoryNotificationsRepository`.
  - `FakeHttpClient` — отдаёт HTML-фикстуры из `tests/fixtures/`.
  - `FakeNotifier` — пишет в список вместо отправки.
  - `FakeEventBus` — собирает published events.
- **Покрытие:** алгоритм early-exit, id_schema_anomaly, idempotency notifier, removal-detection logic, `compute_changes()` (diff-политика для всех типов полей включая `None`/datetime/JSON).
- **Сеть/БД:** нет, абсолютно.

**Инвариант R-tree consistency** (integration-тест в Layer 3): после любого write в `lots` с не-NULL `lat`/`lon` — `COUNT(*)` для (lot_id) в `lots_rtree` должен быть строго 1. Если `lat`/`lon` стали NULL — 0. Тест прогоняется на каждой `upsert`-операции в `SqliteLotRepository` (для обеспечения что `_sync_geo` действительно вызывается внутри tx). См. N-M3.

## Layer 3 — Infrastructure (адаптеры)

- **Integration:** реальная SQLite (`:memory:` или tempfile) + `SqliteLotRepository`, проверка SQL, индексов, миграций.
- **Parser:** `SelectolaxListParser` на датированных HTML-фикстурах (`tests/fixtures/cabinet-free-lot-2026-05-12.html`). Регрессия = точное совпадение полей.
- **HTTP:** `RequestsHttpClient` через `responses` / `requests-mock` — без реальной сети.
- **Notifier (Email):** **`aiosmtpd`** in-process SMTP — реальный send через `smtplib` на localhost.
- **Playwright:** не тестируется автоматически (headed-логин), только smoke-script `tools/smoke_login.py` для ручной проверки.

## Layer 4 — Web (FastAPI routes + SSE)

- **Integration:** `TestClient` + контейнер с **fake-infra**. CSRF, onboarding-gate, корректность Jinja-фрагментов для HTMX-роутов.
- **SSE:** `TestClient.stream()` + публикация в `FakeEventBus`, проверка что фрагмент HTML соответствует контракту из `claude-design/README.md`.

## Layer 5 — End-to-end (smoke)

- **Один тест:** lifespan up → подменить `HttpClient` на fixture-mode → выполнить 1 цикл → проверить что лот в БД, event в bus, нотификация в `notifications`.
- Запускается локально и в CI, **без сети и без Playwright**.

## Что НЕ мокируем

- SQLite в integration-тестах (in-memory достаточно быстра).
- Pydantic (это часть domain).
- selectolax (это часть парсера, не внешний шов — у нас есть конкретный контракт «парсить HTML»).

## Что **всегда** мокируем

- Сеть, время, файловую систему (через `Locker`, `ConfigSource`), Playwright, SMTP (через `aiosmtpd` или прямой мок Notifier'а).
