# Среда разработки

## Главное: Windows VM больше не на критическом пути

С переездом на веб-UI ([[web/ui-architecture]]) **разработка ведётся целиком на Linux**. VM нужна только для финальной интеграционной проверки и сборки `.exe`.

## Что где делаем

### На Linux (95% времени)
- Бэкенд FastAPI: парсер, цикл, БД, API-эндпоинты
- Фронт HTMX/Jinja2: HTML-шаблоны, CSS, минимум JS
- Юнит-тесты pytest (без сети, на фикстурах)
- Интеграционные тесты HTTP API (httpx + uvicorn TestClient)
- Запуск приложения локально — `uvicorn main:app --reload`, открываем в любом Linux-браузере
- Playwright под Linux — для отладки логин-флоу (тот же Chromium API)

### В Windows VM (5% времени, только для интеграции)
- Финальная проверка: ЕСИА-логин через Playwright под Windows
- Установка Task Scheduler (автозапуск)
- Проверка автооткрытия браузера на старте
- Проверка работы Notification API в Edge/Chrome под Windows
- Тестирование «правый клик → Unblock» сценария SmartScreen

### На GitHub Actions (CI)
- Pytest на Linux + Windows runners
- Сборка `.exe` через Nuitka на `windows-latest`
- Артефакт скачивается ссылкой

## Установка Windows VM (один раз)

1. **VirtualBox** — `apt install virtualbox`
2. **Windows 11 Dev VM** с developer.microsoft.com → бесплатный 90-дневный образ
3. Импорт `.ova`, 4 ГБ RAM, 2 CPU
4. Shared Folder на проект — внутри: Python 3.12, `pip install -r requirements.txt`, `playwright install chromium`

## Кросс-платформенные правила в коде

- **Пути**: `pathlib.Path`, никаких `os.path.join`
- **Файлы**: всегда `encoding='utf-8'`
- **Конец строки**: `newline=''` для CSV
- **Платформо-специфичное** (если будет — PowerShell-фолбэк, Task Scheduler регистрация) — за `if sys.platform == 'win32':`
- **Маркер тестов**: `@pytest.mark.windows` — гоняется только в windows-CI

## Развёртывание на хостинге в будущем

Веб-UI открывает возможность переезда на VPS:

- **Сейчас (MVP)**: `localhost:8080` на ПК пользователя, его cookies в его `profile/`.
- **Потом (v2/SaaS)**: тот же FastAPI, но за nginx + HTTPS, мульти-юзер, изолированные профили в БД.

Разработка на Linux уже подготавливает к этому — пишем код так, чтобы он работал и в обоих режимах (с переменной окружения `MODE=local | server`).

См. также: [[web/ui-architecture]], [[decisions-log]].
