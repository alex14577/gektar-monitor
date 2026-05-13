# ADR-006: import-linter в CI

**Context.** Слои domain/services/infra/web легко деградируют без автоматической проверки.

**Decision.** Закрепить через `import-linter` в CI. Контракты (R3-M4 — добавлен `composition` layer):
- `domain` ∉ {`sqlite3`, `infra`, `services`, `web`, `composition`, `fastapi`, `requests`}.
- `services` ∉ {`infra`, `web`, `composition`, `fastapi`, `sqlite3`, `requests`}.
- `infra` ∉ {`web`, `composition`}.
- `web` ∉ {`composition`}.
- `composition` (`composition.py`, `app.py`) — разрешён импорт из всех слоёв (это его задача — собирать граф).

Конкретный фрагмент `.importlinter`:
```ini
[importlinter]
root_package = fis_monitor

[importlinter:contract:layers]
name = Layered architecture
type = layers
layers =
    fis_monitor.composition | fis_monitor.app
    fis_monitor.web
    fis_monitor.services
    fis_monitor.infra
    fis_monitor.domain

[importlinter:contract:domain_purity]
name = Domain doesn't touch infrastructure libs
type = forbidden
source_modules = fis_monitor.domain
forbidden_modules = sqlite3, requests, fastapi, playwright, smtplib
```

**Consequences.** +1 dev-зависимость, +`.importlinter` в репо. Гарантия что архитектура не деградирует по мере роста. `composition.py` живёт «над» web (он импортирует роуты в `app.py`), но не наоборот.

> **Note (R5 review — DB)**: `compute_changes` в `domain/diff.py` импортируется из `infra/sqlite/lot_repo.py` — это легально по onion (infra→domain разрешён). Зафиксировать в `.importlinter` config: `layers` с `domain` строго ниже `infra`. CI-проверка через `lint-imports` обязательна.

См. также: [[decisions-log]], [[architecture/02-layers-dip]].
