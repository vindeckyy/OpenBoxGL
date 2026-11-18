# ADR 0107: One declared state-machine registry

Status: Accepted for 2.0.

## Context

The app runs long-lived state machines — durable operations, the legacy job
mirror, per-feature job facades, sessions, previews, storefront scans, the media
queue — and each grew independently. Their literals were bare strings mutated at
10+ call sites, the same job entity was described by three vocabularies
(`operation`, `legacy_job`, `job_facade`), the frontend hand-rolled the operation
literal sets in eight places, and two concrete bugs followed from that drift:

- `METADATA_JOB` was one dict shared by the database-sync job and the auto-match
  job, so starting either one destroyed the other's visible state.
- `static/sessions.js` subscribed to a `session.state` SSE event that nothing
  emits.

ADR 0011 already documented the authoritative operation lifecycle in prose, but
nothing in code encoded its transition table.

## Decision

- `pkg/state/machines.py` declares every lifecycle as a frozen `Machine`
  (states, terminal set, active set, transition table, storage, persistence,
  API surface). It is standard-library only and importable by any layer.
- Declared does not mean rewritten: machines still driven by scattered flags
  (DOM overlays, wizards, native hosts) are catalogued read-only, so the
  registry is the complete inventory without a risky rewrite.
- `operation` is enforced: `pkg/state/operations.py` derives
  `ACTIVE_STATES`/`TERMINAL_STATES` from the machine and validates
  `mark_running`/`finish` against its transition table. `job_manager.py` derives
  its terminal and active sets from `legacy_job`.
- Facade jobs share one vocabulary mapping: `FACADE_TO_OPERATION` maps
  `installing/updating/downloading/running` to the operation's active state, and
  the facade sync never writes terminal states — `JobManager._notify` owns
  completion, so a facade write cannot race it or flip `partial` back to `done`.
- The preview projection (`done/partial/error` read as `ready`) lives once in
  `job_result_state()`; both preview handlers delegate to it.
- `METADATA_JOB` (sync) and `MATCH_JOB` (auto-match) are separate facades.
  `/api/metadata/status` keeps `job` (sync) and adds `match_job` — additive.
- Persisted and API literals are frozen: this ADR changes no stored value, API
  field, or SSE event name.
- `static/machines.js` mirrors the JS-visible sets (operation, facade,
  storefront) and the pollers import it, so a new state cannot silently wedge
  one poller while the others handle it.
- `tests/test_machines.py` pins the ADR 0011 table, registry completeness, the
  projection, the facade→operation sync, and the derived media-worker state.

## Consequences

- One table per lifecycle replaces scattered guards; adding a state is a
  registry edit plus a test, not a hunt through pollers.
- Agents get a machine-readable inventory (`MACHINES`) instead of inferring
  lifecycles from string literals.
- The UI reports `cancelling` consistently (launch audit previously treated it
  as idle) and metadata sync/match progress no longer clobber each other.
- Machines marked declared-only still hold their state in ad-hoc fields; moving
  one to enforced is a local change that adds `allows()` calls at its writers.
