# ADR 0056: Library health score — weights and never-auto-fix

**Date:** 2026-09-22
**Status:** Accepted

## Context

The v1 audit (`POST /api/health`) returns a flat issue list but no single
actionable number, no explanation of what drags a library down, and no
reversible one-pass fix per problem class. A score that cannot be explained
or acted on is decoration; a score that silently repairs is a liability.

## Decision

- Scoring lives in `pkg/parity/parity_library_health.py` as pure functions
  (`detect_issues`, `score_library`, `merge_incremental`). The only I/O is
  cached path probes. The v1 `health()` handler delegates detection to the
  same function; its response shape is pinned and unchanged.
- Five dimensions, fixed weights summing to 100:

  | Dimension | Weight | What it measures |
  |---|---|---|
  | File integrity | 35 | Missing game file, missing extras, missing save paths |
  | Duplicates | 20 | Duplicate identity groups (cross-source included) |
  | Artwork coverage | 20 | Missing media across `MEDIA_TYPES_ALL`, weighted by type priority (cover ≫ banner/icon; screenshots as a group) |
  | Metadata completeness | 15 | Presence of name/platform/genre/year/developer/description (presence-only, no quality judgment, no network) |
  | Launch readiness | 10 | ROM suffix with no emulator profile and no per-game override, plus games flagged `broken` |

  Rationale: a library whose files are gone is useless however pretty it is,
  hence integrity at 35. Duplicates and artwork tie at 20: duplicates waste
  space and confuse navigation; artwork is the visible face of the library.
  Metadata (15) aids discovery but a game plays fine without a genre tag.
  Launch readiness (10) matters only for ROMs, a subset of most libraries.

- Scoring rule: each finding deducts a fixed per-game amount inside its
  dimension; dimension sub-score = 100 − deductions, clamped at 0; total =
  round(Σ weight × sub-score / 100), clamped 0–100. The deduction ledger
  records `{dimension, points, reason, game_ids[]}` for every deduction, so
  the score is explainable by construction.
- **Never auto-fix.** The engine only measures. The v2 fix queue
  (`POST /api/v2/library/health/fix`) requires an explicit dry-run preview
  first, rejects stale previews via `base_token` (Time Machine pattern), and
  records an inverse operation in the bounded `state["health_fixes"]`
  journal (cap 50, pruned like trash). Destructive fixes route through the
  trash (`POST /api/v2/library/trash/restore` for undo); field changes
  record before/after values. Metadata and launch-readiness dimensions have
  no auto-fix at all — the queue links to the metadata editor and the
  emulator profile assignment instead.

## Consequences

- The score is a workflow (measure → explain → fix reversibly → re-measure),
  not a badge. The v2 API serves a cached snapshot; full scans run as the
  background `library-health-scan` job, paginated issue lists bound the
  20k-library case, and scheduled rescans (daily/weekly/on startup/off,
  default weekly) skip active game sessions.
- Weight changes are ADR-level decisions: changing a weight changes every
  user's score, so it needs the same review as a contract change.
