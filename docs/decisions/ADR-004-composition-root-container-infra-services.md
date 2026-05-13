# ADR-004: Composition root — самописный Container, разделённый на Infra/Services

**Context.** Контейнер для ~15 швов. Варианты: `dependency-injector`, `inject`, самописный.

**Decision.** Самописный, ~200 строк, типизирован. Container — НЕ один God-объект; разделён на frozen `Infra` (швы, repos, инфра-адаптеры) и frozen `Services` (use cases). Оба `repr=False` — против утечки secrets в crash-логи.

**Consequences.** Никакой магии, ясные слои сборки (Layer 0..4), порядок зависимостей виден по коду. Минус — больше boilerplate, чем `dependency-injector`. Принимаем.

См. также: [[decisions-log]], [[architecture/04-composition-root]].
