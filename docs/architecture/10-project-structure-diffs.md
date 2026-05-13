# 10. Расхождения с текущим `project-structure.md`

Текущий [[project-structure]] — **рабочая гипотеза**. Предлагаю следующие корректировки. Не реструктурирую ради реструктуризации; меняю только то, где видны SOLID-нарушения или нечёткие границы слоёв.

## 10.1 Выделить `domain/`

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

## 10.2 Выделить `services/` (application layer)

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

## 10.3 Перенести `monitor/parser_*.py` и `notifiers/email.py` в `infra/`

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

## 10.4 Композиция

**Сейчас:** Композиция предполагается в `app.py`. Это нормально для маленького проекта, но при 18 швах файл раздуется.

**Предлагаю:**
```
src/fis_monitor/
  container.py       # @dataclass Container (типы)
  composition.py     # build_container(settings, data_dir) → Container
  app.py             # FastAPI + lifespan, тонкий
```

## 10.5 Web

**Сейчас:** `web/routes/lots.py, settings.py, auth.py, notifications.py, diagnostics.py` — OK.

**Дополнительно:**
- `web/onboarding_gate.py` — middleware из [[decisions-log]] (redirect на `/onboarding?step=1`).
- `web/deps.py` — `Depends()`-фабрики над Container.
- `web/templates/` и `web/static/` — взять из `claude-design/`.

## 10.6 Итоговое дерево

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
