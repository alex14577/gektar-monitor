# ADR-043: Non-loopback bind for WSL→Windows browser access

**Status:** Accepted — 2026-05-17 (bd `gektar_monitor-2131`)

## Context

Developers running the service under WSL2 need to access the UI from a Windows browser. On Windows 11, WSL2 enables `localhost` forwarding by default — the browser can already reach `http://localhost:8000/` without any bind-address change. This covers the overwhelming majority of cases.

The remaining rare cases are:

1. Older WSL versions / custom WSL configurations where localhost forwarding is disabled.
2. Accessing from another machine on the same LAN (e.g. mobile testing).

In both cases the service must bind to a non-loopback address. Without extending the CSRF allowlists, any state-changing request would receive **421 Misdirected Request** because `CsrfHostOriginMiddleware` enforced a loopback-only `host_allowlist` (ADR-011).

A secondary ergonomics gap existed: developers had to pass `--host` and `--port` on every launch. There was no env-var fallback.

## Decision

### A — Env-var CLI fallback

`FIS_MONITOR_HOST` (default `127.0.0.1`) and `FIS_MONITOR_PORT` (default `8000`) are read at startup. CLI flags take precedence over env vars; env vars take precedence over hardcoded defaults. No behaviour changes for users who do not set these vars.

### B — `csrf_config_for_bind(host, port)`

A new function `csrf_config_for_bind(*, host: str, port: int, _local_ipv4s: list[str] | None = None) -> tuple[frozenset[str], frozenset[str]]` replaces the inline call to `loopback_csrf_config` inside `create_app`.

Behaviour by `host` value:

| `host` | Host allowlist | Origin whitelist |
|--------|---------------|-----------------|
| `127.0.0.1` / `localhost` / `::1` | Loopback set only | Loopback origins only |
| `0.0.0.0` | Loopback + `0.0.0.0:<port>` + each detected non-loopback IPv4 via `socket.gethostbyname_ex` | Mirror with `http://` scheme |
| Specific non-loopback IP | Loopback + that IP | Mirror with `http://` scheme |

NIC-IP detection is best-effort: failure logs a warning and is swallowed. The `_local_ipv4s` parameter is a DI seam for unit tests (no real socket calls in tests).

`loopback_csrf_config` is kept as a thin backward-compatible wrapper delegating to `csrf_config_for_bind(host="127.0.0.1", port=port)`.

`create_app` gains a `host: str = "127.0.0.1"` parameter so the allowlist computation is co-located with ASGI wiring rather than scattered across `main()`.

### C — Startup warning

When `args.host` is not in `{"127.0.0.1", "localhost", "::1"}`, `main()` emits:

```
WARNING  Binding to non-loopback host <host> — exposes service on network.
         Use only in trusted dev environments.
         To revert: set FIS_MONITOR_HOST=127.0.0.1 or omit --host.
```

This surfaces at the bootstrap logging level (WARNING), which is active before the lifespan logging replacement fires, so it reaches `stderr` unconditionally.

## Alternatives considered

| Alternative | Verdict |
|-------------|---------|
| **Docs-only** (document WSL localhost-forwarding) | Insufficient when forwarding is off; silent 421 provides no actionable feedback |
| **Wildcard `*` in Host allowlist** | Rejected — defeats ADR-011 DNS-rebinding protection entirely |
| **Extend GET validation too** | Out of scope; see ADR-011 Addendum for rationale; a separate ADR if ever needed |
| **ngrok / dev tunnel** | Out of scope for a local dev tool; adds external dependency |
| **Automatic detection of WSL without user opt-in** | Fragile; better to keep explicit `--host` / env-var |

## Consequences

**Gained:**
- WSL→Windows access works via `FIS_MONITOR_HOST=0.0.0.0` or `--host 0.0.0.0`.
- No per-launch flag typing when env var is set.
- Non-loopback bind is always accompanied by a loud WARNING — the security boundary is explicit and visible.
- `create_app` is now testable with non-loopback configs without touching argparse.

**Preserved:**
- Default remains `127.0.0.1` — no behaviour change for existing users.
- ADR-011 DNS-rebinding protection is intact: allowlists are always explicit whitelists, never wildcards.
- Production guidance unchanged: deploy behind a reverse proxy, bind loopback, never expose directly on `0.0.0.0` in production.

**Risks:**
- NIC-IP detection via `socket.gethostbyname_ex` is not guaranteed on all WSL configurations. If it misses an IP, the user sees 421 and must add the IP manually (acceptable for a dev-only feature).

## See also

- [[decisions/ADR-011-dns-rebinding-host-allowlist|ADR-011]] — Host/Origin allowlist policy (extended by this ADR)
- `src/fis_monitor/web/middleware.py` — `csrf_config_for_bind` implementation
- `src/fis_monitor/app.py` — `create_app(host=...)` + `main()` warning
- `tests/unit/test_csrf_middleware.py` — unit tests for `csrf_config_for_bind`
