# ADR 0050: Library repair wizard and duplicate merge contracts

**Date:** 2026-09-22
**Status:** Accepted

## Context

The health report could list missing files and duplicate entries but offered
no safe way to fix them. The old blind dedupe deleted records without a
preview, and relinking moved files by hand-editing paths. Any fix must be
reversible and must never write an arbitrary client-supplied path.

## Decision

- `pkg/parity/parity_repair.py` owns the missing-file wizard. `scan_missing_paths()`
  projects the same missing-path information health shows (bounded, detached).
  `scan_candidates()` walks a user-picked folder with entry/depth caps.
  `plan_repair()` matches missing basenames against candidates and keeps
  ambiguous matches out of the automatic plan. `apply_repair()` re-validates
  every target inside the caller's transaction and skips stale rows.
- `pkg/parity/parity_duplicates.py` owns duplicate detection and merge.
  `find_duplicates()` reports `identity` (canonical identities via
  `parity_identity.detect_duplicate_identities`, reused not reimplemented),
  `path`, and `title` collision groups, bounded and read-only. `merge_plan()`
  previews the primary pick (most play history) and every field union;
  `apply_merge()` hands absorbed records to the caller's `trash_game`
  callback so the Trash bin is the undo mechanism.
- Routes (all under `/api/v2`, v1 stays frozen):
  `GET /api/v2/library/repair`, `POST /api/v2/library/repair/preview`,
  `POST /api/v2/library/repair/apply`, `GET /api/v2/library/duplicates`,
  `POST /api/v2/library/duplicates/preview`,
  `POST /api/v2/library/duplicates/merge`.
- The client never sends replacement paths: preview and apply both derive
  the plan from the chosen folder, so an apply cannot write an arbitrary
  path. The health dialog's dedupe button now opens the merge dialog; blind
  dedupe is retired.

## Consequences

- Repair and merge are dry-runnable, bounded, and reversible (skipped-row
  reports and the Trash bin respectively). Partial failures report per entry
  rather than failing the whole batch.
- Identity collisions follow the canonical identity rules, so a path-only
  collision can surface as an identity group; fuzzy path/title matching
  remains the fallback for records without usable identities.
