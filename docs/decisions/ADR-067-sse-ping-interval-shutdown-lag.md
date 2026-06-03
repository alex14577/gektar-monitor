# ADR-067 — SSE keepalive ping interval 15→2 сек: граница shutdown exit-lag

**Status**: Accepted
**Date**: 2026-06-03
**Deciders**: Backend, SRE, Architecture (brainstorm — general-purpose investigation + SRE review)
**Tags**: sse, concurrency, shutdown, keepalive, ping, thread-pool, lifespan
**Fixes**: task gektar-monitor-1iz (остаточный лаг до 15с при завершении процесса)
**Relates**: [[decisions/ADR-014-two-phase-shutdown|ADR-014]] (two-phase shutdown), [[decisions/ADR-066-sse-membership-filter|ADR-066]] (предикат не трогает `sse_executor` → не ухудшает этот лаг)

---

## Context

`SseStreamer._drain_one` блокируется в `subscription.wait_one(timeout=_DEFAULT_PING_INTERVAL)` →
`queue.get(timeout=...)`. `sse_executor` — `ThreadPoolExecutor` с **non-daemon** worker-тредами.
При lifespan-shutdown `sse_executor.shutdown(wait=False, cancel_futures=True)` **не прерывает**
уже запущенный `q.get` (стандартное поведение CPython: `cancel_futures` отменяет только
ещё не стартовавшие futures). Поэтому worker-тред живёт до истечения `q.get`-таймаута —
до **15 сек** при прежнем `_DEFAULT_PING_INTERVAL = 15.0` — задерживая physical exit
интерпретатора. Наблюдается как «процесс висит ~15с» при каждом рестарте сервиса
(systemd `ExecStop` / docker stop / SIGTERM).

В SSE-слое нет механизма прерывания drain (нет `stop_event`/sentinel). Предыдущий фикс
([[decisions/ADR-014-two-phase-shutdown|ADR-014]], `timeout_graceful_shutdown=5`) ограничил
ASGI-ожидание, но `sse_executor`-треды — не ASGI-соединения, лаг оставался.

Acceptance задачи: worker-треды завершаются за **≤2с** после shutdown.

---

## Decision

**Снизить дефолт `_DEFAULT_PING_INTERVAL` 15.0 → 2.0 сек** (`infra/sse/sse_stream.py`).

`_DEFAULT_PING_INTERVAL` получает **двойное назначение**: keepalive-каденс пинга **и**
верхняя граница shutdown exit-lag. Worker выходит из `q.get` в пределах `ping_interval`,
видит отменённую future / dead subscription и завершается через `finally → unsubscribe()`.
Реальный лаг ≤ 2.0с + ~10мс asyncio-overhead, что перекрывается `timeout_graceful_shutdown=5`.

Изменение — одна строка, без правок логики. Тесты, передающие `ping_interval` явно
(`0.05`/`0.1`/`1.0`), не затронуты; прод-композиция использует дефолт.

---

## Consequences

- Рестарт сервиса не висит до 15с — exit ≤2с (выполняет acceptance).
- **Побочный эффект:** keepalive-пинг теперь каждые 2с для каждого открытого SSE-соединения
  (было 15с) — рост ping-трафика/CPU-wakeups ~7×. Для target-деплоя (local-install на RPi,
  один пользователь, несколько вкладок) абсолютные значения ничтожны (десятки байт/с).
  Для reverse-proxy idle-timeout 2с — **надёжнее** держит соединение, чем 15с.
- Константа остаётся module-level, не вынесена в `Settings` → неоперируема без пересборки.
  Этот долг существовал и при 15.0; зафиксирован follow-up (см. ниже), не blocker.

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| **B: `stop_event` + sentinel late-bind в `SseStreamer`** | Корректнее по altitude (прерывание не зависит от ping-каденса), но +40–60 строк в 3 файлах; `SseStreamer` начинает хранить ref на активные подписки → **дублирование state** с `ThreadEventBus._subscribers` → нарушение low coupling, риск desync. Для local-install разница 0мс vs 2с exit-lag не наблюдаема. Не оправдывает сложность. |
| **Daemon-треды для `sse_executor`** | Мгновенный kill при exit (zero lag независимо от ping). Но: in-flight `yield` SSE-фрейма может быть прерван посередине (клиент получит частичный фрейм — пересинхронизируется при reconnect, коррупции данных нет, но поведение непредсказуемо); `finally → unsubscribe()` не выполнится → bus накопит мёртвые подписки (утечка при частых restart). Вариант A даёт предсказуемый bounded exit без side effects. |
| Оставить 15с (defer) | systemd default `TimeoutStopSec=90с` не убьёт процесс, SIGKILL не случится. Но «грязный» 15с-рестарт при каждом деплое — реальный observable дефект; пользователь выбрал фикс. |

---

## Follow-up

- **✅ Реализовано (gektar-monitor-5z8):** `ping_interval` оперируем через env-var
  **`FIS_MONITOR_SSE_PING_INTERVAL`** (НЕ user-facing `Settings` — это операционный/
  deployment-параметр той же семьи, что `FIS_MONITOR_HOST`/`FIS_MONITOR_PORT`).
  Читается **один раз** на boot в `build_container()` у точки сборки `SseStreamer`;
  отсутствует/пусто → дефолт `_DEFAULT_PING_INTERVAL` из `sse_stream.py` (единственный
  источник, не дублируется); невалидное значение → **fail-fast** `ValueError` на старте
  (как `int()` для `FIS_MONITOR_PORT`; live-reload для этого knob нет → тихий fallback
  не нужен). Вариант B (`stop_event`) в follow-up **не включался**.

---

## References

- `src/fis_monitor/infra/sse/sse_stream.py` — `_DEFAULT_PING_INTERVAL = 2.0`, `SseStreamer.__init__(ping_interval=...)`, `_drain_one`
- `src/fis_monitor/infra/sse/subscriptions.py` — `ThreadEventSubscription.wait_one` (`q.get(timeout=...)`)
- `src/fis_monitor/app.py` — `sse_executor.shutdown(wait=False, cancel_futures=True)`, `timeout_graceful_shutdown=5`
- [[architecture/07-concurrency]] §SSE-generator
- [[decisions/ADR-014-two-phase-shutdown|ADR-014]]
