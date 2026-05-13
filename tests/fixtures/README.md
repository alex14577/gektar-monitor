# Test Fixtures — HTML с сайта НаДальнийВосток.рф

Реальные снимки HTML, полученные в разведке 12.05.2026 под авторизованной сессией.

## Файлы

| Файл | Что | URL | Размер |
|---|---|---|---|
| `homepage_anonymous.html` | Главная без авторизации | `/` | 548 КБ |
| `error_8_no_region.html` | Ошибка 8 (нет параметра region) | `/cabinet/free-lot` | 9 КБ |
| `cabinet_profile.html` | Профиль пользователя ЛК | `/cabinet/profile` | 53 КБ |
| `list_region1_perpage50.html` | Список лотов (дефолт) | `/cabinet/free-lot?region=1&per-page=50` | 268 КБ |
| `list_region1_sorted_desc_create.html` | Список с сортировкой по дате создания DESC | `/cabinet/free-lot?region=1&per-page=50&sort=-DATE_CREATE` | 370 КБ |
| `detail_lot_9990.html` | Детальная карточка лота | `/cabinet/free-lot-view?id=9990` | 32 КБ |

## Использование

### Юнит-тесты парсера
```python
def test_list_parser():
    html = Path("tests/fixtures/list_region1_sorted_desc_create.html").read_text()
    lots = parse_lot_list(html)
    assert len(lots) == 50
    assert lots[0].id == 9990
    assert lots[0].cadastral_no == "79:06:2701002:287"
```

### Юнит-тесты детальной карточки
```python
def test_detail_parser():
    html = Path("tests/fixtures/detail_lot_9990.html").read_text()
    lot = parse_lot_detail(html)
    assert lot.lat == pytest.approx(48.5558, abs=0.001)
    assert lot.lon == pytest.approx(134.9539, abs=0.001)
    assert lot.has_boundaries == True
```

### Регрессия при обновлении парсера
При выпуске новой версии парсера — прогон ВСЕХ фикстур должен давать те же выходные значения. Если что-то изменилось — либо парсер сломан, либо мы намеренно добавили новое поле (зафиксировать в CHANGELOG).

## Когда обновлять

Только если сайт меняет структуру HTML. Тогда:
1. Получаем свежие HTML (от живого клиента или своей разведкой)
2. Переписываем фикстуры
3. Прогоняем тесты — должны падать на тех местах, что поменялось
4. Обновляем парсер до прохождения
5. Бампаем `parser_version`
