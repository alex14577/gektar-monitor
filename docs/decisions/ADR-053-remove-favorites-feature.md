# ADR-053 — Remove Favorites Feature (Избранное / starred)

**Status**: Accepted
**Date**: 2026-05-18
**Deciders**: Product (gektar_monitor-qhw8)
**Tags**: chore, favorites, starred, view-filters, lot-user-state, migration

---

## Context

The «Избранное» (starred / favorites) feature allowed users to mark lots with a star
and filter the feed by `only_stars=True`. The feature was implemented across:

- `LotUserState.starred` field (domain model)
- `LotUserDTO.starred` field (presentation DTO)
- `lot_user_state.starred` SQLite column + `idx_lus_starred` partial index
- `UserStateRepository.set_starred()` Protocol method
- `LotUserStateService.toggle_star()` service method
- `POST /lots/{lot_id}/star` HTTP endpoint
- `ViewFilters.only_stars` cookie field
- SSE predicate: `only_stars=True` → suppress all `lot.new` events
- UI: star button in `_lot_details.html.jinja`, `.starbtn` CSS, context-menu «В избранное»
- App.js: star toggle handler, `isStarred` context-menu variable

As of 2026-05-18, the product decision was made to remove this feature.
It was not actively used and added maintenance surface without product value.

---

## Decision

Remove the starred / favorites feature in its entirety — no legacy shims,
no deprecation wrappers, no backward-compatibility layer.

1. **Domain**: remove `starred: bool` from `LotUserState` and `LotUserDTO`.
2. **Protocol**: remove `set_starred()` from `UserStateRepository` Protocol.
3. **Service**: remove `toggle_star()` from `LotUserStateService`.
4. **Service**: remove `only_stars` from `ViewFilters` and all serialization paths.
5. **SSE predicate**: remove `only_stars=True` → always-suppress branch from
   `make_sse_view_filter`; update `_is_default` accordingly.
6. **Web route**: remove `POST /lots/{lot_id}/star` endpoint.
7. **Web route**: remove `only_stars` form parameter from `POST /filters/view`.
8. **Feed context**: remove `only_stars` post-filter from `_assemble_feed_zones`
   and `_build_filters_context`.
9. **SSE encoder**: remove `is_starred` property from `LotViewModel` and
   `LotUserViewModel`.
10. **Templates + static**: remove star button, `.starbtn` CSS, JS handlers.
11. **SQLite migration v5→v6**: `DROP INDEX IF EXISTS idx_lus_starred` +
    `ALTER TABLE lot_user_state DROP COLUMN starred`.
12. **Schema**: `PRAGMA user_version = 6`; `docs/db/schema.sql` reflects new shape.

---

## Consequences

- The `starred` column is permanently removed from `lot_user_state`. Existing
  databases are migrated via v5→v6 migration (DROP COLUMN — SQLite 3.35+ required).
- No UI affordance for starring remains in the application.
- No SSE filtering by starred state remains.
- `ViewFilters` cookies that previously contained `only_stars: true` will be
  silently ignored on deserialization (`extra="ignore"` in Pydantic model).
- `LATEST_SCHEMA_VERSION` bumped to 6 in `infra/sqlite/init_db.py`.
- `composition.py` updated to pass `latest_version=6`.

---

## Alternatives Considered

| Option | Reason rejected |
|--------|----------------|
| **Deprecate with warning, remove in v2** | Single-user local app — no API consumers to migrate. Clean removal is simpler and cheaper. |
| **Keep column as dead weight, hide UI only** | Dead columns accumulate; migration cost is low now (small table). Removing is cleaner. |

---

## References

- [[decisions/ADR-052-sse-view-filter-propagation|ADR-052]] — amended: `only_stars` special-case removed (N/A post-qhw8)
- [[glossary#SqliteUserStateRepository]] — updated
- [[glossary#LotPublicDTO vs LotUserDTO]] — updated
- [[data-model/lot]] — `LotUserDTO.starred` removed
- `src/fis_monitor/infra/sqlite/migrations_v5_to_v6.py` — DROP COLUMN migration
- Issue: gektar_monitor-qhw8
