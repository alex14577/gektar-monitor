# Browser-feedback MCP в Claude Code

Два MCP-сервера дают Claude Code «глаза» — возможность открыть страницу,
которую он только что написал, и сверить результат с тем, что хотел сделать.

Связано: [[ops/dev-environment]], [[web/ui-architecture]], [[agent-conventions]].

## Что установлено

| Сервер | Scope | Команда |
|---|---|---|
| `playwright` | project (`.claude.json` для gektar-monitor) | `npx @playwright/mcp@latest --browser chromium --headless --isolated` |
| `chrome-devtools` | user (глобально для всех проектов) | `npx chrome-devtools-mcp@latest --executablePath <chromium> --headless --isolated` |

Проверка: `claude mcp list` — оба должны быть в состоянии `✓ Connected`.

## Требования к среде

- **Node.js LTS** (≥ 20). Через `nvm`:
  ```bash
  nvm install --lts && nvm use --lts
  ```
- **Важно**: в обоих MCP-конфигах `env` обязан содержать `PATH` с
  nvm-директорией:
  ```json
  "env": { "PATH": "/home/alex/.nvm/versions/node/v24.15.0/bin:/usr/local/bin:/usr/bin:/bin" }
  ```
  Иначе `npx` падает на shebang `#!/usr/bin/env node` («node: No such file
  or directory» → MCP error -32000). Это происходит, когда сессия Claude
  Code запущена не из login-shell (PATH не унаследован).
- Для Playwright MCP **обязательны флаги** `--browser chromium --headless --isolated`,
  иначе сервер ищет системный Chrome в `/opt/google/chrome/chrome` и падает на WSL.
  Bundled Chromium у Playwright уже скачан (`~/.cache/ms-playwright/chromium-1208/`).
- Для Chrome DevTools MCP: бинарь Chrome/Chromium. На WSL2 без системного
  Chrome используется Chromium от Playwright:
  `~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome` (Chrome
  for Testing 145+). Указывается флагом `--executablePath` при `claude mcp add`.
- WSL2: оба сервера работают в headless-режиме без X-сервера.

## Когда какой использовать

### Playwright MCP — дефолт

Использовать **в 90% случаев**: проверка HTMX/Jinja-шаблонов, верстки,
наличия элементов, текста, состояний.

**Сильные стороны:**
- Возвращает **accessibility-tree** (ARIA-снимок) — структурированный
  текст с ролями, именами, состояниями. ~200–400 токенов на снимок.
- Детерминированное взаимодействие через ARIA-роли, не координаты.
- Дешёво по токенам — можно итерировать «правка → snapshot → правка».
- Кроссбраузерный (Chromium/Firefox/WebKit).

**Когда брать:**
- Проверить, что новый шаблон рендерит нужные элементы.
- Убедиться, что HTMX-fragment вставился в правильное место.
- Проверить состояние кнопки/формы/счётчика.
- Регрессионный smoke новой страницы.

**Когда НЕ хватит:**
- CSS-баги: цвет, отступы в пикселях, шрифт, перекрытие элементов.
- Layout-shift, overflow, неправильный z-index.

### Chrome DevTools MCP — для сложных кейсов

Использовать **точечно**, когда Playwright snapshot не покрывает задачу.

**Сильные стороны:**
- `take_screenshot` — настоящий PNG скриншот (image content block).
- `performance_start_trace` / `performance_analyze_insight` —
  Lighthouse-style trace, Core Web Vitals.
- `list_network_requests`, `get_console_message` — сетевые запросы и
  ошибки JS в реальном времени.
- `lighthouse_audit` — аудит perf / Core Web Vitals (a11y-секция — информативно; AT вне scope, см. [[decisions/ADR-061-assistive-tech-out-of-scope|ADR-061]]).
- Подключение к **уже открытому** Chrome (авторизованные сессии).
- Memory heap snapshots, extensions API.

**Когда брать:**
- Визуальный CSS-баг: «карточка лота поехала вправо», «цена налезает на
  статус» — нужны пиксели.
- Performance-регрессия: «страница `/lots` тормозит, найди узкое место».
- Проверить, что HTMX-запрос ушёл с правильными заголовками.
- Поймать JS-ошибку в консоли после Alpine-инициализации.
- Perf/Lighthouse-аудит перед релизом UI-фичи (a11y/AT — вне scope, [[decisions/ADR-061-assistive-tech-out-of-scope|ADR-061]]).

**Когда НЕ брать:**
- Простая проверка «отрисовалось ли» — Playwright snapshot дешевле.
- Не нужно подключаться к авторизованной сессии — Playwright проще.

## Стоимость по токенам (важно)

| Что | Токены на вызов |
|---|---|
| Playwright `browser_snapshot` (ARIA-tree) | ~200–400 |
| Playwright `--vision` screenshot | ~1 500–5 000 |
| Chrome DevTools `take_screenshot` | ~1 500–5 000 |
| Chrome DevTools `lighthouse_audit` | ~5 000–15 000 |
| Полный тест с серией скриншотов | до ~114 000 |

**Правило:** дефолт — `browser_snapshot`. Скриншоты — только когда
снимок ARIA-дерева недостаточен.

## Workflow для оркестратора Claude Code

Промпт sub-agent-у должен **явно** называть инструмент, иначе агент
может запустить Playwright через Bash и пропустить feedback-петлю.

**Шаблон промпта для writer-агента (UI-фича):**

```
1. Внеси правки в templates/<file>.html.
2. Запусти uvicorn (если не запущен): uvicorn main:app --reload.
3. Через Playwright MCP:
   - browser_navigate to http://127.0.0.1:8000/<path>
   - browser_snapshot
   - Сверь ARIA-дерево с acceptance: <список инвариантов>
4. Если расхождение → правка → повтор snapshot.
5. Если визуальный баг (CSS) → переключись на Chrome DevTools MCP:
   - take_screenshot
   - проанализируй и фикси
6. Финальный snapshot для подтверждения.
```

**Шаблон для reviewer-агента (визуальный review):**

```
Через Chrome DevTools MCP открой http://127.0.0.1:8000/<path>,
сделай take_screenshot, проверь:
- layout согласно макету в docs/web/<file>;
- console errors отсутствуют (get_console_message);
- network requests без 4xx/5xx (list_network_requests).
Верни список замечаний с blocker/major/minor.
```

## Ограничения

- **WSL2 + headless только.** Headed-режим требует X-сервера (WSLg).
  Обычно не нужен — модель видит через snapshot/screenshot.
- **Большие скриншоты могут падать с API-ошибкой** (Claude Code
  issue #9049): держать viewport ≤ 1280×800.
- **Не путать с official Chrome Integration** (`claude --chrome`) —
  тот работает только на нативном Chrome через extension и **не
  поддерживается на WSL/Linux**. Поэтому здесь — сторонние MCP.
- **Computer Use** от Anthropic — только macOS, не наш случай.

## Удаление

```bash
claude mcp remove playwright          # из project-scope
claude mcp remove chrome-devtools --scope user
```

## Источники и обновления

- Playwright MCP: https://github.com/microsoft/playwright-mcp
- Chrome DevTools MCP: https://github.com/ChromeDevTools/chrome-devtools-mcp
- Обновлять пакеты не нужно — `npx @latest` тянет свежую версию при
  каждом запуске.
