# e01s02 API and failure-boundary sweep

Date: 2026-07-26
Data root: temporary `OPENBOX_DATA_DIR` created by `test_bug_sweep_api.py` and removed after each group process.
Token: synthetic `sweep-token`; no real credential used or recorded.

## Fixture

status: fixed

- Isolated server binds loopback on an ephemeral port.
- Fixture library contains one `/bin/true` game and canary settings.
- `web_app.DATA` is asserted inside the temporary root.
- POST `/api/health` succeeds before/after fault groups.
- Server shutdown, thread join, socket close, and temporary cleanup occur in `tearDownClass`.

## Authorization

status: fixed

- 49 literal GET API route families were tested with missing and wrong tokens.
- Every protected route returned 403 before route-specific work.
- Query-token access to `/api/library` returned 200.
- Existing premium-route and secret tests pass.

## Validation

status: fixed

- Invalid JSON returns 400 JSON.
- Oversized request bodies return 400 JSON.
- Unknown POST route returns 404 JSON.
- Non-object JSON (`[]`, `null`, string) initially dropped the connection: BUG-001.
- BUG-001 fixed by dispatcher-level object validation plus bounded TypeError/AttributeError mapping.
- Durable regression added to `test_parity_api.py`; sweep validation now passes.

## Exceptions

status: fixed

- Forced malformed update metadata (`AttributeError`) returns 400 JSON.
- Forced storefront source failure (`ValueError`) returns 400 JSON.
- Health request succeeds afterward.

## Settings

status: fixed

- Partial settings POST changes only `screensaver_seconds`.
- Existing watch folder and storefront auto-import canaries survive.
- Raw Gameyfin password is absent from GET `/api/settings`; only `gameyfin_password_set` is exposed.
- `test_secrets.py` passes.

## Lifecycle

status: fixed

- Missing game save/list and discovery IDs return 404 JSON.
- Missing Gameyfin install ID returns 400 JSON.
- Unknown session control remains a bounded JSON response.
- Health succeeds after lifecycle errors.

## Candidates

- id: BUG-001
  priority: P1
  area: API validation
  status: fixed
  reproduction: `python3 -B test_bug_sweep_api.py --group validation`
  red_evidence: RemoteDisconnected for valid non-object JSON
  root_cause: dispatcher assumed a dict and excluded TypeError/AttributeError from request error mapping
  fix: validate top-level object; map request-shape TypeError/AttributeError to 400 JSON
  durable_regression: `python3 -B test_parity_api.py`
  evidence: `specs/bugs/BUG-001-non-object-json-drops-api-connection.md`

## Final verification

All six sweep groups pass. `test_parity_api.py`, `test_sessions.py`, and `test_secrets.py` pass. No new security findings in affected paths remain undispositioned.
