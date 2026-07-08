## Summary

<!-- What does this PR change and why? -->

## Checklist

- [ ] **Invariant impact**: which `GLOBAL-INVARIANTS` entries does this touch? (`doc_id / heading path`)
- [ ] **Contract impact**: does this add/remove/rename any field on a cross-process model (`contracts/envelope.py` / `contracts/patch.py`)? If yes: schema_version bumped + dual-version test added.
- [ ] **Architecture boundary**: any new cross-module import? If yes: declare the public face / owner (`SYSTEM-BOUNDARIES / 2 + / 4`).
- [ ] **Migration**: any schema change? If yes: migration file added + expand→backfill→switch→contract steps documented below.
- [ ] **Rollback**: how to revert this change safely?
- [ ] **Test IDs**: which `tests/architecture/` or functional tests cover this change?

## Migration details (if any)

<!-- version / name / online_safe / requires_backup / backfill plan -->

## Architecture exceptions (if any)

<!-- New violations must reference an ADR with a clear_by date. -->
