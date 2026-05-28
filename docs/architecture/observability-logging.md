# Observability — Structured Logging Registry

Registry of structured DEBUG/INFO events emitted across services and infra layers.
All events use `logger.debug("module.event.name", extra={...})` convention.

## Conventions

- **Format**: `logger.debug("module.event.name", extra={"key": value, ...})`
- **Logger**: `logging.getLogger(__name__)` — one per module, always `fis_monitor.*`
- **PII rules** ([[decisions/ADR-012-pii-isolation|ADR-012]]): `lot_id`, `region_id`, `duration_ms`, `cycle_id`, `ids_count` are safe. Email addresses, cookies, session tokens — NEVER logged in plaintext; SHA-256[:8] hex prefix used for correlation only.
- **Level discipline**: DEBUG for granular flow tracing; INFO for lifecycle milestones; WARNING for degraded-but-recoverable states.

---

## Event Registry

### monitor_cycle.* — `fis_monitor.services.monitor_cycle`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `monitor_cycle.cycle.start` | DEBUG | `region_id` |
| `monitor_cycle.region.fetch.start` | DEBUG | `region_id`, `cycle_id` |
| `monitor_cycle.region.fetch.finish` | DEBUG | `region_id`, `cycle_id`, `http_status`, `duration_ms` |
| `monitor_cycle.region.upsert` | DEBUG | `region_id`, `lot_id`, `was_new`, `changes_count` |
| `monitor_cycle.cycle.finish` | DEBUG | `region_id`, `cycle_id`, `status`, `lots_fetched`, `new_lots` |

### full_scan.* — `fis_monitor.services.full_scan`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `full_scan.region.start` | DEBUG | `region_id` |
| `full_scan.region.finish` | DEBUG | `region_id`, `ids_count`, `pagination_completed` |
| `full_scan.removal_candidates.detected` | DEBUG | `total_seen_ids` |
| `full_scan.mark_inactive` | INFO | `lot_id`, `reason` |

### dispatcher.* — `fis_monitor.services.notifier_dispatcher`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `dispatcher.dispatch.entry` | DEBUG | `lot_id`, `region_id`, `channels_count` |
| `dispatcher.channel.invoked` | DEBUG | `lot_id`, `channel_id`, `recipients_count` |
| `notification.subscribed_at_dropped` | DEBUG | `region_id`, `lot_id`, `lot_date_create`, `subscribed_at`, `decision` |

### session_expired.* — `fis_monitor.services.session_expired_email`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `session_expired.detected` | INFO | `event_type` |
| `session_expired.idempotency_skip` | DEBUG | `guard_key` |
| `session_expired.notification.queued` | DEBUG | `recipients_count` |

### config.* — `fis_monitor.infra.config_source`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `config.file_event` | DEBUG | `path` |
| `config.debounce.scheduled` | DEBUG | `delay_ms` |
| `config.reload.start` | DEBUG | `hash_old`, `hash_new` |
| `config.reload.finish` | DEBUG | `hash_old`, `hash_new`, `regions_diff_count` |
| `config.bootstrap_subscriptions` | DEBUG | `regions_seeded_count` |

### sse.* — `fis_monitor.infra.sse.bus` + `fis_monitor.infra.sse.sse_stream`

Added: gektar_monitor-b9wq

| Event | Level | Logger | Extra fields |
|---|---|---|---|
| `sse.event.queued` | DEBUG | `bus` | `event_type`, `subscriber_count` |
| `sse.queue.drop` | WARNING | `bus` | `event_type`, `drop_reason` |
| `sse.subscribe` | DEBUG | `sse_stream` | `client_id`, `total_subscribers` |
| `sse.unsubscribe` | DEBUG | `sse_stream` | `client_id`, `reason` (`disconnect` or `dead_subscription`) |

### login.* — `fis_monitor.infra.playwright.login`

Added: gektar_monitor-b9wq

| Event | Level | Extra fields |
|---|---|---|
| `login.start.entry` | DEBUG | `trigger` (`headed` or `silent_refresh`), `profile_dir` |
| `login.lock.acquired` | DEBUG | `trigger` |
| `login.lock.timeout` | DEBUG | `trigger` |
| `login.cookie_export.start` | DEBUG | `cookies_count` |
| `login.cookie_export.finish` | DEBUG | `cookies_count`, `duration_ms` |
| `login.deadline.reached` | WARNING | `elapsed_ms`, `trigger` |
| `login.exception` | ERROR | `trigger`, `exc_type` |

### on_login_success.* — `fis_monitor.composition`

| Event | Level | Extra fields |
|---|---|---|
| `on_login_success.callback.fired` | DEBUG | `trigger` |

### license_expiry.* — `fis_monitor.services.license_expiry`

Added: gektar_monitor-rh35 ([[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]] §Runtime expiry enforcement)

| Event | Level | Extra fields |
|---|---|---|
| `license_expiry.supervisor.start` | INFO | — |
| `license_expiry.check.valid` | DEBUG | `today`, `exp`, `days_until_exp` |
| `license_expiry.check.expired` | WARNING | `today`, `exp` |
| `license_expiry.check.error` | ERROR | `error_type`, `today` |
| `license_expiry.shutdown_requested` | WARNING | `today`, `exp` |
| `license_expiry.watchdog.armed` | INFO | `grace_seconds` |
| `license_expiry.watchdog.fired` | CRITICAL | — |
| `license_expiry.supervisor.crash` | ERROR | `exc_type` |
| `license_expiry.supervisor.stop` | INFO | — |

PII policy: `today` and `exp` are dates (`YYYY-MM-DD`). `key_str`, `secret`, licensee, raw payload bytes — NEVER logged.

---

## Related Decisions

- [[decisions/ADR-012-pii-isolation|ADR-012]] — PII isolation contract (what may appear in logs)
- [[decisions/ADR-019-notification-state-machine|ADR-019]] — notification FSM (dispatcher events)
- [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] — EventBus (sse.* events)
