# ADR-040 — EGRN Registration Date as Separate Field

**Status:** Accepted  
**Date:** 2026-05-17  
**Task:** `gektar_monitor-svqi`

## Context

The FIS site exposes two distinct date concepts for a land lot:

1. **`date_create`** — «DATE_CREATE» from column 10 of `/cabinet/free-lot` list page. This is the timestamp when the lot record was created in the FIS database. Not related to EGRN.
2. **«Дата постановки на учет»** — from the `/cabinet/free-lot-view?id=N` detail page. This is the EGRN registration date (the date the parcel was registered in the Unified State Real Estate Register). Entirely different semantic meaning.

Previously `detail_parser.py` only extracted `date_update` («Дата изменения сведений в ЕГРН») from the detail page and silently ignored «Дата постановки на учет». Users had no way to see when the parcel was actually registered in EGRN.

## Decision

Add a new field `date_registry: datetime | None = None` to both `Lot` and `ParsedDetail` domain models. Parse «Дата постановки на учет» from the detail page in `SelectolaxDetailParser`. Display both dates in the lot poster card with distinct labels and tooltips.

**Option 3 was chosen** (user-clarified 2026-05-17): show both dates in the card simultaneously — FIS creation date and EGRN registration date — rather than replacing one with the other.

`date_registry` is **not added to `TrackedField`** — changes to EGRN registration date are not a business event worth tracking in `lots_history`.

## Alternatives Considered

- **Option 1 — Replace `date_create` display with `date_registry`**: rejected — `date_create` (FIS arrival date) is useful for freshness sorting and subscription cutoff logic (ADR-039). Removing it from the UI loses information.
- **Option 2 — Show only `date_registry`, drop FIS date from card**: same objection as Option 1.
- **Option 3 — Show both dates with labels**: chosen. Minimal user confusion; both data points are independently useful.
- **Drop entirely**: rejected — EGRN registration date is materially useful when evaluating a lot (indicates how long the parcel has been registered).

## Implementation

- Schema: `date_registry TIMESTAMP NULL` added to `lots` table; `user_version` bumped 4→5.
- Migration: `migrations_v4_to_v5.py` — idempotent `ALTER TABLE lots ADD COLUMN date_registry TIMESTAMP`. Runs inside the `SqliteMigrationRunner` BEGIN IMMEDIATE transaction.
- Parser: `SelectolaxDetailParser` extracts key `"Дата постановки на учет"` via `all_kv.get(...)` using the same `_parse_date` helper as `date_update`.
- Enrichment: `EnrichmentService._enrich_one` propagates `date_registry` from `ParsedDetail` to `Lot` using the same preserve-existing pattern as `date_update` — if `detail.date_registry is None` the current `lot.date_registry` is kept.
- Repository: `SELECT`, `INSERT`, `UPDATE` in `SqliteLotRepository` and `LotQueryService` include `date_registry`.
- View-model: `LotViewModel.registry_date_human` property — `%d.%m.%Y` format (date-only), returns `"—"` when `None`.
- Template: `_lot_poster.html.jinja` shows EGRN date segment only when `registry_date_human != "—"`.

## Consequences

- Positive: users see EGRN registration date directly in the lot card without opening the external site.
- Positive: `date_create` meaning is now unambiguous — it is the FIS DB creation date, not an EGRN date.
- Neutral: existing lots have `date_registry = NULL` until their detail page is re-fetched by `EnrichmentService`. The card gracefully hides the EGRN segment for such lots.
- Negative: one additional column in `lots` table; no performance impact at current scale.

## See Also

- [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]] — `date_create` used as subscription cutoff timestamp
- [[data-model/lot]] — `Lot` model with `date_registry`
- [[parser/cabinet-free-lot-view]] — detail page key mappings
- [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]] — migration transaction invariants
