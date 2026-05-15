# ADR-032 — Backfill Auto-trigger: Login-success event (revised)

**Status**: Deprecated to secondary fallback — superseded as primary trigger by delta-based mechanism in ADR-028 §Generation 3 (Updated 2026-05-15)
**Date (original)**: 2026-05-15
**Date (revision)**: 2026-05-15 (f5u race-condition fix)
**Deciders**: Backend Architect
**Tags**: backfill, onboarding, login, trigger, race-condition

> **Note (Updated 2026-05-15)**: This ADR describes the `on_login_success` callback trigger, now
> **secondary fallback only**. It fires exclusively when `ParsedListPage.total_count is None` —
> i.e., the site did not return paginator markup and the delta-trigger (ADR-028 §Generation 3)
> cannot operate. When `total_count` is available, the delta-trigger in `MonitorCycleService` is
> the primary mechanism. `on_login_success` + `count_active() == 0` guard remains active code;
> it is not removed.

Supersedes the «Auto-trigger heuristic» section of
[[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] only.
The rest of ADR-028 (PaginatedListFetcher design, single-flight, cancellation)
remains unchanged.

---

## Context

### Original problem (lifespan trigger → ADR-028)

ADR-028 декларирует: «lifespan checks `lot_repo.count_active() == 0` after startup».
Это означало запуск backfill при старте процесса — до онбординга, до login, до
настройки регионов. Результат: 0 строк.

### First fix (onboarding-completion trigger, commit 5774095)

Перенесли trigger в `_handle_step4_next` (POST /onboarding/save?step=4).
ADR-032 (первоначальная версия) описывает это решение.

### Производственная гонка (f5u)

Прод-логи (`/tmp/fis/fis-monitor/var/logs/app.jsonl`) показали новую гонку:

```
08:46:07  onboarding: completed → auto-backfill scheduled
08:46:08  backfill region=1 → ParseBugError (cookies нет)
08:46:08  backfill region=2 → ParseBugError
08:46:08  backfill: finished, 0 rows
08:46:40  PlaywrightLoginSession: login succeeded  ← через 33s
```

Playwright headed-login (пользователь кликает в браузере) занимает 10-60 секунд.
Onboarding step4 → `svc.advance(COMPLETED)` → backfill trigger — всё ещё
**до** завершения login. Session cookies не установлены → `ParseBugError` на
каждый регион → 0 строк → `backfill: finished`.

Последствие: после первого `monitor_cycle` `count_active() > 0` → guard
блокирует все будущие auto-backfill навсегда. Исторические данные потеряны.

---

## Decision (revised)

### Trigger placement: `LoginService.on_login_success` callback

**Убрать** trigger из `_handle_step4_next` (onboarding route).

**Добавить** `on_login_success: Callable[[LoginOutcome], None] | None` в
`LoginService.__init__`. Callback вызывается в `_on_login_done` — только при
`start_login()` (headed login), НЕ при `start_refresh()` (silent cookie refresh).

В `composition.py` создаётся closure `_backfill_on_login_success`:

```python
def _backfill_on_login_success(_outcome: object) -> None:
    # Guards:
    if onboarding.current() != OnboardingState.COMPLETED:
        return           # онбординг не завершён
    if lot_repo.count_active() != 0:
        return           # каталог уже заполнен
    if not config_source.current().regions:
        return           # регионы не настроены
    sup = _supervisor_cell[0]
    if sup is None:
        return           # supervisor ещё не создан (тест без lifespan)
    sup.start("backfill-auto", lambda stop: backfill.start(stop))
```

**Почему callback на `LoginService`, а не route handler:**
- Route handler `_handle_step4_next` выполняется **до** завершения login.
  Единственная точка, где login success **известен** — `LoginService._on_done`,
  вызываемый через `Future.add_done_callback` на executor thread.
- `LoginService` не импортирует `BackfillService` — callback тип `Callable`,
  инжектируется из composition root. Coupling остаётся минимальным.
- Supervisor инжектируется через mutable cell (`_supervisor_cell: list[object]`)
  — supervisor создаётся в lifespan после `build_container()`, поэтому прямая
  ссылка в closure невозможна без циклической зависимости.

**Почему только `start_login()`, не `start_refresh()`:**
- Silent refresh (`start_refresh()`) не означает «первый вход пользователя» —
  он лишь обновляет существующие cookies. Семантика «пользователь залогинился»
  относится только к headed login.
- `_on_login_done` (новый метод) подключается только через `start_login()`;
  `start_refresh()` по-прежнему использует `_on_done` напрямую.

**Guard conditions** (все должны быть true, проверяются в callback):
1. `onboarding.current() == COMPLETED` — backfill для завершённого онбординга.
2. `lot_repo.count_active() == 0` — каталог пустой (первый запуск).
3. `config_source.current().regions != []` — регионы настроены.
4. `_supervisor_cell[0] is not None` — lifespan уже создал supervisor.
5. Single-flight: `BackfillService.start()` idempotent — повторный вызов returns `False`.

---

## Alternatives Considered

| Option | Reason Rejected |
|---|---|
| Оставить в lifespan | Гонка: регионы не настроены, сессия не открыта при старте |
| Route handler `_handle_step4_next` (ADR-032 v1) | Прод-гонка: login ещё не завершён при step4 |
| `OnboardingService.complete()` внутри домена | Low-coupling нарушен: домен зависит от BackfillService |
| EventBus `LoginSuccessEvent` | Overkill: дополнительный event-type, subscriber, persistence |
| Retry-loop в BackfillService на `ParseBugError` | Backfill не знает о login state; нарушает SRP; усложняет cancel |
| `POST /backfill/auto-start` от UI после polling login status | JS-логика, дублирует guards на клиенте |

---

## Consequences

- `LoginService.__init__` получает `on_login_success: Callable[[LoginOutcome], None] | None`.
- `LoginService._on_login_done` — новый private callback для headed login.
- `composition.py`: `backfill` создаётся **до** `login`; closure `_backfill_on_login_success`
  передаётся в `LoginService(on_login_success=...)`.
- `app.py` lifespan заполняет `_supervisor_cell[0] = supervisor` после
  создания `ThreadSupervisor` (single line, non-invasive).
- `_handle_step4_next` в `onboarding.py` — trigger убран. Параметры `lot_repo`
  и `backfill` остаются в сигнатуре (инжектируются FastAPI, не используются
  напрямую — можно убрать в follow-up рефакторинге).
- `_should_trigger_backfill()` в `onboarding.py` — сохранена как pure-function
  для изолированного тестирования guard-логики.

---

## References

- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — остальные аспекты backfill
- `src/fis_monitor/services/login.py` — `LoginService.on_login_success`, `_on_login_done`
- `src/fis_monitor/composition.py` — `_backfill_on_login_success` closure, `_supervisor_cell`
- `src/fis_monitor/app.py` — lifespan supervisor cell wiring
- `src/fis_monitor/web/routes/onboarding.py` — trigger removed from `_handle_step4_next`
- `tests/unit/services/test_login_backfill_trigger.py` — 5 unit tests for the trigger
