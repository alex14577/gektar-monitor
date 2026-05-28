---
name: licensing-index
description: MOC подсистемы лицензирования — ключи, HMAC, офлайн-верификация
type: reference
---

# Licensing system — Map of Content

Подсистема активационных ключей для `fis-monitor`. Stateless, offline, stdlib only.
Целевая сложность: **2/10** (явное требование заказчика).

## Архитектура и форматы

- [[licensing/architecture|Архитектура модулей]] — coupling-матрица, поток запуска
- [[licensing/key-format|Формат ключа]] — строка `v1.<payload>.<sig>`, поля payload
- [[licensing/crypto-hmac|HMAC-SHA256]] — sign/verify, constant-time, граничные случаи дат
- [[licensing/secret-obfuscation|XOR-обфускация секрета]] — honest security tradeoff

## API и интеграция

- [[licensing/module-api|Публичный API модуля]] — `LicenseStatus`, `LicenseResult`, `verify_license`
- [[licensing/license-key-file|Файл license.key]] — расположение, формат, `load_license_key`
- [[licensing/generator-cli|CLI-генератор]] — `init-secret`, `issue`, dev-only гарантия
- [[licensing/integration|Интеграция в app.py]] — fail-closed flow, composition root

## Качество и границы

- [[licensing/test-strategy|Стратегия тестирования]] — layer-матрица, smoke vs авто
- [[licensing/manual-smoke|Ручной smoke]] — runbook для проверки fail-closed перед релизом
- [[licensing/out-of-scope|Out of scope]] — что осознанно не реализуется

## Решение

- [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]] — выбор HMAC-SHA256 + XOR

## Спека

- [[superpowers/specs/2026-05-28-licensing-system-design|Спека v1]] — первоисточник
