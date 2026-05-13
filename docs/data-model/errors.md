# DomainError hierarchy, ErrorCategory

Доменные исключения и категории ошибок для двухконтурной error-strategy ([[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]]).

## ErrorCategory

R4-M5: `error_category` — закрытый Literal-enum. Произвольная строка (`exception.__class__.__name__`, например) — НЕ допускается. Mapper в use case переводит низкоуровневое исключение в одну из категорий.

```python
ErrorCategory = Literal[
    "network", "http_5xx", "http_4xx", "redirect_login",
    "timeout", "parse_bug", "schema_anomaly",
]
```

## DomainError / UpstreamError

См. [[architecture/08-error-strategy]] — двухконтурная модель. Базовые исключения:

```python
class DomainError(Exception): ...
    # Внутренний баг, инвариант. Не ожидаемый failure mode.
    # Подклассы: SchemaAnomalyError, InvariantViolationError.

class UpstreamError(Exception):
    category: Literal["network", "http_5xx", "http_4xx", "redirect_login", "timeout"]
    # Ожидаемая сетевая/upstream ошибка. Поднимается из адаптеров.

class ParseBugError(DomainError): ...
    # Контракт парсера сломан: селектор не нашёл поле. БАГ — поднимается в cycle.error.

class ParserVersionMismatch(DomainError): ...
    # Старая запись с parser_version=N, реальный парсер уже N+1.
    # НЕ ошибка цикла — триггер lazy reparse.

class SmtpHostPolicyError(UpstreamError):
    # Наследуется от UpstreamError по решению [[decisions/ADR-022-allowed-tracked-fields-ssot-smtp-policy-error|ADR-022]].
    # SmtpEmailNotifier.send() ловит UpstreamError единым except-блоком.
```

См. также: [[architecture/03-protocols]] §3.6.2, [[architecture/08-error-strategy]], [[data-model/sse]] (SseCycleError использует ErrorCategory).
