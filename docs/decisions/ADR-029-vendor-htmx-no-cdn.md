# ADR-029 — Vendor htmx locally; no CDN for JS assets

**Date:** 2026-05-15
**Status:** Accepted
**Context:** bd gektar_monitor-mi8 (supply-chain mitigation F-03)

---

## Context

`base.html.jinja` loaded htmx 1.9.12 and its SSE extension directly from
`unpkg.com` — an open CDN that any registered npm publisher can push to.
A compromised or typo-squatted `htmx.org` package on npm would silently execute
arbitrary JS in every user's browser session (supply-chain risk F-03).

The app is a local desktop tool served on 127.0.0.1; it has no CDN caching
benefit and full control over its own static files via FastAPI `StaticFiles`.

## Decision

Vendor htmx JS assets locally under
`src/fis_monitor/web/static/vendor/htmx-<version>/`.

- `htmx.min.js` — minified core
- `ext/sse.js` — SSE extension

`base.html.jinja` uses `url_for('static', path='/vendor/htmx-1.9.12/...')` —
no external network request at page load.

A `README.md` inside the vendor directory records: source URLs, download date,
version, and SHA256 checksums for integrity verification.

PyInstaller spec (`build/fis-monitor.spec` line 37) already bundles the entire
`web/static` tree — no spec change required.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **CDN + SRI `integrity=`** | Mitigates tampered delivery, but not a compromised upstream package on npm. CDN still has internet dependency; desktop app has no latency benefit. |
| **Keep CDN, add monitoring** | Operational overhead; doesn't eliminate the attack surface. |
| **Build-time download in CI** | More complex; vendor directory is simpler and auditable in git. |

## Consequences

**Positive:**
- Zero external JS at runtime — works fully offline.
- Eliminates CDN supply-chain risk (F-03) for JS execution.
- Checksums in README make integrity auditable without tooling.
- PyInstaller bundle unaffected (already includes full `static/` tree).

**Negative / trade-offs:**
- Manual upgrade burden: new htmx version → download → update README + SHA256 →
  update `base.html.jinja` → run tests. Mitigated by versioned folder convention
  (`htmx-<version>/`) and documented procedure in `vendor/htmx-1.9.12/README.md`.
- Adds ~60 KB to the repo and PyInstaller bundle (acceptable).

## Scope note

Google Fonts (`<link>` elements, lines 17–20) remain CDN-hosted — fonts carry
no JS execution risk and vendoring web fonts would add several MB. This ADR
covers JS assets only.
