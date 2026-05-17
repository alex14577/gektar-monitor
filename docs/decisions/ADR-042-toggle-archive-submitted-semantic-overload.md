# ADR-042: toggle_archive — semantic overload of `submitted` flag

## Status
Accepted

## Context

`LotUserState` (domain model) and `UserStateRepository` (Protocol) expose a
`submitted: bool` field backed by the `lot_user_state.submitted` SQLite column.
This column was originally introduced to track whether a user had submitted a
lot to a deal. In practice the UX never used "submitted" as a user-visible
concept — the only boolean flag reachable from the UI is **"Архив"** (archive).

`LotUserStateService.toggle_archive` uses `UserStateRepository.set_submitted`
to persist the archive state, creating an implicit aliasing:

```
UX semantic:        "lot is archived"
Storage column:     submitted = True
```

There is no dedicated `archived` column; `submitted` carries double duty.

This was discovered while reviewing bd `gektar_monitor-41sb`. No runtime bug
exists: the aliasing is internally consistent within the service layer and is
invisible to consumers of `LotUserDTO` (which exposes `submitted` under its own
name, not surfaced to the front-end template that uses the `archived` action).

## Decision

**Option A — accept overload, document, no schema change** (chosen).

Keep `toggle_archive` as the UX-facing name. Keep `set_submitted` as the
storage-layer call inside `toggle_archive`. Add inline documentation (docstring
+ ADR link) to make the aliasing explicit. Add a glossary entry.

Rationale:
- Task priority is P4 (semantic debt, no functional defect).
- Introducing a proper `archived` column (Option B) requires a schema migration
  (LATEST_SCHEMA_VERSION + 1), a new `UserStateRepository.set_archived` method,
  and a migration shim — disproportionate to the risk.
- Renaming the method to `toggle_submitted` (Option C) breaks UX language ("Архив"
  button) and conflicts with the web route `/lots/{lot_id}/archive`.
- The overload is fully contained within `LotUserStateService.toggle_archive`;
  callers of `UserStateRepository` outside that method always pass
  business-meaningful values.

## Alternatives

**Option B — add `archived` boolean column** (deferred).

Split the two semantics at the storage level. Trigger: when a second consumer
needs `submitted` in its original sense (e.g. deal-flow integration), making the
overload ambiguous at the query level. Until then, the complexity cost is not
justified.

**Option C — rename to `toggle_submitted`** (rejected).

Leaks storage semantics into UX vocabulary. The route is `/archive`, the button
says "Архив" — naming must follow UX, not schema.

## Consequences

- Easier: the aliasing is documented; future developers will not be surprised.
- Harder: nothing new; the semantic debt is intentionally carried.
- **Future trigger for Option B**: if `submitted` is ever needed to mean
  "user submitted lot to deal" independently of archive state, open a migration
  task to split the column and supersede this ADR.
- `docs/glossary.md` updated with `toggle_archive` entry pointing here.
