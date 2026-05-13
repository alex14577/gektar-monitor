# ADR-016: Repository invariants — BEGIN IMMEDIATE + identifier whitelist + private _sync_geo

**Context.** Первая версия `LotRepository.upsert(lot, *, tracked_fields)` нарушала SRP: репозиторий вычислял diff (нормализация status casing, datetime-precision) — это бизнес-правило, не CRUD. Параметр `tracked_fields: Sequence[str]` потенциально — vector identifier-инъекции (имя поля в SQL не параметризуется). `sync_geo` был публичным методом Protocol → утечка инварианта «вызывать только внутри upsert-tx».

**Decision.**
1. **Вариант A: caller считает diff** (см. [[architecture/03-protocols]] §3.1). `LotRepository.upsert(lot, *, changes: list[FieldChange])` — repo принимает уже готовый список. `compute_changes()` и `normalize_for_diff()` — чистые функции в `domain/diff.py`.
2. **`BEGIN IMMEDIATE`** — обязательный для всех read-then-write (`upsert`, `mark_inactive`, `set_last_known_id`). Захватывает writer-lock до первого SELECT.
3. **Identifier whitelist**: `FieldChange.field: Literal[...]` ограничивает на типе; `ALLOWED_TRACKED_FIELDS` frozenset — defence-in-depth runtime check.
4. **`_sync_geo` приватный**: из публичного Protocol убран, зовётся только внутри `upsert`. Будущий публичный `update_geo` — отдельный метод с BEGIN IMMEDIATE.

**Consequences.** Repo стал тонким CRUD + tx-invariant. Diff-политика тестируется без БД. Identifier-инъекции исключены на типах. R-tree consistency гарантирована (нет внешнего пути менять lat/lon без _sync_geo).

**Расширение R3-C2 (`compute_changes` зовётся repo внутри tx).** Caller-stage `get(id)` + `compute_changes(old, new)` + `upsert(new, changes=changes)` имел silent data-corruption window: между `get` (no-tx) и `upsert` (BEGIN IMMEDIATE) другой writer мог изменить ту же строку — `upsert` писал в `lots_history` фантомный `old_value`. Решение: caller передаёт **только** `tracked: Sequence[TrackedField]`; repo внутри своей BEGIN IMMEDIATE tx делает `SELECT old`, импортирует чистую domain-функцию `compute_changes`, вычисляет diff, пишет историю. `compute_changes` остаётся в `domain/diff.py` (testable in-memory без БД). Импорт infra→domain валиден (DIP — domain независим от infra; infra легально использует domain как библиотеку). Двойной SELECT устранён (`was_new` возвращается в `LotUpsertResult`). SRP: domain — «как считать diff»; infra — «выполнить diff атомарно в tx и записать историю»; caller — «дать новый лот».

**Расширение R3-M8 (`_sync_geo` для всех переходов lat/lon).** `_sync_geo` зовётся при любом изменении координат, включая `value→NULL` (DELETE FROM lots_rtree) и `NULL→value` (INSERT). Если оба NULL — `DELETE FROM lots_rtree WHERE id=?`. Integration-тест покрывает: `NULL→value`, `value→NULL`, `value→value'`, no-change (R-tree row не трогается).

См. также: [[decisions-log]], [[architecture/03-protocols]] §3.1, [[data-model/lot]].
