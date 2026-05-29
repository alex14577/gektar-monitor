---
name: licensing-manual-smoke
description: Ручной smoke-тест fail-closed лицензионной проверки перед релизом
type: reference
---

# Ручной smoke — лицензионная проверка

`app.py:main` не покрывается автотестами (см. [[licensing/test-strategy]]). Runbook
проверяет fail-closed поведение перед релизом.

> **Предупреждение**: если в корне уже есть `license.key` — переименуйте его до smoke
> (`mv license.key license.key.bak`) и восстановите после.

## Prerequisites

Установка пакета в dev-режиме:
```bash
uv pip install -e .
# или: pip install -e .
```

После этого команды `fis-monitor` и `gektar-gen-license` доступны как entry-points.

## 1. Нет файла → exit 1

```bash
rm -f license.key && fis-monitor --data-dir ./var-smoke
```
stderr: `ERROR: license.key not found. Place a valid license file at ./license.key`

## 2. Битый ключ → exit 1

```bash
echo "garbage" > license.key && fis-monitor --data-dir ./var-smoke
rm license.key
```
stderr: `ERROR: License is invalid. Check license.key contents.`

## 3. Просроченный ключ → exit 1

Past-date guard в `src/fis_monitor/licensing/cli.py` блокирует выпуск ключей с `exp < nbf`.
**Временно закомментировать** guard и восстановить после теста.

Найти блок:
```bash
grep -n "is before" src/fis_monitor/licensing/cli.py
```
Закомментировать строки начиная с `if exp < nbf:` (включая print и return 1).

```bash
gektar-gen-license issue --nbf 2020-01-01 --exp 2020-01-01 --out .
fis-monitor --data-dir ./var-smoke
git checkout src/fis_monitor/licensing/cli.py && rm license.key
```
stderr: `ERROR: License expired on 2020-01-01. Renew your license.`

## 4. Валидный ключ → нормальный старт

```bash
gektar-gen-license issue --nbf 2026-01-01 --exp 2026-12-31 --out .
fis-monitor --data-dir ./var-smoke   # CTRL+C для остановки
rm license.key
```
Ожидаемый результат: uvicorn стартует без строк `ERROR` в stderr.

## 5. Интерактивный режим (двойной клик)

```bash
gektar-gen-license
```
Ожидаемый результат: три вопроса → файл `license.key` создан в текущей директории → пауза «Нажмите Enter для выхода…».

## Cleanup

```bash
rm -f license.key && rm -rf ./var-smoke
git checkout src/fis_monitor/licensing/cli.py  # на случай прерванного сценария 3
# mv license.key.bak license.key  # если переименовывали
```
