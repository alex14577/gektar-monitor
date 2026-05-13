# 2. Слои и направление зависимостей (DIP)

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

См. [[architecture/10-project-structure-diffs]] — что меняется относительно текущего `project-structure.md`.
