# ADR-003: Error strategy — Exception для всего, Result только для Notifier

**Context.** Когда использовать Exception vs Result?

**Decision.** Двухконтурно. **Contour 1**: `UpstreamError(category=...)` (network/http_4xx/http_5xx/redirect_login/timeout) и `DomainError` — exception. Поднимаются из адаптеров, ловятся в use case `run_forever()`. **Contour 2**: `NotifyResult(ok, detail, retryable)` — только для `Notifier.send()` и `.test()`. Один канал упал — остальные идут, нужна структура для retry по `retryable`.

**Consequences.** HttpClient — exception (никакого Result). Нотификации — Result, retry на основании `retryable` флага. Python без `?`-оператора слишком шумный для всеобщего Result.

См. также: [[decisions-log]], [[architecture/08-error-strategy]], [[data-model/errors]].
