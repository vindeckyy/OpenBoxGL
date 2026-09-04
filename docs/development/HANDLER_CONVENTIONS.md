# Handler Conventions

How handlers are structured, validated, and wired in OpenBox.

## Error types

- `BadRequest` (400): input validation failure. Use for missing required fields, invalid values, type mismatches.
- `NotFound` (404): requested resource does not exist. Use `GameNotFound`, `DocumentNotFound`, `PlatformDocumentNotFound` as appropriate.
- `Conflict` (409): operation cannot proceed due to current state (e.g., metadata database not downloaded).
- `ValueError`: caught by the framework and mapped to 400. Prefer raising `BadRequest` directly for machine-readable codes.
- `FileNotFoundError`: caught by the framework and mapped to 404.

## State access

- **GET handlers**: use `load_state_view()` (cached read-only). Never `load_state()` unless you need fresh state for validation before mutation.
- **POST handlers**: use `transact_state(mutate_fn)` for atomic read-modify-write. The mutate function receives the state dict and returns a result.
- **POST handlers (read-only)**: use `load_state_view()` for endpoints that read state but don't modify it.

## Route registration

Routes are registered via the `@route(method, path)` decorator from `routes.registry`. Two styles exist:

- **Class-style** (`handlers/library.py`: `class LibraryHandlers`): the method name follows the pattern `_api_{method}_{path_underscored}`.
- **Function-style** (`handlers/native.py`: plain `def capabilities(handler, parsed)`): the function name is free-form; the decorator carries the method + path.

## Input validation

Follow the `_clean_*` function pattern from `handlers/settings.py`:
- Extract validation into a `_clean_field(merged)` function.
- Raise `BadRequest` for validation failures (machine-readable code). Legacy `_clean_*` helpers raise `ValueError`, which the framework also maps to 400 — keep that working, but prefer `BadRequest` in new code.
- Return the cleaned value.

## Async jobs

Long-running operations use `JOB_MANAGER.submit(name, worker_fn)` and return HTTP 202. The worker function runs in a background thread and updates a shared status dict under `PROCESS_LOCK`.

## Shared utilities

Cross-handler helper functions live in `handlers/_shared.py`. Import from there rather than calling across handler classes via `self`.
