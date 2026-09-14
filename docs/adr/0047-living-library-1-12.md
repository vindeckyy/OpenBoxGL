# ADR 0047: Living Library additions (1.12.0)

**Date:** 2026-09-14
**Status:** Accepted

## Context

1.11 landed Backlog Radio, the query grammar, Moments, and the backup
engine, but left four gaps: saved searches couldn't persist, per-game
history had no narrative surface, the launch sheet lacked environment
overrides and a confirm step, and backups had no schedule. The SQLite read
model also stayed opt-in behind an env var despite large-library demand.
All work must stay dependency-free, keep the v1 contract frozen, and write
only through the canonical state boundary.

## Decision

1. **Smart collections** live in `state["smart_collections"]` as
   `{name, query}` pairs (cap 50). Membership is evaluated at read time
   through `parity_query.parse_query` + `filter_games_by_query`, so a saved
   filter can never drift from the chips shown when it was saved. Unparsable
   queries (grammar drift) evaluate to zero, never an error.
2. **Game Story** (`GET /api/v2/story`) is a pure read projection over the
   game record, session journal, and Moments. Nothing is written, so it can
   never go stale.
3. **Per-game launch options** finish with `launch_env` (validated
   `KEY=value` map at save, merged over the spawn environment after
   MangoHud) and `launch_confirm` (client-side confirm before preflight).
   Merge order stays: game > platform > global.
4. **Weekly auto-backup** runs on an hourly daemon tick gated by
   `auto_backup_due()` in `parity_backup.py`: enabled flag plus a 7-day
   interval over `settings.last_auto_backup`. Missing/unparsable stamps
   count as due. Content and rotation reuse `create_backup` unchanged.
5. **SQLite self-enables at 5,000 games** via `should_auto_enable()`,
   latched per process. An explicit `OPENBOX_ENABLE_SQLITE_READ=0/false/no`
   opt-out is never overridden. The response `source` field already
   distinguishes `sqlite` from `json`.
6. **CSP framing contract** is gate-enforced (`scripts/check_csp.py`,
   Stage 2.8): everything stays `frame-ancestors 'none'` except the two
   document endpoints, which relax only framing to `'self'`.

## Consequences

- New v2 routes: `GET/POST /api/v2/collections`,
  `POST /api/v2/collections/delete`, `GET /api/v2/story`. No v1 changes.
- New state fields: `smart_collections`, `launch_env`, `launch_confirm`,
  `backup_auto_enabled`, `backup_auto_keep`, `last_auto_backup`.
- Large libraries get FTS-path search without configuration; small
  libraries see byte-identical behavior.
- The 1.11 reader-framing regression class cannot return without failing
  the gate.
