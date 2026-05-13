# Архитектура сайта

## Стек
- **Бэкенд:** PHP, фреймворк **Yii2** (видно по структуре `/frontend/web/assets/...`, hidden field `_csrf`, шаблон URL `controller/action`, GridView, PJAX)
- **Фронтенд-роутинг:** classic **SSR**, всё рендерится сервером, отдаётся как HTML. SPA-частей нет.
- **HTTP:** редирект `http → https` (302). На цепочке два слоя: фронт (IIS-подобный, отвечает `Cache-Control: private`) + nginx (виден в заголовках на `/frontend/web/`).
- **SSL:** self-signed в цепочке → для curl/requests **обязательно** `-k` / `verify=False`.
- **JS-библиотеки:** jQuery 3.x, jQuery-UI, Bootstrap (старый), jsTree, Chosen.js. Никаких React/Vue.
- **Аналитика:** Яндекс.Метрика `yaCounter32379010`.
- **Заголовки безопасности:** `X-Frame-Options: SAMEORIGIN`, `X-XSS-Protection: 1; mode=block`, `X-Content-Type-Options: nosniff`. CSP отсутствует.

## Cookies, которые ставит сайт
| Cookie | Назначение | TTL |
|---|---|---|
| `PHPSESSID` | Сессия PHP | Session |
| `_csrf` | Yii2 anti-CSRF (signed + base64-PHP-serialized) | Session |
| `session-cookie` | Балансировщик/sticky-session (одинаков для всех) | 86400s |
| `JSESSIONID` | Java-сессия (отдельный backend-компонент, появляется после ЕСИА-логина) | Session |
| `browser_info` | Сервер сам парсит UA и кладёт в cookie | 7d |
| `closable_alert_was_read*` | Закрытые баннеры | Session |
| `_ym_*` | Метрика | до 1 года |

## Поведение
- На любой `/cabinet/*` без валидной сессии: 302 → `/default/login` → 302 → ЕСИА OAuth2.
- `/frontend/web/*` отдаёт 403 на listing — служебный путь Yii2.
- Ошибки приложения возвращают 500 со страницей «Ошибка N» (например, [[parser/cabinet-free-lot|«Ошибка 8»]] = отсутствует обязательный параметр `region`).

См. также: [[web/authentication]], [[parser/donor-site-urls]].
