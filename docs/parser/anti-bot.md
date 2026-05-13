# Анти-бот защиты

## Краткий вывод
**Защит практически нет.** Сайт — стандартный гос-сервис на Yii2 без коммерческих анти-скрейпинг решений.

## Что проверено
| Защита | Статус |
|---|---|
| Cloudflare / CF-Ray | ❌ нет |
| DDoS-Guard / Qrator / StormWall / Variti | ❌ нет |
| Imperva / Incapsula / Akamai | ❌ нет |
| reCAPTCHA / hCaptcha | ❌ нет |
| JS-challenge | ❌ нет |
| Browser fingerprinting (FingerprintJS, DataDome) | ❌ нет |
| User-Agent фильтрация | ❌ нет (отвечает даже на пустой UA и `python-requests`) |
| Rate-limit | ❌ не сработал на 5 запросов подряд |

## Что есть (стандартное, не мешает парсингу)
- `X-Frame-Options`, `X-XSS-Protection`, `X-Content-Type-Options` — заголовки против XSS/clickjacking
- CSRF на POST через `_csrf` cookie + hidden field (для парсинга GET-страниц не нужно)
- `browser_info` cookie — сервер парсит UA и кладёт в cookie (детект, не блок)
- ЕСИА на стороне Госуслуг — может потребовать SMS «новое устройство» при смене IP

## Ограничения от Госуслуг (важнее)
- ESIA_SESSION = 3 часа → каждые ~3 часа нужно проверять валидность сессии
- Смена IP/устройства → может потребоваться 2FA при следующем входе
- См. [[product/risks-legal]]

См. также: [[web/authentication]], [[product/site-architecture]].
