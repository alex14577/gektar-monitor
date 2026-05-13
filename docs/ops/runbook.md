# Runbook

Что делать, когда что-то сломалось. Каждый сценарий: **симптом → диагностика → действие → как предотвратить**.

## 1. Селекторы парсера сломались (сайт изменился)

**Симптом.**
- Поля лотов приходят `NULL` (cadastral_no, area_sqm, coords).
- В логах `parser.exception` или `AttributeError: 'NoneType' has no attribute 'text'`.
- Цикл проходит без ошибок, но `new_lots = 0` много дней подряд, хотя на сайте новые лоты видны.

**Диагностика.**
1. Открыть сайт вручную, сохранить актуальный HTML карточки/списка.
2. Сравнить с `tests/fixtures/cabinet-free-lot-*.html` (diff по структуре, классам, data-атрибутам).
3. Прогнать `pytest tests/unit/test_parser_*.py` на новых снапшотах — увидеть какие селекторы упали.

**Действие.**
1. Обновить селекторы в `parser_list.py` / `parser_detail.py`.
2. Положить новый снапшот в `tests/fixtures/` с датой.
3. Поднять `parser_version` в `db/schema.sql` (или константе). Старые строки лениво репарсятся.
4. Прогнать всю регрессию: `pytest tests/unit`.

**Предотвращение.** Daily smoke-job на сравнение хеша структуры списка. Алерт в логе если ключевые селекторы не находят узлов.

## 2. Сессия истекла / ЕСИА требует 2FA

**Симптом.**
- HTTP-клиент получает `302` на `/login` или `esia.gosuslugi.ru`.
- В UI плашка «Сессия истекла».
- В логах `auth.session_expired`.

**Действие.**
1. В UI клиент нажимает **«Перелогиниться»** (см. [[web/ui-architecture]]).
2. Сервер запускает Playwright в **headed** режиме с `user_data_dir=./profile`.
3. Клиент проходит ЕСИА (логин, пароль, 2FA) в открывшемся окне.
4. После редиректа на ФИС cookies автоматически обновлены в `profile/`. Окно закрывается.
5. Цикл мониторинга возобновляется.

**Предотвращение.** Heartbeat-проверка `GET /cabinet/free-lot` раз в N циклов. При первом 302 — алерт «нужен релогин», не ждать таймаута.

## 3. `state.db` повреждена

**Симптом.**
- `sqlite3.DatabaseError: database disk image is malformed`.
- Приложение не стартует или падает на любом запросе к БД.

**Диагностика.**
```bash
sqlite3 state.db "PRAGMA integrity_check;"
```

**Действие.**
1. **Бэкап**: `cp state.db state.db.corrupted-$(date +%F)`.
2. Если повреждение в mirror-таблицах (`lots`, `lot_history`) — стереть БД и перезалить: lazy enrichment первой страницы + цикл.
3. Если повреждение в user-state (`notifications`, `settings`) — попытаться `.recover`:
   ```bash
   sqlite3 state.db.corrupted ".recover" | sqlite3 state.db.new
   ```
   Перенести user-state из `state.db.new` в свежую БД.

**Предотвращение.** WAL-mode + `PRAGMA synchronous=NORMAL`. Еженедельный `VACUUM INTO` бэкап. См. [[architecture]] → §10.8 Backup-стратегия.

## 4. `app.lock` остался от мёртвого процесса

**Симптом.**
- При запуске: `Already running (pid=12345)`.
- Процесса 12345 в системе нет.

**Действие.**
1. Проверить PID:
   ```bash
   # Windows
   tasklist /FI "PID eq 12345"
   # Linux
   ps -p 12345
   ```
2. Если процесса нет — удалить файл `{data_dir}/app.lock` (Windows: `%LOCALAPPDATA%\fis-monitor\app.lock`, Linux: `~/.local/share/fis-monitor/app.lock`).
3. Запустить приложение заново.

**Предотвращение.** Логика автозабора lock-файла при мёртвом PID — см. код-сниппет в [[product/monitoring-plan]] → «Защита от двух копий».

## 5. Бот-ящик заблокирован Yandex (app-password отозван)

**Симптом.**
- SMTP-ошибка `535 Authentication failed` от Yandex.
- В логах `notifier.email.auth_error`.
- Письма не уходят, в `notifications` таблице `sent_at IS NULL`, попытки растут.

**Действие.**
1. В UI → «Настройки → Email → SMTP override» — клиент вводит **свой** SMTP (host/port/login/password). Логин/пароль сохраняются в `state.db` (таблица `smtp_credentials`), не в `config.json`.
2. Сохранить — изменения применяются к следующему циклу.
3. Повторить отправку через `POST /api/notifications/retry`.

**Предотвращение.** Мониторинг `notifier.email.auth_error` в логах. Документировать у клиента: «если бот-ящик упал — переключаемся на свой SMTP, инструкция в UI». См. [[notifications]].

## 6. Две копии программы запущены

**Симптом.** Не должно случаться — защищено `app.lock`. Если случилось:
- Дубли уведомлений.
- Гонка за `state.db` (WAL спасёт от потери данных, но логически плохо).

**Действие.**
1. Найти PIDы: `tasklist | findstr fis-monitor` (Windows) / `pgrep -fa fis_monitor` (Linux).
2. Убить лишний: `taskkill /PID <pid> /F`.
3. Проверить `app.lock` — должен остаться один PID.

**Предотвращение.** Не запускать через два разных пути установки. Использовать только ярлык из инсталлятора.

## 7. Сервер сайта в техработах

**Симптом.**
- HTTP `502`/`503`/`504` на запросы списка.
- В логах `monitor.cycle.upstream_5xx`.

**Действие.**
- Цикл автоматически уходит в **exponential backoff** (1m → 5m → 15m → 30m, cap).
- В UI поднимается флаг «сайт недоступен» (badge в шапке).
- После первого успешного ответа backoff сбрасывается.
- Ручных действий не нужно. Если 5xx > 6 часов — проверить, что ФИС вообще отвечает в браузере.

**Предотвращение.** Не делать собственных ретраев поверх backoff — удвоится нагрузка.

## 8. Enrichment стопорится (очередь не двигается)

**Симптом.**
- В UI «лотов в очереди enrichment: N» не уменьшается.
- Воркер enrichment без новых записей в логе.

**Диагностика.**
1. `tail -f logs/app.jsonl | jq 'select(.logger | startswith("enrichment"))'`.
2. Проверить, не уперлись ли в session-expired (см. сценарий 2) — enrichment должен останавливаться при 302.
3. Проверить лимит параллелизма (по умолчанию 10) — не зажат ли в 0.

**Действие.**
1. Если воркер живой, но простаивает — `POST /api/enrichment/retry`, чтобы перепоставить «зависшие» задачи в очередь.
2. Если умер — рестарт сервера (lock-файл подхватится, БД переживёт).

**Предотвращение.** Watchdog на пульс enrichment-воркера: если 5 минут нет активности при непустой очереди — алерт в UI.

## 9. ЕСИА flag-ает Chrome for Testing (Playwright 1.58)

**Симптом.**
- При логине через ЕСИА Playwright открывает Госуслуги, но появляется CAPTCHA, «подозрительная активность» или редирект обратно на форму без явной ошибки.
- В нормальном Chrome тот же клиент логинится без проблем.

**Причина.** С Playwright 1.57 дефолтный бинарь — **Chrome for Testing (CfT)**, не классический Chromium. У него slightly другая сигнатура (UA, иконка, отсутствие некоторых системных интеграций). Антифрод-системы Госуслуг могут на это реагировать.

**Диагностика.**
1. Логи `auth.playwright` — есть ли редиректы на CAPTCHA / `/anomaly`.
2. Сравнить UA: `navigator.userAgent` в DevTools запущенного Playwright vs обычный Chrome.
3. Попробовать залогиниться в обычном Chrome тем же клиентом — если работает, проблема в CfT.

**Действие.**
1. Откатить Playwright на `==1.56.0` (последний классический Chromium 141):
   ```
   pip install playwright==1.56.0
   python -m playwright install chromium
   ```
2. Удалить `profile/` (новая сессия с новым UA).
3. Повторный логин.

**Предотвращение.** При первой установке тестировать ЕСИА-логин в headed-режиме. Если 1.58 ломается у клиента — фиксируем 1.56 в его сборке.

## 10. Запрос диагностики у пользователя (R3-M10)

**Только через UI**: «Настройки → Диагностика → Скачать архив». Этот путь физически исключает чувствительные файлы (ADR-012, ADR-017):
- `audit.jsonl` (полный config-diff с PII: recipients, smtp.host) — НЕ включается.
- `smtp_credentials` table — DB cursor не открывает.
- crash dumps (`*.dmp`, `core.*`, `Werfault*`, `CrashDumps/`) — exclude.
- Логи редактируются: Cookie/Authorization/`?code=`/email/СНИЛС/паспорт/ИНН → `<redacted:...>`.
- Schema-snapshot fail-closed (R3-M5): новая колонка в БД без обновления `DIAGNOSTIC_SCHEMA_V1` — bundle НЕ собирается.

**НЕ просите пользователя**:
- Отправлять `data_dir/audit.jsonl` руками — там полный config-diff с PII.
- Архивировать `data_dir/` целиком — там `state.db` с `smtp_credentials` (логин/пароль plain).
- Прикладывать `CrashDumps/` или `*.dmp` — фрагменты адресного пространства, потенциально с secrets.

Если UI не работает (например, БД упала): сначала диагностируем через п.3 «state.db повреждена», далее — UI после восстановления.

## 11. systemd unit — TimeoutStopSec (R3-M3)

Для Linux-инсталляции (если когда-то — MVP только Windows): unit-файл ДОЛЖЕН содержать `TimeoutStopSec=45` (grace 35с + phase 1.5 ~5с + запас 5с). Меньше — systemd прибьёт процесс SIGKILL'ом во время phase 1.5, in-flight уведомления потеряются. Документируется в installer-скрипте.

## 12. Жалоба пользователя на дубль email-уведомления (R4-C5)

**Симптом.** Пользователь жалуется: «На один лот пришло 2 одинаковых письма за минуту».

**Причина.** SMTP — at-least-once на адресата (ADR-019 ext R4-C5). При крэше процесса между «250 OK от SMTP-сервера» и `mark_sent` COMMIT — recovery (`list_pending_older_than`) повторит отправку при следующем старте → второе письмо уйдёт. Окно дубля — секунды.

**Диагностика.**
1. Запросить у пользователя timestamp дубля (заголовок Date в письме).
2. В `app.jsonl` искать `notification.recovery_resend{lot_id, channel, prev_attempt_no}` в окне ±2min от timestamp:
   ```bash
   jq 'select(.event=="notification.recovery_resend")' app.jsonl | grep '<lot_id>'
   ```
3. Если событие есть — дубль ожидаемый (crash-recovery между ACK и COMMIT). Если события нет — копать дальше (возможно две копии процесса, см. сценарий 6).

**Действие.**
- Объяснить пользователю: at-least-once семантика — известная характеристика, не баг.
- Major MTA (Gmail/Yandex/Mail.ru/Outlook) обычно дедуплицируют по `Message-ID` — спросить, действительно ли два письма пришли (некоторые клиенты показывают «новое» уведомление при reprocessing того же ID).
- Если дубль фактический и регулярный → диагностировать частые крэши процесса (см. `app.jsonl` на panic/SIGTERM/OOM).

**Предотвращение.** Графichный shutdown через UI/Ctrl+C — не триггерит recovery-дубль (mark_sent коммитится до выхода). Дубль возможен только при abnormal termination (kill -9, OOM-killer, BSOD).

## 13. Audit-лог отключён из-за cloud-sync data_dir (R4-M7)

**Симптом.** В UI баннер: «Audit-лог отключён из-за cloud-sync data_dir, переместите данные на локальный путь». `audit.jsonl` в `data_dir/` пустой или отсутствует.

**Причина.** `warn_if_in_cloud_sync(data_dir)` сматчил cloud-sync паттерн (OneDrive/Dropbox/Yandex.Disk/Google Drive/iCloud/...). `audit.jsonl` writer заменён на no-op fail-closed: файл содержит PII (recipients, smtp.host) — попадание в облако = утечка вне controlled-ACL зоны.

**Действие.**
1. Найти `data_dir` в логах при старте (`logger.warning("data_dir_cloud_sync_detected: %s", path)`).
2. Перенести `data_dir` на локальный путь:
   - Windows: `%LOCALAPPDATA%\fis-monitor\` (НЕ `%USERPROFILE%\Documents` — может быть в OneDrive).
   - Linux: `~/.local/share/fis-monitor/`.
3. Перезапустить приложение. Если detection пропал — audit.jsonl снова пишется.

**Предотвращение.** Installer по умолчанию использует `%LOCALAPPDATA%`. Документация: не переносить data_dir в облачные папки.

## Windows-shutdown — known limitation (R3-M3)

При shutdown машины Windows даёт ~5с (`WaitToKillAppTimeout` по умолчанию). Phase 1 (35с) не успевает — in-flight HTTP/SMTP не докомитятся. Принимаем как known-limitation: monitor не гарантирует доставку in-flight уведомлений при shutdown машины. Graceful app-shutdown (через UI / Ctrl+C / закрытие иконки в трее) — гарантируется.

- **WSL2/Docker loopback pierce** (R3-M1 / R5 review — Security): bind на `127.0.0.1:8080` доступен с Windows-host через WSL2 forwarder и из Docker containers с `host.docker.internal`. Для desktop MVP приемлемо (single-user trust model). Для server-mode (v3) — перейти на Unix socket или явно требовать `wsl --shutdown` / `localhostForwarding=false`.

## См. также

- [[product/monitoring-plan]]
- [[notifications]]
- [[web/authentication]]
- [[decisions-log]]
