# Onboarding — server-side FSM

> Спецификация state-machine онбординга. Ссылается [[architecture]] §3.5 (контракт `OnboardingService`), [[decisions-log]] ADR-018. Замена «query-param редирект» на «server-enforced последний валидный step».

## Зачем отдельный документ

В первой версии middleware `onboarding_gate` редиректил на `?step=N` из query-param. Пользователь мог `GET /?step=4` и пропустить настройку SMTP. Это **дыра в guard-rails**, а не косметика — лот «по умолчанию» уйдёт через дефолтный бот-ящик без явного согласия клиента.

Решение — server-side FSM. UI всегда показывает то, что разрешает сервер.

## States

```
not_started → regions_set → smtp_configured → recipients_set → completed
```

| State | UI экран | Что должно быть true для входа |
|---|---|---|
| `not_started` | step 1 (область наблюдения) | (initial) |
| `regions_set` | step 2 (SMTP) | `len(settings.regions) > 0` |
| `smtp_configured` | step 3 (получатели) | `smtp_test.last_result.ok` **OR** `email_skipped=true` |
| `recipients_set` | step 4 (тест-письмо) | `len(notifications.email.recipients) > 0` **OR** `email_skipped` |
| `completed` | `/` (главная) | `test_email_sent=true` **OR** `email_skipped` |

`email_skipped` — флаг, выставляемый при выборе «Пропустить email» (см. decisions-log → «Ответы дизайнеру»).

## Transitions

```
            advance(regions)         advance(smtp_ok|skip)
not_started ───────────────► regions_set ──────────────► smtp_configured
                                                            │
                                advance(recipients|skip)    ▼
                                                       recipients_set
                                                            │
                                advance(test_ok|skip)       ▼
                                                        completed
```

Любая попытка пропустить state (POST на guard-protected endpoint) → 409 Conflict с телом `{"error": "invalid_transition", "current_state": "<state>", "redirect_to": "/onboarding"}`.

## Контракт OnboardingService

```python
class OnboardingState(Enum):
    NOT_STARTED      = "not_started"
    REGIONS_SET      = "regions_set"
    SMTP_CONFIGURED  = "smtp_configured"
    RECIPIENTS_SET   = "recipients_set"
    COMPLETED        = "completed"


class OnboardingService(Protocol):
    def current(self) -> OnboardingState: ...
    # Читает state.value("onboarding_state"), default NOT_STARTED.

    def can_advance(self, from_state: OnboardingState,
                    to_state: OnboardingState) -> bool: ...
    # Проверяет guard для перехода. Чистая функция: смотрит на settings/notif_repo.

    def advance(self, from_state: OnboardingState,
                to_state: OnboardingState) -> None: ...
    # Атомарно (BEGIN IMMEDIATE):
    #   1) verify current() == from_state (защита от concurrent transitions);
    #   2) verify can_advance(from_state, to_state) == True;
    #   3) UPDATE state SET value=to_state.value WHERE key='onboarding_state';
    # Raises InvalidTransitionError(current, requested) при провале guard.

    def skip_email(self) -> None: ...
    # Atomic SET state.email_skipped=true. Применяется только в state
    # smtp_configured или recipients_set.

    def url_for_current_step(self) -> str: ...
    # Маппит state на URL UI: regions_set → /onboarding/smtp, и т.д.
```

## Middleware `onboarding_gate`

```python
async def onboarding_gate(request: Request, call_next):
    if request.url.path.startswith(("/static/", "/sse/", "/api/health",
                                     "/onboarding")):
        return await call_next(request)

    state = container.services.onboarding.current()
    if state != OnboardingState.COMPLETED:
        target = container.services.onboarding.url_for_current_step()
        return RedirectResponse(target, status_code=302)
    return await call_next(request)
```

Ключевая деталь: target определяется **сервером** на основании текущего state в БД, **не** из query-param. Если пользователь руками меняет URL на `/onboarding/done` — он редиректнется обратно на свой валидный step.

## Guards в деталях

### `not_started → regions_set`
- Источник входа: `POST /onboarding/regions` с телом `{"regions": [1, 2]}`.
- Guard: `len(regions) > 0`, каждый из `{1, 2}` (валидируется Pydantic).
- Эффект: `Settings.regions` обновлён в `config.json`, далее `advance()`.

### `regions_set → smtp_configured`
- Источник входа A: `POST /onboarding/smtp/test` → если `NotifyResult.ok` → `advance()`.
- Источник входа B: `POST /onboarding/smtp/skip` → `skip_email() → advance()`.
- Guard: `smtp_test_last_result.ok` (хранится в `state` key `smtp_test_last_result_ok`, TTL 5 мин) **OR** `email_skipped`.

### `smtp_configured → recipients_set`
- Источник входа: `POST /onboarding/recipients` с `{"recipients": ["a@b"]}`.
- Guard: `len(recipients) > 0` (Pydantic EmailStr) **OR** `email_skipped`.

### `recipients_set → completed`
- Источник входа: `POST /onboarding/test-email` → `NotifyResult.ok` → `advance()`.
- Guard: тест-письмо успешно доставлено в текущей сессии (key `onboarding_test_email_ok`) **OR** `email_skipped`.

## Concurrency

`advance()` использует `BEGIN IMMEDIATE` (см. ADR-016) — две вкладки не могут одновременно advance. Вторая увидит `InvalidTransitionError(current=<уже_новый>, requested=<старый>)` и редиректнется на свой step.

## Тесты

Layer 2 (unit, fakes):
- Каждая legal transition — green.
- Каждая illegal (skip state, повторный advance) — `InvalidTransitionError`.
- Параллельный advance из двух потоков — ровно один success.

Layer 4 (integration, TestClient):
- `GET /` в `not_started` → 302 на `/onboarding/regions`.
- `GET /onboarding/done` без `completed` → 302 на `url_for_current_step()`.
- `POST /onboarding/recipients` в `not_started` → 409 invalid_transition.

## Storage

Key-value в `state`:
- `onboarding_state` — текущий state (one of `OnboardingState`).
- `email_skipped` — `"true"` / отсутствует.
- `smtp_test_last_result_ok` — `"true"` + `updated_at` для TTL.
- `onboarding_test_email_ok` — `"true"`.
- `onboarding_completed_at` — ISO timestamp (audit).

См. также:
- [[architecture]] §3.5 (контракт), §11 (ADR-018)
- [[decisions-log]] (ADR-018)
- [[data-model]] (OnboardingState model)
