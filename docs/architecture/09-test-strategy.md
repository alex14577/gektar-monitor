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

## §Wiring tests belong to Layer 5 (smoke)

Тесты типа `test_build_container`, `test_composition_wires_all_dependencies` и любые тесты, инстанциирующие реальный DI-граф (Container, composition.py), **принадлежат Layer 5 (smoke)**, а не Layer 2.

**Почему wiring ≠ unit:**
- Цель wiring-теста — убедиться, что реальный граф зависимостей собирается без ошибок. Это по определению требует реальных зависимостей.
- Мокирование контейнера уничтожает проверяемую ценность: тест проверяет mock, а не граф.
- Layer 2 инвариант — «Сеть/БД: нет, абсолютно» — нарушается уже при инициализации SQLite-репозитория внутри Container, даже без запросов.

**Правило размещения:**
- `tests/unit/` — только тесты с fake-зависимостями (InMemory*, Fake*). Никаких реальных адаптеров.
- `tests/smoke/` — wiring-тесты и Layer 5 e2e. Допускают реальные адаптеры с in-memory/tempfile SQLite.

**Паттерн допустимого wiring-теста (Layer 5):**

```python
def test_build_container_succeeds(tmp_path):
    container = build_container(data_dir=tmp_path)
    assert container.lot_repository is not None
    # проверяем граф, не поведение
```

---

## §Logging tests — parametrize-collapse rule

Logging satellite-тесты (отдельные файлы `test_*_logging.py`) подчиняются следующим ограничениям:

1. **Размер файла ≤ 120 строк** (по `wc -l`). Превышение — обязательный рефактор перед merge.
2. **Shared fixtures mandatory** при дублировании между двумя и более лог-сателлитами. Общие фикстуры переносятся в `tests/conftest.py` или ближайший package-level conftest. «Дублирование» = тело фикстуры совпадает на ≥70%.
3. **Parametrize однотипные assert.** Набор тестов вида «assert message contains X», «assert message contains Y» (один и тот же logger, одно и то же событие, разные строки) — схлопываются в один параметризованный тест:

```python
@pytest.mark.parametrize("fragment", ["lot_id=42", "region=1", "status=active"])
def test_monitor_cycle_logs_contain(fragment, caplog):
    ...
    assert fragment in caplog.text
```

Исключение: если тест проверяет **разные события** или **разные уровни** — параметризация не обязательна.

---

## §Layer location rule — persistence engine visibility

`import sqlite3` (и любые прямые ссылки на SQLite API: `sqlite3.Connection`, `sqlite3.Row`, `sqlite3.connect`) **запрещены** в:

- `tests/unit/services/`
- `tests/unit/domain/`

Persistence-движок виден только в:

- `tests/integration/` — integration-тесты репозиториев и миграций
- `tests/unit/infra/` — unit-тесты инфра-адаптеров (парсеры, SQL-хелперы)

**Rationale:** domain и service-слои взаимодействуют с persistence исключительно через Protocol-интерфейсы (`LotRepository`, `NotificationsRepository` и т.д.). Тесты этих слоёв используют InMemory-fakes. Прямой `import sqlite3` в `tests/unit/services/` означает, что тест неявно проверяет конкретный адаптер — это нарушение low coupling и искажение назначения layer.

**CI enforcement:** добавить правило в `.importlinter`:

```ini
[importlinter:contract:no-sqlite-in-unit-services]
name = No sqlite3 in unit service tests
type = forbidden
source_modules =
    tests.unit.services
    tests.unit.domain
forbidden_modules =
    sqlite3
```

---

## §Pyramid baseline (non-binding)

**Статус: aspirational** — целевые доли не отражают текущее распределение (на 2026-05-17 фактически L2≈22%, L4≈22% при baseline L2 45%/L4 10%). Baseline фиксирует **направление эволюции тест-сьюта**, а не текущий инвариант. Reviewer ссылается на baseline как на ориентир при оценке нового файла: «новый L4-тест уводит ratio дальше от целевого 10% — это OK по non-binding критерию, но flag для следующей ретроспективы».

Ориентир по распределению тест-файлов между слоями. **Non-binding** = ориентир для code review, не gate в CI.

| Слой | Содержание | Целевая доля файлов |
|------|-----------|-------------------|
| L1 — Domain | Pydantic, Protocol-сигнатуры, diff | ~10% |
| L2 — Services | Use cases с fake-зависимостями | ~45% |
| L3 — Infra | SQLite, парсеры, HTTP-адаптеры | ~30% |
| L4 — Web | FastAPI routes, SSE, Jinja | ~10% |
| L5 — Smoke | Wiring, e2e-cycle | ~5% |

**Как считать:** по числу файлов `test_*.py`, не по LOC и не по числу test-функций.

**LOC ratio test:code ≤ 2:1** для всех слоёв, кроме:
- Парсеры (`tests/unit/infra/parsers/`) — допустим ratio >2:1 из-за объёма HTML-фикстур.
- Фикстуры (`tests/fixtures/`) — не учитываются в ratio.

Отклонение от pyramid (например, L2 < 30%) — флаг для ретроспективы, не автоматический fail.

---

## §Fake signature canon

Каждый Protocol из `src/fis_monitor/domain/` имеет ровно **один canonical fake**:

- **Расположение:** `tests/fakes/<protocol_name_snake_case>.py`
  - Пример: `LotRepository` → `tests/fakes/lot_repository.py`
- **Класс:** `Fake<ProtocolName>` — реализует все методы Protocol с точно совпадающими сигнатурами.
- **Проверка:** `mypy --strict tests/fakes/` должна проходить без ошибок. Несовпадение сигнатуры = ошибка типов.
- **Dev/CI step:** `uv run mypy --strict tests/fakes/` — запускать локально перед коммитом и в любом lint-job. Зависимость: `mypy>=1.13` в `[project.optional-dependencies] dev`. Пакет `fis_monitor` должен содержать `py.typed` (PEP 561) чтобы mypy не давал `import-untyped` при анализе fakes.
- **Единственность:** один canonical fake на Protocol. Callsite-специфичные вариации — через subclass или параметр конструктора (`raise_on_call: bool = False`), но не через отдельный файл.
- **Полнота вызовов:** в тест-сьюте должен существовать хотя бы один тест, вызывающий **все публичные методы** fake (не только `isinstance()`). Покрывает runtime-баги невалидного API.

**Пример структуры:**

```python
# tests/fakes/lot_repository.py
from fis_monitor.domain.protocols import LotRepository
from fis_monitor.domain.models import Lot, LotUpsertResult

class FakeLotRepository:
    def __init__(self) -> None:
        self._lots: dict[int, Lot] = {}

    def upsert(self, lot: Lot) -> LotUpsertResult:
        existed = lot.id in self._lots
        self._lots[lot.id] = lot
        return LotUpsertResult(created=not existed, updated=existed)  # adapt to actual model fields

    def get_by_id(self, lot_id: int) -> Lot | None:
        return self._lots.get(lot_id)

    def count_active(self, region_id: int | None = None) -> int:
        return sum(1 for lot in self._lots.values() if lot.status == "active")
```

Запрещено: `FakeLotRepositoryForEnrichmentTest`, `MockLotRepo` в `tests/unit/services/test_enrichment.py` inline.
