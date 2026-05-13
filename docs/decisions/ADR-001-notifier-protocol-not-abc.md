# ADR-001: Notifier — Protocol, не ABC

**Context.** Первая версия `notifications.md` описывала `Notifier` как `ABC` с дефолтным методом `send_to_all` и retry-логикой. Это связывает наследников с реализацией базы (изменение базы ломает наследников) и нарушает «composition over inheritance».

**Decision.** `Notifier` — `typing.Protocol`. Retry — функция-декоратор `with_retry(notifier, attempts, backoff) -> Notifier` (structurally compatible). `send_to_all` — снят с интерфейса, живёт в `NotifierDispatcher` (у него есть доступ к `NotificationsRepository` для idempotency).

**Consequences.** Плюсы: композиция, легче тестировать, легче добавлять каналы (нет требования наследоваться). Минус: дублирование `channel_id`/`display_name` декларации в каждом классе — но это всё равно ClassVar, не overhead.

См. также: [[decisions-log]], [[architecture/03-protocols]] §3.3, [[notifications]].
