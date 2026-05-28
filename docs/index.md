# Мониторинг ФИС «На Дальний Восток»

Карта знаний по проекту мониторинга свободных земельных лотов на сайте `xn--80aaggvgieoeoa2bo7l.xn--p1ai` (НаДальнийВосток.рф) — ФИС Росреестра для программы «Дальневосточный и Арктический гектар».

## Контекст задачи

Пользователь хочет получать уведомления о появлении новых земельных лотов в [[parser/cabinet-free-lot|реестре кабинета]] под своей ЕСИА-сессией. Архитектура — **локальный веб-сервер с UI в браузере**, запускается на ПК пользователя под Windows. Forward-compat с переездом на VPS в будущем.

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
- [[parser/pkk-deeplink]] — deep-link на кадастровую карту по кадастровому номеру (ПКК Росреестра + зеркало roscadastres.com)
- [[parser/anti-bot]] — защит нет
- [[ops/server-performance]] — измерения скорости, режим техработ

## Подсистемы

- [[licensing/index|Licensing system]] — активационные ключи, HMAC-SHA256, stateless offline-верификация

## Реализация

- [[ops/getting-started]] — Day 1: клонирование, venv, dev-сервер, ЕСИА-сессия
- [[wave-plan]] — **план распараллеливания bd-issues по волнам до MVP + session log**
- [[project-structure]] — раскладка `src/fis_monitor/`, модули по слоям
- [[ops/runbook]] — что делать при авариях (8 сценариев)
- [[web/api-reference]] — единый список API-эндпоинтов
- [[config-reference]] — таблица всех ключей config.json
- [[glossary]] — словарь терминов: ЕСИА, ФИС, ВРИ, ПКК, lazy/mirror, …
- [[ops/dev-environment]] — разработка из Linux, Windows VM минимально
- [[ops/browser-feedback-mcp]] — Playwright + Chrome DevTools MCP для визуальной обратной связи Claude Code
- [[ops/cost-estimate]] — оценка 150–180к ₽ / 7–9 дней

## Юридические/безопасность

- [[product/risks-legal]] — ЕСИА-риски, 152-ФЗ, что фиксировать в договоре

