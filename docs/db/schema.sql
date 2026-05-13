-- fis-monitor SQLite schema (canonical)
-- Version: 1
-- See: docs/decisions-log.md → «Хранение», «Idempotency notifier», «SQLite concurrency»
--      docs/brainstorm-storage-schema.md (полное обоснование)
--      docs/data-model.md (Pydantic-зеркала)

-- ============================================================================
-- PRAGMAs
-- ============================================================================
-- РАЗДЕЛЕНИЕ (см. architecture.md §3.1, ADR «Per-connection PRAGMA vs persistent»):
--   1) Persistent PRAGMA — применяются ОДИН раз при инициализации БД (этот файл).
--      Хранятся в самом файле БД и переживают reconnect.
--   2) Per-connection PRAGMA — применяются на КАЖДОМ новом коннекте кодом
--      (см. ThreadLocalConnectionProvider._configure):
--        busy_timeout = 5000
--        synchronous  = NORMAL
--        foreign_keys = OFF
--        temp_store   = MEMORY
--        cache_size   = -20000     (≈ 20 МБ)
--        mmap_size    = 268435456  (256 МБ)
--      В этом файле их ставить НЕ нужно — они потеряются на reconnect.
--   3) sqlite3.connect(check_same_thread=False) — обязательный аргумент в
--      _configure. Threading-safety гарантируется per-thread provider'ом,
--      sqlite-проверку отключаем (иначе невозможен close_all() из shutdown).
--
-- ИНВАРИАНТ writers (см. architecture.md §3.1, ADR-016):
--   Все read-then-write операции репозиториев (upsert, mark_inactive,
--   set_last_known_id) ОТКРЫВАЮТ tx через BEGIN IMMEDIATE — захват
--   writer-lock сразу, без race window между SELECT old и UPDATE.
--
-- Persistent PRAGMA:
PRAGMA journal_mode = WAL;
PRAGMA auto_vacuum  = INCREMENTAL;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA user_version = 2;
-- user_version bumped 1→2 (R4-M8): добавлены колонки notifications
--   (status, attempt_no, last_attempt_at) + расширение smtp_credentials
--   (smtp_host, smtp_port). См. ADR-019, ADR-020 и MigrationRunner v1→v2.
-- ВНИМАНИЕ: per-connection PRAGMA wal_autocheckpoint=1000 ДУБЛИРУЕТСЯ в
-- ThreadLocalConnectionProvider._configure() (R4-minor) — persistent-значение
-- срабатывает только если БД создавалась через этот файл; на чужих БД
-- (например, после restore из бэкапа) per-connection дубль гарантирует
-- одинаковое поведение.
-- Maintenance (см. architecture.md §7.2.bis):
--   раз в час   — PRAGMA wal_checkpoint(RESTART)    [не TRUNCATE: RESTART
--                  работает при активных читателях, TRUNCATE — no-op]
--   раз в сутки — PRAGMA incremental_vacuum         [требует auto_vacuum=INCREMENTAL]

-- ============================================================================
-- MIRROR (можно стереть и переразобрать из lot_html_archive)
-- ============================================================================

CREATE TABLE IF NOT EXISTS lots (
    id                   INTEGER PRIMARY KEY,        -- data-key сайта (== rowid)
    cadastral_no         TEXT    NOT NULL,
    area_sqm             INTEGER,
    region               TEXT    NOT NULL,
    municipality         TEXT,
    land_category        TEXT,
    permitted_use        TEXT,                       -- ВРИ
    ogv                  TEXT,
    status               TEXT    NOT NULL,
    date_create          TIMESTAMP NOT NULL,
    date_update          TIMESTAMP,
    lat                  REAL,
    lon                  REAL,
    has_boundaries       INTEGER CHECK (has_boundaries IN (0, 1) OR has_boundaries IS NULL),
    raw_json             TEXT    NOT NULL DEFAULT '{}',
    parser_version       INTEGER NOT NULL DEFAULT 1,

    -- ВНИМАНИЕ: DEFAULT CURRENT_TIMESTAMP оставлен ТОЛЬКО как safety-net.
    -- Инвариант (см. architecture.md §10.9): UTC ISO время в БД пишет код
    -- через Clock.now().isoformat(). Repository всегда передаёт значение явно.
    first_seen           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    detail_fetched_at    TIMESTAMP,
    enrichment_status    TEXT,                       -- 'pending'|'done'|'failed'|'permanent_fail'
    enrichment_retries   INTEGER NOT NULL DEFAULT 0,
    enrichment_last_error TEXT,

    -- Removal-tracking (decisions-log → Removal-detection)
    last_seen_at         TIMESTAMP,
    last_status          TEXT,
    last_status_at       TIMESTAMP,
    is_active            INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    inactive_reason      TEXT,                       -- 'status_changed'|'hard_removed'|'list_absent'
    inactive_since       TIMESTAMP,
    inactive_confirmed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lots_cadastral_no ON lots(cadastral_no);
CREATE INDEX IF NOT EXISTS idx_lots_region       ON lots(region);
CREATE INDEX IF NOT EXISTS idx_lots_status       ON lots(status);
CREATE INDEX IF NOT EXISTS idx_lots_date_create  ON lots(date_create);
CREATE INDEX IF NOT EXISTS idx_lots_municipality ON lots(municipality);
CREATE INDEX IF NOT EXISTS idx_lots_enrichment   ON lots(enrichment_status)
    WHERE detail_fetched_at IS NULL;
-- ВНИМАНИЕ (R3-minor): этот индекс частично дублирует idx_lots_stale
-- (последний — partial, более эффективен для is_active=1).
-- Пометка: после первых production-запросов проверить EXPLAIN QUERY PLAN на
-- реальных запросах removal-detection — возможно удалить idx_lots_active_last_seen
-- если не используется ни одним «hot» запросом.
CREATE INDEX IF NOT EXISTS idx_lots_active_last_seen ON lots(is_active, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_lots_inactive_since   ON lots(inactive_since)
    WHERE is_active = 0;
-- Partial-индекс на «протухающие» лоты (последний раз видели давно).
-- Используется removal-detection (architecture.md §7.2.bis).
CREATE INDEX IF NOT EXISTS idx_lots_stale            ON lots(last_seen_at)
    WHERE is_active = 1;

-- История изменений: status, area_sqm, date_update, auction, is_active, list_presence
--
-- ФОРМАТ old_value/new_value (см. architecture.md §3.6.1, N-M9):
--   Скаляр кодируется через json.dumps(value, ensure_ascii=False).
--   Это даёт type-roundtrip через единый формат:
--     int  → "42"
--     str  → "\"Свободен\""
--     bool → "true" / "false"
--     null → "null"        (поле потеряло значение)
--     dict → "{\"a\":1}"   (для будущих JSON-полей)
--   На чтении — json.loads. Repository отвечает за encoding в одной tx с UPSERT.
--
-- ИНВАРИАНТ: вставка строк в lots_history идёт ВНУТРИ той же tx, что и
-- UPSERT в lots (см. LotRepository.upsert contract, ADR-016).
CREATE TABLE IF NOT EXISTS lots_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id      INTEGER NOT NULL,
    field       TEXT    NOT NULL,
    old_value   TEXT,                                -- json-encoded scalar
    new_value   TEXT,                                -- json-encoded scalar
    changed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lots_history_lot ON lots_history(lot_id, changed_at);
-- Для retention DELETE (1 год, см. architecture.md §7.2.bis).
CREATE INDEX IF NOT EXISTS idx_history_changed_at ON lots_history(changed_at);

-- Архив HTML карточек (gzip-сжатый, ~30 МБ через год)
CREATE TABLE IF NOT EXISTS lot_html_archive (
    lot_id          INTEGER PRIMARY KEY,
    html            BLOB    NOT NULL,                -- gzip
    fetched_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    parser_version  INTEGER NOT NULL DEFAULT 1
);

-- ============================================================================
-- FTS5 / R-tree (виртуальные таблицы)
-- ============================================================================

-- Contentless FTS, sync через триггеры ниже
CREATE VIRTUAL TABLE IF NOT EXISTS lots_fts USING fts5(
    cadastral_no,
    municipality,
    permitted_use,
    ogv,
    content='lots',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2 tokenchars '_:.-'"
);

CREATE VIRTUAL TABLE IF NOT EXISTS lots_rtree USING rtree(
    id,
    min_lat, max_lat,
    min_lon, max_lon
);

-- FTS5 sync triggers
CREATE TRIGGER IF NOT EXISTS lots_ai AFTER INSERT ON lots BEGIN
    INSERT INTO lots_fts(rowid, cadastral_no, municipality, permitted_use, ogv)
    VALUES (new.id, new.cadastral_no, new.municipality, new.permitted_use, new.ogv);
END;

CREATE TRIGGER IF NOT EXISTS lots_ad AFTER DELETE ON lots BEGIN
    INSERT INTO lots_fts(lots_fts, rowid, cadastral_no, municipality, permitted_use, ogv)
    VALUES ('delete', old.id, old.cadastral_no, old.municipality, old.permitted_use, old.ogv);
END;

-- Условный AU-триггер: re-index FTS только когда индексируемые поля менялись.
-- Без WHEN-фильтра каждый UPSERT (включая обновления last_seen/enrichment/...)
-- триггерит лишний delete+insert в FTS — это десятки тысяч операций в день.
CREATE TRIGGER IF NOT EXISTS lots_au AFTER UPDATE ON lots
WHEN old.cadastral_no  IS NOT new.cadastral_no
  OR old.municipality  IS NOT new.municipality
  OR old.permitted_use IS NOT new.permitted_use
  OR old.ogv           IS NOT new.ogv
BEGIN
    INSERT INTO lots_fts(lots_fts, rowid, cadastral_no, municipality, permitted_use, ogv)
    VALUES ('delete', old.id, old.cadastral_no, old.municipality, old.permitted_use, old.ogv);
    INSERT INTO lots_fts(rowid, cadastral_no, municipality, permitted_use, ogv)
    VALUES (new.id, new.cadastral_no, new.municipality, new.permitted_use, new.ogv);
END;

-- ============================================================================
-- R-tree sync: ПРИВАТНЫЙ метод репозитория, никогда снаружи
-- ============================================================================
-- Решение (ADR-016, N-M3): _sync_geo — приватный метод SqliteLotRepository,
-- вызывается ТОЛЬКО внутри upsert() в рамках одной tx с UPDATE lots.
-- Из публичного Protocol LotRepository sync_geo УБРАН.
-- Триггера НЕТ, потому что:
--   1) lat/lon обновляются крайне редко (после enrichment карточки);
--   2) R-tree принимает min_lat/max_lat/min_lon/max_lon (точку записываем как
--      min==max), нельзя выразить чисто из NEW.* без CASE на NULL.
-- Если решим обратное — заменять на CREATE TRIGGER с WHEN-фильтром на
-- (new.lat IS NOT NULL AND new.lon IS NOT NULL AND (old.lat IS NOT new.lat OR
--  old.lon IS NOT new.lon)).
-- Если появится legitimate use case менять координаты отдельно — публичный
-- update_geo(lot_id, lat, lon) в BEGIN IMMEDIATE + внутри _sync_geo.
-- Integration-test инвариант (см. architecture.md §9): после любого write
-- в lots с не-NULL lat/lon → COUNT(*) для lot_id в lots_rtree строго 1.

-- ============================================================================
-- Циклы мониторинга
-- ============================================================================

CREATE TABLE IF NOT EXISTS cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    region          INTEGER NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          TEXT    NOT NULL                  -- 'open' (in-flight) | 'ok'|'error'|'aborted'
                       CHECK (status IN ('open', 'ok', 'error', 'aborted')),
    lots_fetched    INTEGER NOT NULL DEFAULT 0,
    new_lots        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    id_schema_check TEXT    NOT NULL DEFAULT 'ok'    -- 'ok'|'anomaly'|'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_cycles_started ON cycles(started_at);
CREATE INDEX IF NOT EXISTS idx_cycles_region  ON cycles(region, started_at);

-- ============================================================================
-- USER-STATE (НЕ теряем при пересборе mirror)
-- ============================================================================

CREATE TABLE IF NOT EXISTS lot_user_state (
    lot_id        INTEGER PRIMARY KEY,
    starred       INTEGER NOT NULL DEFAULT 0 CHECK (starred   IN (0, 1)),
    submitted     INTEGER NOT NULL DEFAULT 0 CHECK (submitted IN (0, 1)),
    submitted_at  TIMESTAMP,
    note          TEXT,
    seen_at       TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lus_starred   ON lot_user_state(starred)   WHERE starred = 1;
CREATE INDEX IF NOT EXISTS idx_lus_submitted ON lot_user_state(submitted) WHERE submitted = 1;

-- Журнал отправленных уведомлений + state-machine попыток (ADR-019).
-- PK гарантирует идемпотентность одной записи на адресата
-- (decisions-log → «Idempotency notifier»). recipient='local' для browser/heartbeat.
--
-- State machine (см. notifications.md → «State machine»):
--   reserve  → INSERT OR IGNORE row со status='pending', attempt_no=0
--   attempt  → UPDATE attempt_no=attempt_no+1, last_attempt_at=? WHERE status='pending'
--   sent     → UPDATE status='sent', sent_at=? WHERE status='pending'
--   permanent_fail → UPDATE status='permanent_fail' WHERE status='pending'
--
-- Все операции — внутри BEGIN IMMEDIATE (см. ADR-016, ADR-019).
-- На рестарте Dispatcher видит status='pending' и продолжает retry с того же attempt_no.
CREATE TABLE IF NOT EXISTS notifications (
    lot_id          INTEGER NOT NULL,
    channel         TEXT    NOT NULL,               -- 'email'|'browser'|'heartbeat'
    recipient       TEXT    NOT NULL,               -- email или 'local'
    status          TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'sent', 'permanent_fail')),
    attempt_no      INTEGER NOT NULL DEFAULT 0,     -- инкрементируется на каждой попытке
    -- TODO (R5 review — DB): defense-in-depth `CHECK (attempt_no >= 0 AND attempt_no < 1000)`
    -- против runaway-counter при возможном баге retry-loop.
    last_attempt_at TIMESTAMP,                       -- время последней попытки (nullable до первой)
    sent_at         TIMESTAMP,                       -- NULL пока status='pending'/'permanent_fail'
    PRIMARY KEY (lot_id, channel, recipient)
);
-- R4-M9: partial + DESC. Запросы list_recent / audit идут только по
-- status='sent' и сортируют по sent_at DESC. Старый full-table-индекс
-- содержал NULL-строки (pending/permanent_fail) — балласт.
CREATE INDEX IF NOT EXISTS idx_notifications_sent_at ON notifications(sent_at DESC)
    WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS idx_notifications_channel ON notifications(channel, sent_at);
-- TODO (R5 review — DB): после R5 sent_at стал NULLable → индекс содержит NULL-балласт для pending.
-- Привести к виду `WHERE status='sent'` partial (симметрично idx_notifications_sent_at).
-- Для recovery после рестарта: «найди все pending старше N минут» (Dispatcher
-- consumer-loop на старте). Partial-индекс, маленький.
-- ВНИМАНИЕ (R4-C3): индекс хранит NULL last_attempt_at — запрос recovery
--   `WHERE status='pending' AND (last_attempt_at IS NULL OR last_attempt_at < :cutoff)`
-- учитывает обе ветки (NULL = zombie-резерват после reserve() до первого
-- mark_attempt(); < cutoff = нормальный pending после неудачной попытки).
-- ВАЖНО (R4-minor): запросы должны использовать ТОЧНЫЙ предикат
-- `status='pending'`. Конструкции `status IN ('pending', ...)` индекс не
-- используют (SQLite планировщик не сможет применить partial WHERE).
CREATE INDEX IF NOT EXISTS idx_notifications_pending  ON notifications(last_attempt_at)
    WHERE status = 'pending';
-- Retention: permanent_fail старше 90 дней удаляются в maintenance
-- (chunked DELETE, см. architecture.md §7.2.bis).

-- SMTP-логин/пароль/host/port (decisions-log → «SMTP-пароль хранится в state.db», ADR-020).
-- ОДНА строка: id=1 enforced CHECK-ом.
--
-- ADR-020 (R4-C1): smtp_host / smtp_port — ЗДЕСЬ (SSOT = state.db), НЕ в config.json.
-- Причины: (а) когезия с smtp_user/smtp_password (один атомарный апдейт smtp-секции
-- → одна tx, без race между config.json и state.db); (б) защита от config-write-vector
-- (атакующий с write-доступом к config.json не сможет перенаправить SMTP на свой хост,
-- т.к. host читается из state.db с ACL %LOCALAPPDATA%); (в) Pydantic-модель Settings
-- НЕ содержит smtp_host/smtp_port — отсутствие write-вектора через config-reload.
-- В config.json остаются только дефолтные значения для прешитого бот-ящика как
-- литералы в коде (smtp.yandex.ru:587). Любой override пишется в state.db.
CREATE TABLE IF NOT EXISTS smtp_credentials (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    smtp_user     TEXT    NOT NULL,
    smtp_password TEXT    NOT NULL,                                       -- plain, ACL %LOCALAPPDATA%
    smtp_host     TEXT    NOT NULL,                                       -- R4-C1, ADR-020
    smtp_port     INTEGER NOT NULL DEFAULT 587 CHECK (smtp_port BETWEEN 1 AND 65535),  -- R4-C1, ADR-020
    use_default   INTEGER NOT NULL DEFAULT 1 CHECK (use_default IN (0, 1)),
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Key-value state. Известные ключи (key namespace, R4-M14):
--   last_known_id_<region>          INTEGER (например last_known_id_1, last_known_id_2)
--   session_expired                 BOOL    ('1' / отсутствует)
--   onboarded                       BOOL    (legacy — до перехода на onboarding_state FSM)
--   onboarding_state                TEXT    (FSM, см. onboarding.md и ADR-018)
--   onboarding_step                 INTEGER (legacy, deprecated)
--   onboarding_completed_at         TIMESTAMP (audit)
--   smtp_test_last_result_ok        BOOL    (TTL 5 мин — guard regions_set→smtp_configured)
--   email_skipped                   BOOL    (пользователь выбрал «Пропустить email»)
--   onboarding_test_email_ok        BOOL    (guard recipients_set→completed)
--   last_visit_at                   TIMESTAMP
--   last_full_scan_at               TIMESTAMP
--   monitor_paused                  BOOL
--   dnd_until                       TIMESTAMP
--   last_critical_event:session     JSON    (TTL 1 час, R3-C5, see ADR-008 ext)
--   last_critical_event:cycle       JSON    (TTL 1 час)
--   last_critical_event:smtp        JSON    (TTL 1 час)
--
-- НЕ в этой таблице:
--   cycle_in_progress — in-memory threading.Event (N-M8, ADR-005 ext);
--                       это soft-yield координатор, потеря на рестарте OK.
CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
