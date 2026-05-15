# ADR-032 — Onboarding-driven Backfill Auto-trigger

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Backend Architect
**Tags**: backfill, onboarding, lifespan, trigger

Supersedes the «Auto-trigger heuristic» section of
[[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] only.
The rest of ADR-028 (PaginatedListFetcher design, single-flight, cancellation)
remains unchanged.

---

## Context

ADR-028 декларирует: «lifespan checks `lot_repo.count_active() == 0` after startup».
На практике это означает: backfill запускается в момент старта процесса, когда:

1. `settings.regions` ещё содержит дефолт `[1, 2]` (онбординг не пройден).
2. Playwright-сессия не залогинена (ЕСИА ещё не открывали).
3. Пользователь ещё не видел интерфейс.

Результат: `BackfillService._run` итерирует пустой (или дефолтный) список регионов,
делает HTTP-запрос без авторизованной сессии (или с ней, если она была), получает
403/пустой ответ, завершается no-op с `regions_total=N, lots_seen=0`. Повторного
триггера нет. Пользователь после онбординга не видит каталог.

ADR-028 декларирует семантику «first post-onboarding run», но lifespan ≠ post-onboarding.

---

## Decision

### Trigger placement: route handler `_handle_step4_next`

Убрать auto-backfill блок из `app.py:238-251` (lifespan).

Добавить trigger в `_handle_step4_next` в
`src/fis_monitor/web/routes/onboarding.py` — после успешного
`svc.advance(RECIPIENTS_SET, COMPLETED)`:

```python
# Pseudo-code для writer-агента:
if (
    _lot_repo is not None
    and _backfill is not None
    and _lot_repo.count_active() == 0
    and settings.regions  # не пустой список
    # login_session.is_logged_in() — опционально, не блокирует если True
):
    supervisor.start("backfill-auto", lambda stop: _backfill.start(stop))
    logger.info("onboarding: completed → auto-backfill scheduled")
```

`_lot_repo`, `_backfill`, `supervisor` инжектируются через `Depends()` аналогично
другим зависимостям в `settings.py` / `backfill.py`.

**Почему route handler, а не `OnboardingService.complete()`:**
- `OnboardingService` — доменный сервис, без зависимости от `BackfillService`.
  Внедрение BackfillService в OnboardingService нарушает low-coupling (домен
  зависел бы от infra-сервиса).
- Route handler — orchestration-слой между HTTP и сервисами. Именно здесь уместно
  скоординировать два сервиса после перехода FSM.
- Event/observer через ConfigSource subscribers — overkill для одного триггера
  в одной точке FSM.

**Guard conditions** (все должны быть true):
1. `lot_repo.count_active() == 0` — не дублировать если каталог уже есть.
2. `settings.regions != []` — нет смысла стартовать с пустым scope.
3. Backfill не запущен (`_backfill.is_running() == False`) — уже обеспечен
   single-flight внутри `BackfillService.start()`.

**Guard про login**: не проверять `login_session.is_logged_in()` как обязательный
guard. Сессия может быть уже активной (юзер залогинился до онбординга) или
не нужна (сайт доступен публично). BackfillService получит 403 и завершится с
предупреждением — это допустимый исход (юзер видит пустой каталог и может нажать
«Запустить backfill» вручную из UI).

---

## Alternatives Considered

| Option | Reason Rejected |
|---|---|
| Оставить в lifespan | Гонка: регионы не настроены, сессия не открыта при старте |
| `OnboardingService.complete()` внутри домена | Нарушает low-coupling: домен зависит от BackfillService |
| ConfigSource subscriber / EventBus | Overkill для одного триггера; усложняет трассировку |
| Отдельный `POST /backfill/auto-start` от UI | Требует JS-логики; дублирует guard conditions на клиенте |

---

## Consequences

- `app.py:238-251` — блок auto-backfill **удаляется**.
- `_handle_step4_next` получает дополнительные зависимости: `lot_repo`, `backfill`,
  `supervisor` через `Depends()`.
- Тест «lifespan не стартует backfill при empty regions» — надо написать /
  обновить (бывший тест был зелёным при lifespan-размещении).
- Тест «onboarding step4 next → backfill.start вызван один раз» — новый.
- Double-submit защита: `BackfillService.start()` single-flight уже обеспечивает
  идемпотентность; дублирующий вызов возвращает `False` без паники.

---

## References

- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — остальные аспекты backfill
- `src/fis_monitor/web/routes/onboarding.py` — `_handle_step4_next`
- `src/fis_monitor/app.py:238-251` — удаляемый блок
- `src/fis_monitor/services/backfill.py` — `BackfillService.start()` single-flight
