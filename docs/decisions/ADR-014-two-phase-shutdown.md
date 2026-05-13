# ADR-014: Two-phase shutdown policy

**Context.** `supervisor.shutdown(timeout=10)` против HTTP timeout=30s + SMTP send=30s → каждая остановка фиксировала WARN с pending threads. Network timeouts арифметически больше supervisor-deadline.

**Decision.** Двухфазный shutdown:
- **Phase 1 (graceful, `grace_timeout=35s`)**: `stop_event.set()` + join каждого потока. 35с = `max(network_timeouts) + 5s` запас. Каждый `run_forever(stop_event)` проверяет event между итерациями/батчами/fetch'ами.
- **Phase 2 (forceful)**: при истечении grace — WARN с pending thread-stacks (через `faulthandler.dump_traceback`); `executor.shutdown(wait=False, cancel_futures=True)`; dangling threads помечены `daemon=True` при start (Python прибьёт при interpreter exit).
- **Network timeouts ≤ grace_timeout - 5s — обязательный инвариант**: HTTP `timeout=(10, 25)` (connect, read), SMTP connect=10s + send=20s + close=5s. Playwright nav=20s, action=10s.
- `conn_provider.close_all()` — ТОЛЬКО после phase 2 (иначе writers упадут с SQLITE_MISUSE).

**Consequences.** Shutdown без warn-флопа при гладком закрытии запросов. Цена: документированный инвариант на каждый network adapter, проверяется в config (lint/test). Расширяет ADR-005.

**Расширение R3-C3 (Playwright headed-login — pw_executor special-case).** `pw_executor` исключён из phase 1 supervisor.shutdown (Playwright sync API в C-extension не реагирует на `stop_event`; `cancel_futures=True` отменяет только pending). Добавлена **phase 1.5** между phase 1 и phase 2: `LoginService.cancel_active_job()` зовёт `LoginSession.cancel()`, который делает `browser.close()` извне worker-thread — активный `page.wait_for_url` развернётся с `TargetClosedError` за ~2-3 секунды, job завершится с `LoginOutcome(success=False, error="cancelled")`. После — `pw_executor.shutdown(wait=True)`. Дополнительно: `open_headed_login(deadline=300.0)` — hard timeout 5 минут (страховка от пользователя, закрывшего вкладку без логина). UI показывает «Закройте окно браузера для остановки» если headed-login активен при shutdown.

**Расширение R3-M3 (Known limitations).**
- **Windows shutdown машины**: `WaitToKillAppTimeout` по умолчанию 5с — phase 1 grace=35с не успевает; in-flight notifications/lots могут не записаться. Документировано в [[runbook]]: «при shutdown машины монитор не гарантирует доставку in-flight уведомлений». Будущее улучшение (не MVP): `SetConsoleCtrlHandler(CTRL_SHUTDOWN_EVENT)` fast-path с grace_timeout=4с.
- **systemd**: для корректного shutdown unit-файл должен иметь `TimeoutStopSec=45s` (grace 35с + phase 1.5 + 2 запас). Указано в installer-скрипте и runbook.
- **macOS (если когда-то)**: launchd по умолчанию даёт 5с — тот же класс проблемы что Windows.

Принимаем как known-limitation: machine-shutdown — не предмет гарантий MVP. Graceful app-shutdown (через UI / Ctrl+C) — гарантируется.

См. также: [[decisions-log]], [[architecture/04-composition-root]] §4.3.bis, [[ops/runbook]].
