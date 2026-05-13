# 10.9 HTTP-логи — fields-whitelist

`requests.jsonl` пишет ТОЛЬКО разрешённые поля:
- `method`, `url_path` (без query), `status`, `duration_ms`, `bytes`, `parser_version`.

**Никогда**: `Cookie`, `Authorization`, `Set-Cookie`, request/response body.

**Query**: пишется только для whitelist-путей (`/cabinet/free-lot` со списочной выборкой `?page=N` — но без OAuth-параметров). Для логин-роутов query замаскирована как `?<redacted>`.

UTC ISO-время через `Clock.now().isoformat()`. **Никаких `DEFAULT CURRENT_TIMESTAMP`** в SQL — это инвариант: время в БД пишет код через `Clock` (тестируемость).
