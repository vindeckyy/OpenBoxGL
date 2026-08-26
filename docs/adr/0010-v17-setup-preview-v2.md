# ADR 0010: v1.7 setup preview/commit and additive v2 contracts

Date: 2026-08-25
Status: Accepted

## Context

OpenBox v1.7 introduces a Library Setup Center that must preview imports and metadata work without mutating canonical state, then commit idempotently. The release adds exact v2 routes alongside every frozen `/api/v1/*` and legacy path. `library.json` must remain schema version **6** for rollback compatibility; preview data lives in separate atomic files outside canonical storage.

## Decision

### Preview and commit lifecycle

- **Scan preview** (`POST /api/v2/setup/preview`) performs read-only classification: no writes to `library.json`, no installs, no archive extraction, no media downloads, and no ROM-folder mutations.
- Previews are stored in private atomic files under the OpenBox data directory, retained at most **10** previews for **24 hours**; active jobs keep their preview alive.
- Preview size is capped at **100,000** scanned entries; exceeding the cap returns a structured error rather than silent truncation.
- Results paginate via opaque cursors bound to preview ID and revision (`GET /api/v2/setup/preview/items`).
- Review decisions apply in batches of at most **200** (`POST /api/v2/setup/preview/decisions`).
- **Revalidate** (`POST /api/v2/setup/preview/revalidate`) compares source fingerprints and library signature before commit.
- **Commit** (`POST /api/v2/setup/commit`) stages generated files under the data directory, atomically promotes them, then performs one state transaction. Commit is idempotent: retrying the same preview cannot duplicate games or repeat completed merges.
- **Summary** (`GET /api/v2/setup/summary`) exposes library overview metrics for the Setup Center entry step.
- Expired or corrupt previews are safely discardable and never affect the library.

### Additive exact v2 routes

All routes require the existing OpenBox token and return structured request IDs on errors. Every frozen v1 route and legacy path remains registered unchanged.

| Method | Path |
|---|---|
| GET | `/api/v2/setup/summary` |
| POST | `/api/v2/setup/preview` |
| GET | `/api/v2/setup/preview` |
| GET | `/api/v2/setup/preview/items` |
| POST | `/api/v2/setup/preview/decisions` |
| POST | `/api/v2/setup/preview/revalidate` |
| POST | `/api/v2/setup/commit` |
| GET | `/api/v2/emulators/registry` |
| POST | `/api/v2/launch/preflight` |
| POST | `/api/v2/launch/preflight/batch` |
| POST | `/api/v2/metadata/matches/preview` |
| GET | `/api/v2/metadata/matches/preview` |
| GET | `/api/v2/metadata/matches/items` |
| POST | `/api/v2/metadata/matches/decisions` |
| POST | `/api/v2/metadata/matches/apply` |
| GET | `/api/v2/jobs` |
| GET | `/api/v2/jobs/items` |
| POST | `/api/v2/jobs/cancel` |
| POST | `/api/v2/jobs/retry` |
| POST | `/api/v2/jobs/resume` |

Routes are exact, non-parameterized paths compatible with the current router. Route registration must fail on duplicate method/path pairs.

### Stable error codes

| Code | Typical use |
|---|---|
| `PREVIEW_NOT_FOUND` | Preview ID missing or already discarded |
| `PREVIEW_EXPIRED` | Preview past retention window |
| `PREVIEW_STALE` | Source fingerprints changed since last validation |
| `PREVIEW_LIBRARY_CHANGED` | Library signature changed since preview classification |
| `UNRESOLVED_CANDIDATES` | Commit blocked by unresolved review decisions |
| `AMBIGUOUS_PLATFORM` | Scanner cannot resolve platform without explicit choice |
| `EMULATOR_REQUIRED` | Launch readiness blocked by missing emulator/adapter |
| `LAUNCH_PREFLIGHT_BLOCKED` | Launch Doctor found blocking checks |
| `JOB_STATE_CONFLICT` | Operation not in expected state for requested action |
| `JOB_NOT_CANCELLABLE` | Operation in unsafe commit/restore phase |
| `JOB_NOT_RESUMABLE` | No checkpoint-safe resume point |
| `CLOUD_REMOTE_INVALID` | Remote cloud JSON invalid or unreadable |

### Schema version 6

- Canonical `library.json` schema version stays **6**. No schema bump in v1.7.
- Preview files, operation metadata, and import-batch tags are additive disposable storage or backward-compatible unknown fields (`emulator_id`, `emulator_adapter_id`, `import_batch_id`, bounded metadata-candidate rejection records).
- `emulator_def` remains a compatibility alias.

## Consequences

- Setup Center can trust preview/commit separation without breaking rollback to pre-1.7 state files.
- v2 contracts are documented and gated; v1 contract checks continue to pass without version bump.
- No frozen v1 path is dropped; no canonical schema migration is required.
