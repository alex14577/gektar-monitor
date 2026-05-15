# ADR-033 — Web-editable Monitor Schedule

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: settings, schedule, hot-reload, interval, web-ui

---

## Context

`settings.html.jinja:140-145` показывает расписание мониторинга как read-only
(`<span>` без формы). Изменить `interval_minutes`, `monitoring.full_scan_time`,
`monitoring.full_scan_l2_priority_days` можно только через ручное редактирование
`var/config.json` и перезапуск. Это расходится с остальным UX где `/settings/regions`
и `/settings/recipients` редактируются через веб.

`MonitorCycleService.run_forever` читает `config_source.current()` на **каждой
итерации** (подтверждено `monitor_cycle.py:230, 236`). `FullScanService.run_forever`
тоже читает `settings.monitoring.full_scan_time` на каждой итерации
(`full_scan.py:170`). Горячая перезагрузка при изменении через веб происходит
автоматически — ConfigSource обновляет свой кэш через watchdog на `config.json`,
сервисы читают `.current()` при следующем цикле без дополнительных subscribers.

---

## Decision

### Q4 — Endpoint: единый `POST /settings/schedule`

Один endpoint с тремя полями в одном payload, по аналогии с `POST /settings/regions`.

**Тело запроса** (`ScheduleBody: BaseModel`):
```python
class ScheduleBody(BaseModel):
    interval_minutes: Annotated[int, Field(ge=0, le=60)]
    full_scan_time: str          # формат "HH:MM", regex-валидация
    full_scan_l2_priority_days: Annotated[int, Field(ge=1, le=365)]
```

**Обоснование единого endpoint**:
- Три поля семантически связаны («расписание мониторинга»).
- Три отдельных endpoint'а привели бы к partial-update races: если пользователь
  меняет `interval_minutes` и `full_scan_time` одновременно через два POST'а,
  второй может затереть изменение первого (ConfigSource compute-and-replace).
  Один payload атомарен.
- Прецедент: `POST /settings/regions` — один endpoint для всего списка.

**Паттерн записи** (по аналогии с `post_regions`):
```python
current = config_source.current()
new_monitoring = current.monitoring.model_copy(update={
    "full_scan_time": body.full_scan_time,
    "full_scan_l2_priority_days": body.full_scan_l2_priority_days,
})
new_settings = current.model_copy(update={
    "interval_minutes": body.interval_minutes,
    "monitoring": new_monitoring,
})
config_source.save(new_settings)
```

Возвращает 204 + htmx-partial с обновлёнными значениями (аналог других settings-POST).

### Hot-reload механизм

Нет отдельных subscribers на изменение расписания. `MonitorCycleService.run_forever`
читает `config_source.current()` каждую итерацию (строки 230, 236 в monitor_cycle.py) —
новый `interval_minutes` применяется **с начала следующего idle-периода**.
`FullScanService` читает `settings.monitoring.full_scan_time` каждую итерацию —
новое время применяется в **следующем цикле sleep/check**.

Это означает: если пользователь меняет `interval_minutes` с 15 на 1, текущий
15-минутный sleep **не прерывается**. Допустимое поведение для MVP.

### Валидация `full_scan_time`

Regex `^([01]\d|2[0-3]):[0-5]\d$` на уровне Pydantic field_validator в `ScheduleBody`.
Тот же формат что `MonitoringConfig.full_scan_time` принимает в `full_scan.py:92`.

### UX / Template

В `settings.html.jinja` секция «Расписание мониторинга» заменяется на форму
с `hx-post="/settings/schedule"`, `hx-target="#schedule-section"`,
`hx-swap="outerHTML"`. Три поля: `<input type="number">` для interval и l2_days,
`<input type="text" pattern="...">` для full_scan_time. После 204 htmx обновляет
секцию.

---

## Alternatives Considered

| Option | Reason Rejected |
|---|---|
| Три отдельных endpoint'а | Partial-update race; не нужно для одной формы |
| Перезапуск сервисов при смене расписания | Overkill; текущая read-on-each-iteration достаточна |
| Interrupt текущего sleep при смене interval | Сложность (stop_event.wait vs time.sleep); не нужно для MVP |

---

## Consequences

- Добавить `ScheduleBody` в `settings.py`, новый handler `post_schedule`.
- `settings.html.jinja`: секция расписания становится htmx-формой.
- `MonitoringConfig` и `Settings.interval_minutes` в `domain/models.py` — без изменений.
- `FullScanService` и `MonitorCycleService` — без изменений (hot-reload работает).
- `var/config.json` остаётся SSOT; веб-форма только пишет в него через ConfigSource.
- Тест: validation regex `HH:MM`, ge/le bounds, persistence roundtrip через ConfigSource.

---

## References

- `src/fis_monitor/web/routes/settings.py` — паттерн `post_regions`
- `src/fis_monitor/services/monitor_cycle.py:230,236` — hot-reload confirmed
- `src/fis_monitor/services/full_scan.py:170` — hot-reload confirmed
- `src/fis_monitor/domain/models.py:438-475` — MonitoringConfig, Settings
- [[decisions/ADR-023-configsource-save-extension|ADR-023]] — ConfigSource.save() паттерн
