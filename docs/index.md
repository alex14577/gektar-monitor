# Мониторинг ФИС «На Дальний Восток»

Карта знаний по проекту мониторинга свободных земельных лотов на сайте `xn--80aaggvgieoeoa2bo7l.xn--p1ai` (НаДальнийВосток.рф) — ФИС Росреестра для программы «Дальневосточный и Арктический гектар».

## Контекст задачи

Клиент хочет получать уведомления о появлении новых земельных лотов в [[parser/cabinet-free-lot|реестре кабинета]] под своей ЕСИА-сессией. Архитектура — **локальный веб-сервер с UI в браузере**, запускается на ПК клиента под Windows. Forward-compat с переездом на VPS в будущем.

## Главные решения

- ⭐ [[decisions-log]] — **финальный список зафиксированных решений** (читать первым)
- ⭐ [[product/mvp-scope]] — **что входит/не входит в MVP**
- ⭐ [[web/ui-architecture]] — архитектура веб-UI, FastAPI + HTMX
- ⭐ [[parser/sort-strategy]] — 1 запрос на цикл через `sort=-DATE_CREATE` + ранний выход
- ⭐ [[notifications]] — каналы уведомлений (плагин-архитектура)
- ⭐ [[product/monitoring-plan]] — защита от смены ID-схемы сайта-донора (anomaly detection)
- [[parser/local-catalog]] **(v2)** — отложено в v2 (архитектурно поддерживается)

## Технические заметки

- [[product/site-architecture]] — стек сайта, движок Yii2, поведение сервера
- [[web/authentication]] — авторизация через ЕСИА, минимальный набор cookies
- [[parser/cabinet-free-lot]] — структура страницы реестра, поля таблицы, фильтры
- [[parser/donor-site-urls]] — карта URL сайта-донора, отсутствие публичного API
- [[parser/anti-bot]] — защит нет
- [[ops/server-performance]] — измерения скорости, режим техработ

## Реализация

- [[ops/getting-started]] — Day 1: клонирование, venv, dev-сервер, ЕСИА-сессия
- [[project-structure]] — раскладка `src/fis_monitor/`, модули по слоям
- [[ops/runbook]] — что делать при авариях (8 сценариев)
- [[web/api-reference]] — единый список API-эндпоинтов
- [[config-reference]] — таблица всех ключей config.json
- [[glossary]] — словарь терминов: ЕСИА, ФИС, ВРИ, ПКК, lazy/mirror, …
- [[ops/dev-environment]] — разработка из Linux, Windows VM минимально
- [[ops/cost-estimate]] — оценка 150–180к ₽ / 7–9 дней

## Артефакты от дизайнера

Распакованный архив `monitor.zip` в `/home/alex/dev/gektar_monitor/claude-design/`. Готовые шаблоны Jinja2, CSS, JS — не редизайн, а финальные файлы под FastAPI + HTMX + SSE.

- `claude-design/HANDOFF.md` — инструкция бэкенду: что готово, что дописать, таблица эндпоинтов
- `claude-design/README.md` — контракты данных Jinja, карта SSE-событий, JS-API
- `claude-design/templates/` — feed + onboarding (4 шага)
- `claude-design/static/` — app.css + app.js
- `claude-design/Monitor - main feed.html`, `Monitor - onboarding.html` — demo для визуальной сверки
- `claude-design/Wireframes.html` — низкоточные варианты (history)

## Юридические/безопасность

- [[product/risks-legal]] — ЕСИА-риски, 152-ФЗ, что фиксировать в договоре

