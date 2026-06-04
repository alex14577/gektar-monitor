---
name: ADR-056-licensing-hmac-stateless-offline
description: Система лицензирования на HMAC-SHA256 — stateless, offline, XOR-обфускация
type: decision
---

# ADR-056: Licensing — HMAC-SHA256, stateless, offline, XOR-обфускация секрета

## Context

`fis-monitor` требует базового механизма лицензирования для контроля распространения.
Требования заказчика:

- **Сложность ≤2/10** — никакой сложной инфраструктуры
- **Без внешних крипто-зависимостей** — только Python stdlib
- **Offline-first** — проверка без сети и без сервера
- **Без привязки к железу** — Machine ID, MAC-адрес — вне scope
- **Fail-closed UX** — stderr + exit(1), никакого интерактивного prompt

## Decision

**HMAC-SHA256 с симметричным секретом, stateless офлайн-верификация.**

Структура ключа: `v1.<base64url_payload>.<base64url_sig>`

Payload содержит: `v` (версия формата), `iat` (дата выпуска, anti-rollback floor), `exp` (дата истечения, опционально), `lic` (идентификатор получателя). Все даты — `YYYY-MM-DD`, сравнение только по дате.

**XOR-обфускация секрета** в `_secret._assemble_secret()`: секрет разбивается на две части `_P1` и `_P2`, хранящиеся как локальные переменные функции. Это блокирует `strings`-атаку на бинарь; дизассемблер всё равно сработает — tradeoff принят явно ([[licensing/secret-obfuscation|деталь]]).

**Абсолютная дата истечения** в payload (не relative duration) — верификатор не вычисляет ничего от «сейчас», кроме сравнения. `now` инжектируется как параметр — чистая функция, тестируемость без моков времени.

**`hmac.compare_digest`** обязателен — blocker при code review.

## Alternatives considered

### Ed25519 / асимметричная криптография

- Плюс: компрометация секрета верификатора не позволяет генерировать ключи
- Минус: `cryptography` или `PyNaCl` как внешняя зависимость; сложность сборки PyInstaller бандла; выходит за рамки 2/10
- Отвергнута

### KMS / онлайн-валидация подписи

- Плюс: возможность отзыва ключей; секрет никогда не попадает в бинарь
- Минус: требует сети; инфраструктура сервера; latency при каждом запуске
- Явно вынесена в [[licensing/out-of-scope|out of scope]]

### HMAC с секретом-строкой (без обфускации)

- Минус: секрет легко найти через `strings` на бинаре за секунды
- Отвергнута в пользу XOR-схемы

### Привязка к железу (Machine ID, MAC-адрес)

- Плюс: ключ работает только на конкретной машине
- Минус: проблема при переустановке ОС, смене оборудования; сложность UX
- Вынесена в out of scope

## Consequences

**Выигрываем:**
- 0 внешних зависимостей, только stdlib
- Stateless верификация за <1 мс при старте
- Генератор и верификатор используют единый источник секрета (`_secret.py`)
- `verify_license` — чистая функция; тесты без моков I/O

**Теряем:**
- Секрет в бинаре извлекаем при reverse-engineering → атакующий может генерировать произвольные ключи
- Нет отзыва ключей — скомпрометированный секрет требует пересборки бинаря
- Защита от тонкого отката часов (часы/минуты) невозможна без persistent state

Все потери — осознанные и принятые явно в рамках ограничения «сложность ≤2/10».

## Runtime expiry enforcement

Стартовая проверка (`main()` lines ~551-572) — defence-in-depth: синхронная, до запуска lifespan.

Для работающего приложения добавлен `LicenseExpirySupervisor` — фоновый поток, проверяющий лицензию раз в сутки в 00:01 UTC.

**Ключевые решения:**

- **UTC-расписание 00:01** — выбрано вместо «через 24 часа» чтобы поведение было предсказуемым независимо от времени старта. `next_check_at` — pure function, тестируемая без моков времени.
- **Re-read с диска на каждой проверке** — `LicenseKeyProvider.load_key()` вызывается при каждом тике. Осознанное решение: key rotation (замена `license.key` без перезапуска) работает автоматически.
- **Watchdog 45 s policy** — после вызова `request_shutdown()` взводится `threading.Timer(45.0, os._exit(1))`. Если graceful shutdown uvicorn не завершится за 45 с, watchdog принудительно завершает процесс. При успешном graceful shutdown lifespan отменяет таймер.
- **Fail-closed на crash** — необработанное исключение в цикле проверки логируется как `supervisor.crash` и немедленно вызывает shutdown (не игнорируется).
- **Идемпотентность `_handle_expiry`** — `threading.Event _expiry_handled` гарантирует ровно один вызов watchdog/SSE/shutdown даже при состоянии гонки.

**DI-Protocol сеамы** (в `domain/interfaces.py`): `SecretProvider`, `LicenseKeyProvider`, `ShutdownRequester`. Позволяют подменять зависимости в тестах без реального диска/uvicorn/времени.

## License key location

Canonical location: `<archive-root>/license.key` — рядом с `run.sh` / `run.bat`, **не** внутри `bin/`.

Both startup sites use a single frozen-aware `resolve_base_dir()` from `fis_monitor._license_loader`:

- **`app.py` (startup check)**: вычисляет `base_dir` через `resolve_base_dir(frozen, sys.executable, Path(__file__))`, затем передаёт в `load_license_key(base_dir)`.
- **`composition.py` `_LicenseKeyLoaderProvider` (runtime supervisor)**: получает `base_dir` через DI из `build_container(base_dir=...)`. `build_container` принимает `base_dir: Path | None = None`; если `None` — вычисляет через `resolve_base_dir` self-fallback. `app.py` передаёт явный `base_dir` чтобы оба сайта использовали одно и то же значение.

Таким образом `LicenseExpirySupervisor` (суточный re-read 00:01 UTC) читает ключ с того же пути, что и стартовая проверка — рассинхрон устранён.

## Links

- [[licensing/index|Licensing MOC]]
- [[licensing/architecture|Архитектура модулей]]
- [[licensing/crypto-hmac|HMAC-SHA256 детали]]
- [[licensing/secret-obfuscation|XOR-обфускация]]
- [[superpowers/specs/2026-05-28-licensing-system-design|Spec v1]]

---

## Amendment (2026-06-04): display-path лицензии в UI

Дата окончания, «осталось N дн.» и версия программы отображаются в сайдбаре
feed-страницы (bd gektar-monitor-49i/9t9):

- `_startup_verify_license` (app.py) выполняет verify_license **один раз** в
  lifespan после `app.state.container` и кладёт результат в
  `app.state.license_result`. Fail-open для отображения: любое исключение →
  `LicenseResult(INVALID)` (enforcement fail-fast остаётся в `main()` ДО
  старта ASGI). Роуты читают через `get_license_result` (web/deps.py).
- Семантика дней — включительная: `days_left = max(0, exp − today)`;
  в последний день действия показывается «осталось 0 дн.».
- Состояния: VALID → дата+дни (muted); EXPIRED → «Лицензия истекла · дата»
  (var(--err), декоративно — shutdown делает supervisor); INVALID/нет ключа →
  блок скрыт. Версия — из `importlib.metadata` (fallback `"dev"`).
