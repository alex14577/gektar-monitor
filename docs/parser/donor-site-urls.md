# URL-карта сайта-донора

> URL-карта сайта-донора (НаДальнийВосток.рф), **НЕ наш HTTP API**.
> Наш API — [[web/api-reference]].

## Публичные страницы (без авторизации)
| URL | Содержание |
|---|---|
| `/` | Главная (~548 KB) |
| `/business-plan` | Каталог бизнес-планов |
| `/business-plan/detail?id=N` | Карточка БП |
| `/best-practice` | Лучшие практики |
| `/best-practice/view?id=N` | Карточка практики |
| `/best-practice/map?id=N&practiceId=M` | Карта практики |
| `/support-measure` | Меры поддержки |
| `/news` | Новости |
| `/npa` | НПА |
| `/faq` | Вопрос-ответ |
| `/default/login` | Запуск ЕСИА-логина |
| `/default/maininfo` | Информация |
| `/robots.txt` | `User-agent: *  Allow: /` |
| `/sitemap.xml` | **отсутствует** (404) |

## Кабинет (под ЕСИА)
| URL | Содержание |
|---|---|
| `/cabinet/profile` | Профиль пользователя |
| `/cabinet/free-lot?region=N` | [[parser/cabinet-free-lot|Реестр свободных лотов]] |
| `/cabinet/free-lot-view?id=N` | Карточка лота |
| `/cabinet/statement` | Заявления (просмотр/создание) |
| `/cabinet/statements-history` | История заявлений |
| `/cabinet/children` | Дети (для семейного гектара) |
| `/cabinet/delegate` | Делегирование |
| `/cabinet/log-entry-list` | Журнал действий |
| `/cabinet/notifications` | Уведомления |
| `/cabinet/notification-read-all` | Пометить все прочитанными |
| `/cabinet/system-news-list` | Системные новости |
| `/cabinet/system-faq-list` | Системный FAQ |

## Служебные AJAX
- `/default/read-closable-alert` — закрыть баннер (POST с CSRF)
- `/feedback/send` — форма обратной связи

## API
**Публичного REST/JSON API нет.** Проверено:
- `/api`, `/api/v1`, `/api/v2`, `/rest`, `/graphql`, `/swagger`, `/swagger.json`, `/api-docs`, `/openapi.json` — все 404
- В HTML страниц нет вызовов `fetch()`, `$.ajax({url:...})`, axios — только PJAX и единичные служебные хуки
- В ЛК (`/cabinet/*`) тоже нет видимых API-токенов или JSON-эндпоинтов

**Вывод:** парсить можно только HTML страниц. См. [[product/monitoring-plan]].
