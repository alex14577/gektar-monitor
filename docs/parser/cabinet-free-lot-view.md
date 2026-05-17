# Детальная карточка лота (`/cabinet/free-lot-view`)

**URL:** `https://xn--80aaggvgieoeoa2bo7l.xn--p1ai/cabinet/free-lot-view?id={lot_id}`

`lot_id` — значение `data-key` атрибута строки таблицы из [[parser/cabinet-free-lot]]. Это внутренний ID ФИС, не кадастровый номер.

## Парсер

`SelectolaxDetailParser` (`infra/parsers/detail_parser.py`) — selectolax-based, stateless. Версия: `parser_version=1`.

Блок данных находится в `.request-declaration__block-main > .request-domain__key-value`. Каждая пара — `<div><div>Ключ</div><div>Значение</div></div>`.

## Ключевые поля (типизированные)

| Ключ на странице | Поле в `ParsedDetail` / `Lot` | Тип | Примечание |
|---|---|---|---|
| «Широта» | `lat` | `float \| None` | DMS → decimal, `infra/parsers/detail_parser.py::_dms_to_decimal` |
| «Долгота» | `lon` | `float \| None` | DMS → decimal |
| «Границы участка» | `has_boundaries` | `bool \| None` | «Есть» → True, «Нет» → False |
| **«Дата постановки на учет»** | `date_registry` | `datetime \| None` | ЕГРН-дата регистрации участка. `DD.MM.YYYY` → datetime UTC midnight. NULL до обогащения. ADR-040 |
| «Дата изменения сведений в ЕГРН» | `date_update` | `datetime \| None` | Последнее изменение записи в ЕГРН. `DD.MM.YYYY` → datetime UTC midnight. |

## Дата-семантика (важно)

| Поле | Источник | Значение |
|---|---|---|
| `date_create` | список `/cabinet/free-lot`, col 10 | Дата добавления лота в БД ФИС. **НЕ ЕГРН**. |
| `date_registry` | detail-страница, ключ «Дата постановки на учет» | Дата регистрации участка в ЕГРН. |
| `date_update` | detail-страница, ключ «Дата изменения сведений в ЕГРН» | Дата последнего обновления записи ЕГРН. |

Путаница между `date_create` и `date_registry` была источником бага, исправлённого в задаче `gektar_monitor-svqi`. Канонический ADR: [[decisions/ADR-040-egrn-registration-date|ADR-040]].

## Остальные поля

Все прочие пары ключ-значение складываются в `ParsedDetail.raw_json: dict[str, Any]` для forward-compatibility.

## Фикстуры

`tests/fixtures/detail_lot_9990.html` — реальная страница лота 9990. Содержит:
- «Дата постановки на учет»: «22.04.2026»
- «Дата изменения сведений в ЕГРН»: (пусто)
- «Границы участка»: «Есть»
- координаты в DMS

## См. также

- [[parser/cabinet-free-lot]] — список лотов (col 10 = `date_create`)
- [[decisions/ADR-040-egrn-registration-date|ADR-040]] — rationale разделения дат
- [[data-model/lot]] — доменная модель `Lot`
