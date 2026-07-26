# BUG-001 — Non-object JSON drops POST API connections

priority: P1
status: fixed
area: API validation
owner_story: e01s04

## Symptom

Authenticated POST requests with valid JSON whose top-level value is an array, null, or string close the connection without an HTTP response. Nested wrong types can do the same when they raise `TypeError` or `AttributeError`.

## Deterministic reproduction

```bash
python3 -B test_bug_sweep_api.py --group validation
```

Pre-fix result: `http.client.RemoteDisconnected: Remote end closed connection without response` for `[]` after malformed JSON correctly returns 400.

## Root cause

Mechanism: `Handler.do_POST()` passes `self.body()` directly to route handlers assuming a dictionary. Non-object JSON reaches `.items()`/`.get()` calls, raising `AttributeError`; nested invalid values can raise `TypeError`. Neither exception is in the `do_POST` 400-response catch list.

Trigger: an authenticated POST with syntactically valid JSON of the wrong shape, such as `[]`, `null`, `"text"`, or a wrong nested scalar used by `int()`/mapping operations.

Discriminating check: `{` raises `JSONDecodeError` and returns 400; `[]` raises uncaught `AttributeError` and drops the connection. The server process survives, proving the request handler exception—not server shutdown—is responsible.

## Purpose / callers / contracts

- Purpose: `web_app.Handler.do_POST` authenticates, decodes, routes, and bounds failures for all mutation endpoints.
- Callers: `index.html`, local API clients, deep links/integrations, and API tests.
- Contract: malformed request shapes return bounded 4xx JSON and cannot terminate a request without a response.

## Impact

All POST routes share this dispatcher. Exploitation requires the local token, but browser/client bugs or local callers can trigger opaque NetworkError behavior. No data loss was observed; server remains alive.

## Requirement delta

### MODIFIED: POST request shape validation

**Before:** Syntactically valid non-object JSON or nested wrong types can raise uncaught exceptions and close the HTTP connection.

**After:** The dispatcher rejects non-object top-level JSON explicitly and maps request-shape `TypeError`/`AttributeError` failures to status 400 JSON while remaining available.

## Red evidence

Command: `python3 -B test_bug_sweep_api.py --group validation`

Result before production edit: exit 1 with `RemoteDisconnected`.

## Planned smallest fix

Validate that decoded payload is a dictionary before routing and include `TypeError`/`AttributeError` in the existing request-validation exception mapping. Add a durable real-HTTP regression to `test_parity_api.py`.

## Verification

```bash
python3 -B test_parity_api.py
python3 -B test_bug_sweep_api.py --group validation
python3 -B test_secrets.py
./run_all_tests.sh
```

Post-fix results: durable API regression 13/13, boundary regression 6/6, secrets 3/3, and full runner 27 modules / 71 unittest-reported cases; all exit 0 with no failure/warning signals.
