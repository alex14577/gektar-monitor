---
bd-id: gektar_monitor-531.3
title: Domain — compute_changes (pure diff)
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/domain/diff.py
  - tests/domain/test_diff.py
---

# Domain — compute_changes (pure diff)

## Что сделано

- Реализована `compute_changes(old: Lot | None, new: Lot, tracked: Sequence[TrackedField]) -> list[FieldChange]`
  в `domain/diff.py` — чистая функция, без I/O, полностью детерминирована.
- `ALLOWED_TRACKED_FIELDS: frozenset[str]` деривируется из `TrackedField` Literal через
  `typing.get_args()` — единый источник правды, дрейф невозможен
  (см. [[decisions-log#ADR-022]]).
- Поля `auction` и `list_presence` (forward-compat, в TrackedField, но ещё без атрибута
  в `Lot`) выбрасывают `NotImplementedError` вместо `AttributeError` — fail-fast с
  явным сообщением не выходит за пределы транзакции.
- 19 тестов (1 skipped — hypothesis optional): INSERT-путь (`old=None`), no-change, дельта
  по каждому tracked-полю, `NULL→value`, `value→NULL`, порядок сохраняется,
  неизвестное поле → `ValueError`, симметрия, frozen-DTO инвариант.

## Почему так

- Caller передаёт только `tracked: Sequence[TrackedField]`; repo внутри BEGIN IMMEDIATE
  сам зовёт `compute_changes` — закрывает TOCTOU window между `SELECT old` и `UPDATE`
  [[decisions-log#ADR-016]] (R3-C2).
- Чистая функция в `domain/` тестируема без БД; infra-слой (repo) вправе импортировать
  domain (DIP, import-linter контракт [[decisions-log#ADR-006]]).
- `typing.get_args(TrackedField)` для SSOT `ALLOWED_TRACKED_FIELDS` — runtime defence-in-depth
  поверх type-level Literal; SQL-identifier-инъекция исключена на обоих уровнях.

## Связи

- Закрывает: `bd #gektar_monitor-531.3`
- Связано: [[gektar_monitor-531.1]], [[decisions-log#ADR-016]], [[data-model#FieldChange--LotUpsertResult]]
- Новые термины: [[glossary#compute_changes]], [[glossary#TrackedField]], [[glossary#FieldChange--LotUpsertResult]]

## Follow-up

- Разблокирован: `gektar_monitor-akv.5` (LotRepository.upsert + `_sync_geo` под BEGIN IMMEDIATE).
