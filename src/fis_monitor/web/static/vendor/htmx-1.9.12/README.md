# Vendored: htmx 1.9.12

Vendored on 2026-05-15 as part of supply-chain mitigation (bd: gektar_monitor-mi8 / ADR-029).

## Source URLs

- `htmx.min.js`: https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js
- `ext/sse.js`: https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js

## Version

htmx 1.9.12

## SHA256 checksums

```
449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452  htmx.min.js
be05b2e2265279f035271adbea0b72a356f20ce4dfa5870481bfe9c51b822fc1  ext/sse.js
```

## Upgrade procedure

1. Download new version to `static/vendor/htmx-<NEW_VERSION>/`
2. Compute SHA256 sums and update this README
3. Update `base.html.jinja` script src paths
4. Run `pytest` + smoke test the UI
5. Delete old version directory once tests pass
