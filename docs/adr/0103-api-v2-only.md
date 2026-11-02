# ADR 0103: API v2 only, and retirement of the compatibility surfaces

Status: Accepted and implemented (2026-11-01)

## Context

2.0 introduced `/api/v2` and `/api/host`, kept an opt-in `/api/v1` bridge, kept unversioned `/api/*` as an internal layer, and kept legacy host names for pre-2.0 hosts. ADR 0100 schedules all of them for removal in 2.1. The frontend already uses `/api/v2`; the native hosts already use `/api/host`.

## Decision

- The public API is `/api/v2`. An unversioned `/api/*` base path is a private implementation alias used to build the v2 twin; it is not dispatched.
- The native host channel is `/api/host`. `/api/launch`, `/api/shutdown`, and `/api/native/capabilities` are removed.
- `/api/v1/*` is removed. `OPENBOX_ENABLE_V1_API` is ignored and logs a startup warning.
- Retired requests are authenticated first. Unauthenticated → `403`; authenticated → `410` with `code`, `context.retired`, `context.use`, and `context.since`.
- `routes.registry.all_routes()` synthesizes public `/api/v2` aliases for private base routes so gates and docs describe the public surface. Explicit v2 registrations win.
- The jobs panel uses `/api/v2/jobs`; the 1.19 job-name map and `/api/jobs` are gone.

## Consequences

- A pre-2.0 native host must be updated; a 2.0+ host keeps working.
- A script on v1 or an unversioned path must move to v2.
- The public v2 surface is unchanged: 358 paths / 387 operations, plus 4 host operations.

## Verification

`tests/test_api_v2_contract.py`, `tests/test_retired_surfaces.py`, `scripts/check_api_contract.py`, `scripts/check_routes_contract.py`, `scripts/check_api_paths.py`, `scripts/check_native_paths.py`.
