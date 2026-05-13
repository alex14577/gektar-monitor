---
bd-id: gektar_monitor-bye.3
title: Infra — SmtpHostPolicy.resolve_and_check
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/domain/errors.py
  - src/fis_monitor/infra/__init__.py
  - src/fis_monitor/infra/smtp/__init__.py
  - src/fis_monitor/infra/smtp/host_policy.py
  - tests/infra/__init__.py
  - tests/infra/smtp/__init__.py
  - tests/infra/smtp/test_host_policy.py
---

# Infra — SmtpHostPolicy.resolve_and_check

## Что сделано

- Реализована `DefaultSmtpHostPolicy` с injectable resolver (тестируется без реального DNS).
- Pre-resolve отсев: пустая строка, `"localhost"`, `"0"`, integer-форматы IP,
  internal TLDs (`.local`, `.internal`, `.lan`, `.corp`, `.home`, `.localdomain`,
  `.test`, `.example`, `.invalid`, `.localhost`) — case-insensitive, tolerant к trailing dot.
- IP-литерал short-circuit: блокируется до `getaddrinfo` (нет сетевого обращения).
- Fail-closed по всем адресам из `getaddrinfo` (DNS-rebinding multi-record).
- IPv4-mapped IPv6 (`::ffff:a.b.c.d`) unwrap перед blocklist.
- Cloud-metadata (`169.254.169.254` / `fd00:ec2::254`) — явное сообщение в ошибке.
- Blocklist через `ipaddress` stdlib: `is_private`, `is_loopback`, `is_link_local`,
  `is_multicast`, `is_reserved`, `is_unspecified` + `255.255.255.255`.
- `gaierror` оборачивается в `SmtpHostPolicyError(UpstreamError)` без утечки сообщений
  от resolver'а (см. [[decisions-log#ADR-022]]).
- 39 тестов + 1 skipped (real-DNS smoke).

## Почему так

- `resolve_and_check() -> ResolvedSmtpEndpoint` закрывает TOCTOU между policy-check и
  connect: возвращает pin'нутый IP, `SmtpEmailNotifier.send()` коннектится по `endpoint.ip`,
  но SNI/TLS-cert верифицирует по `endpoint.original_host` — [[decisions-log#ADR-015]] R3-C4.
- DNS resolve ВНЕ любой БД-транзакции (R4-M2): `getaddrinfo` может блокироваться на сотни
  миллисекунд, держать `BEGIN IMMEDIATE` в это время = SQLITE_BUSY для всех writers.
- `SmtpHostPolicyError` наследует `UpstreamError` — [[decisions-log#ADR-022]]: network-layer
  ошибки из policy входят в ту же категорию `upstream`, что HTTP/SMTP-ошибки.
- Manual STARTTLS (следующий шаг `bye.4`) потребовал именно `ResolvedSmtpEndpoint` с
  отдельными `ip` и `original_host` — [[decisions-log#ADR-021]].

## Связи

- Закрывает: `bd #gektar_monitor-bye.3`
- Зависит от: [[gektar_monitor-531.1]] (ResolvedSmtpEndpoint, SmtpHostPolicyError)
- Связано: [[decisions-log#ADR-015]], [[decisions-log#ADR-021]], [[decisions-log#ADR-022]],
  [[data-model#ResolvedSmtpEndpoint]]
- Новые термины: [[glossary#SmtpHostPolicy]], [[glossary#ResolvedSmtpEndpoint]]

## Follow-up

- Разблокирован: `gektar_monitor-bye.4` (SmtpEmailNotifier с manual STARTTLS + Message-ID).
- Разблокирован: `gektar_monitor-a4t.6` (SettingsService + SmtpTestService).
