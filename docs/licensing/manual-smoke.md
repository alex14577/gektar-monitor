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

После этого команда `fis-monitor` доступна как entry-point.

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

Past-date guard в `tools/gen_license.py` блокирует выпуск ключей прошедшей датой.
**Временно закомментировать** guard и восстановить после теста.

Найти блок:
```bash
grep -n "is before today" tools/gen_license.py
```
Закомментировать 3 строки начиная с `if exp is not None and exp < iat:` (включая print и return 1).

```bash
python -m tools.gen_license issue --expires 2020-01-01 --licensee Smoke --out license.key
fis-monitor --data-dir ./var-smoke
git checkout tools/gen_license.py && rm license.key
```
stderr: `ERROR: License expired on 2020-01-01. Renew your license.`

## 4. Валидный ключ → нормальный старт

```bash
python -m tools.gen_license issue --duration day --licensee Smoke --out license.key
fis-monitor --data-dir ./var-smoke   # CTRL+C для остановки
rm license.key
```
Ожидаемый результат: uvicorn стартует без строк `ERROR` в stderr.

## Cleanup

```bash
rm -f license.key && rm -rf ./var-smoke
git checkout tools/gen_license.py  # на случай прерванного сценария 3 (восстановить past-date guard)
# mv license.key.bak license.key  # если переименовывали
```
