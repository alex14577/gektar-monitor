# ADR-055 — region_id Canonical Namespace: site-id, not macro-id

**Status**: Accepted
**Date**: 2026-05-19
**Deciders**: Backend
**Tags**: region_id, site-id, macro-id, filter, pc1g
**Fixes**: bug pc1g (lots.region_id stored macro-id; rf_subjects holds site-ids → filter never matched)

---

## Context

`Settings.filters.rf_subjects` stores **site-ids** — keys of `SUBJECT_TITLE_BY_ID`
(e.g. 27 = Республика Карелия). `RfSubjectFilterMatcher` does
`lot.region_id in filters.rf_subjects`.

Prior to this ADR, the parser set `lot.region_id = region` where `region` is
the macro-region loop variable (1=ДФО, 2=Арктика). These are different namespaces:
a lot from Карелия got `region_id=2` (Арктика macro-id), but the filter looked for
27 (Карелия site-id) — they never intersect, so email was silently suppressed for
all lots regardless of `rf_subjects` configuration.

---

## Decision

`lots.region_id` stores a **site-id** (`∈ keys(SUBJECT_TITLE_BY_ID)`) or `NULL`.
Macro-region IDs (1=ДФО, 2=Арктика) are only used in the fetch-scope URL parameter
and `SUBJECTS_BY_MACRO` — they do not appear in `lots.region_id`.

**Implementation**:
1. `domain/regions.py` — new helper `subject_id_by_title(title: str | None) -> int | None`
   inverts `SUBJECT_TITLE_BY_ID` via a cached dict. Strict match, no normalization.
2. `monitor_cycle.py` + `backfill.py` — call `subject_id_by_title(row.region)` at ingest
   time instead of passing the macro-id directly.
3. Migration v7→v8 — backfills existing rows from macro-id to site-id using the same
   inverted catalog. Unconditional UPDATE per distinct `region` text (idempotent).

---

## Why inverse-map in domain, not in parser/infra

`parsed_row_to_lot` is a domain function. The resolve step converts a raw HTML string
to a domain identifier — it belongs in the domain layer alongside the catalog it consults.
Keeping it in domain also means backfill (infra) and monitor (service) share one path.

---

## Consequences

- Email notifications now correctly filter by RF-subject. Lots from "Республика Карелия"
  (site-id 27) pass filter when `rf_subjects=[27]`.
- `region_id=None` (unknown region text) triggers fail-open per ADR-035 I2.
- `LATEST_SCHEMA_VERSION` bumped 7→8.
- Tests updated: `TestRegionIdStamping` now asserts site-id (27 for Карелия) or None
  for unknown names.

---

## Alternatives Considered

### 1. Resolve macro-id → site-ids at query time in `RfSubjectFilterMatcher`

Instead of storing site-id at ingest, keep macro-id in `lots.region_id` and expand the filter's `rf_subjects` set to include all site-ids for the matching macro-region on each filter evaluation.

**Rejected**: `RfSubjectFilterMatcher.matches()` is called every monitor cycle × every lot in the result page. Computing the inverse map and expanding sets on every call adds overhead that scales with lot volume. Ingest-time resolution pays the cost once per lot and stores the correct identifier directly — O(1) lookup at filter time.

### 2. Store both `region_macro_id` and `region_site_id` as separate columns

Preserve the macro-id for display/grouping purposes alongside the site-id used for filtering.

**Rejected**: Redundant storage risks drift (a migration updates one column but not the other). The macro-id is derivable from `SUBJECTS_BY_MACRO` if ever needed for display — it does not need to be persisted. Adding a column to `lots` widens the schema and every INSERT/SELECT unnecessarily.

---

## References

- [[decisions/ADR-035-three-scope-filter-model|ADR-035]] §I2 — filter invariant
- `src/fis_monitor/domain/regions.py` — `subject_id_by_title`
- `src/fis_monitor/infra/sqlite/migrations_v7_to_v8.py` — backfill migration
- [[glossary#site-id]], [[glossary#macro-id]]
