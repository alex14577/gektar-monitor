# 5. Точки расширения (Open/Closed)

| Расширение | Что добавляешь | Где регистрируешь | Что НЕ трогаешь |
|---|---|---|---|
| Новый канал уведомлений (Telegram, ntfy, ...) | Класс реализует `Notifier` + `NotifierConfig` | `composition.py`: `registry.register(...)` | `NotifierDispatcher`, use cases, БД |
| Новый сайт-донор (другой регион, другая структура) | `XxxListParser`/`XxxDetailParser` + новый `MonitorCycleService` или параметризация существующего | `composition.py` или `multi-cycle service` | `LotRepository`, БД, web-слой |
| Новая платформа автозапуска (macOS) | `MacOsAutostart(AutostartManager)` | `build_autostart()` диспатч по `sys.platform` | use cases, контейнер |
| Новая стратегия сортировки/раннего выхода | `EarlyExitStrategy(Protocol)` — выделить из `MonitorCycleService` | `composition.py`: передать в `MonitorCycleService` | парсер, repo |
| Хостинг (PostgreSQL вместо SQLite) | `PostgresXxxRepository` для каждого repo | переключение в `build_container` по `MODE` | все use cases |
| Шифрование секретов (если threat model поменяется) | `EncryptedSmtpCredentialsRepository` (decorator над Sqlite-реализацией) | composition | все use cases, остальные репы |
| L2 verification стратегия | `RemovalVerifier(Protocol)`, реализации `ActiveVerifier`/`PassiveVerifier` | `composition.py` → `FullScanService` | repo, cycle |
| Каталог / поиск (v2) | новый use case `CatalogQueryService`, тащит из `LotRepository` + FTS | `web/routes/catalog.py` + `composition` | mirror-схема, monitor cycle |

**Ключевая идея OCP:** если для добавления фичи нужно править существующий use case или domain-модель — это знак, что нужен новый Protocol.
