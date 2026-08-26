# ADR 0011: Durable operations and legacy jobs adapter

Date: 2026-08-25
Status: Accepted

## Context

OpenBox v1.7 replaces parallel in-memory job maps with one authoritative operation service for Setup scans, metadata work, media downloads, emulator installs, backups, and cloud sync. Long-running work must survive restarts, expose truthful cancellation/retry/resume semantics, and remain visible in the Activity Center. Existing clients still call `/api/jobs` and `/api/v1/jobs`.

## Decision

### Separate storage

- Persist nonsecret operation metadata in **`operations.json`**, separate from `library.json`.
- Write files with mode **0600** and atomic replacement.
- Exclude previews and operation history from portable exports by default.
- Store detailed per-item failures separately with pagination; bounded error summaries on the operation record.

### Operation model

Each operation contains: `job_id`, immutable `root_job_id`, optional `retry_of` / `resume_of`; type and human-readable title; state, phase, current, total, message; created, started, updated, and finished timestamps; `can_cancel`, `can_retry`, and `can_resume`; sanitized input summary and checkpoint; result counts.

### Lifecycle states

| State | Meaning |
|---|---|
| `queued` | Accepted, not yet running |
| `running` | Active work |
| `cancelling` | Cancel acknowledged; stops after current bounded I/O |
| `done` | Completed successfully |
| `partial` | Completed with recoverable failures |
| `error` | Terminal failure |
| `cancelled` | Cancelled before unsafe phases |
| `interrupted` | Was queued/running/cancelling at prior shutdown |

On startup, previously `queued`, `running`, or `cancelling` operations convert to **`interrupted`**. Resume only checkpoint-safe operations; otherwise expose Retry. Retry creates a new attempt linked via `retry_of`, preserving history.

### Retention

- Keep at most **100** operations.
- Drop completed items older than **30** days.

### Runtime behavior

- Persist progress at phase changes and no more than twice per second.
- Emit SSE at a bounded rate; always emit terminal events.
- Never claim an operation was cancelled while an unsafe commit/restore phase is already being atomically promoted.

### SSE event additions

- `job.queued`
- `job.progress`
- `job.cancelling`
- `job.finished`
- `job.interrupted`

### v2 jobs API

| Method | Path |
|---|---|
| GET | `/api/v2/jobs` |
| GET | `/api/v2/jobs/items` |
| POST | `/api/v2/jobs/cancel` |
| POST | `/api/v2/jobs/retry` |
| POST | `/api/v2/jobs/resume` |

Structured errors use `JOB_STATE_CONFLICT`, `JOB_NOT_CANCELLABLE`, and `JOB_NOT_RESUMABLE` (see ADR 0010).

### Legacy adapter

- Existing **`/api/jobs`** and **`/api/v1/jobs`** remain available through an adapter to the new operation model.
- Adapter translates list/cancel/retry/resume semantics without duplicating authoritative state.

## Consequences

- Activity Center reads one durable snapshot, then subscribes to SSE.
- Partial success remains visibly distinct from success and failure.
- Legacy job consumers continue to work for one release via the adapter.
