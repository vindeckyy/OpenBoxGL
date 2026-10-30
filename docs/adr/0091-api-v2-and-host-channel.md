# ADR 0091: API v2-only public surface and the native host channel

Status: Accepted and implemented (2026-10-29)

## Context

The frontend called `/api/v1/*`, newer features used `/api/v2/*`, and the native hosts called unversioned `/api/launch`, `/api/shutdown`, `/api/v2/resume`, and `/api/native/capabilities`. The 1.19 route tables built v1 aliases by hand.

## Decision

- Every route has a visibility: `public` (under `/api/v2/`), `host` (under `/api/host/`), or `internal`. Registration fails when the prefix does not match (`routes/registry.py`). A v1 alias can never be public.
- Every unversioned `/api/*` table entry gets a `/api/v2` twin with the same spec. Explicit `/api/v2` handlers keep precedence.
- The native hosts call `/api/host/launch`, `/api/host/resume`, `/api/host/shutdown`, and `/api/host/capabilities`. The legacy names stay routable through 2.0.x for old hosts.
- `/api/v1/*` is off by default. `OPENBOX_ENABLE_V1_API=1` restores the frozen 61-route surface with `Deprecation` and `Sunset` headers. Off returns `410 V1_API_DISABLED`.
- A native host that sends `X-OpenBox-API-Version` with a major other than 2 is refused with `409 HOST_API_UNSUPPORTED`.
- Errors are `application/problem+json`: `type`, `title`, `status`, `detail`, `code`, `retryable`, `request_id`, and the legacy `error` field.
- `GET /api/v2/version` advertises the state, API, plugin, backup, and sync versions.
- `docs/openapi.json` is generated from the live public surface. Operations without a hand-written schema are marked `x-openbox-schema: untyped`; they are not guessed.

## Consequences

- Scripts that call `/api/v1` need `OPENBOX_ENABLE_V1_API=1` until 2.1, when the bridge is removed.
- The frontend calls `/api/v2` through one `apiPath()` helper (`static/util.js`). `scripts/check_api_paths.py` blocks `/api/v1` and direct unversioned requests.

## Verification

`tests/test_api_v2_contract.py`, `tests/test_routes_registry.py`, `tests/test_native_ipc.py` (with `PYTHONPATH=.`), `scripts/check_api_contract.py`, `scripts/check_api_paths.py`, `scripts/check_native_paths.py`, `scripts/check_openapi.py`.
