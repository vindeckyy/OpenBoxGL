# e01s04 root-cause and fix ledger

confirmed_count: 1
fix_budget_hours: 3
status: fixed

## Ranked queue

1. BUG-001 — P1 API validation: valid non-object JSON drops authenticated POST connections.
2. UI-001 — P3 browser polish: missing favicon creates a harmless console 404; excluded from today's P1/P2 fix budget.

No other P1/P2 candidate was confirmed by automated, API, or browser discovery.

## BUG-001

priority: P1
status: fixed
root_cause: `Handler.do_POST` assumed decoded JSON was a mapping and omitted request-shape TypeError/AttributeError from its bounded 400 error mapping.
trigger: authenticated POST body with top-level `[]`, `null`, or string; nested wrong types could trigger the same exception classes.
reproduction: `python3 -B test_bug_sweep_api.py --group validation`
red_evidence: durable pre-fix `test_parity_api.py` run exited 1 with `RemoteDisconnected`; boundary validation independently reproduced the same result.
impact: all POST mutation routes share the dispatcher; server survives but the request receives no HTTP response.
requirement_before: valid JSON of the wrong shape could close the connection.
requirement_after: non-object JSON is rejected explicitly and request-shape TypeError/AttributeError returns status 400 JSON.
changed_files: `web_app.py`, `test_parity_api.py`, `test_bug_sweep_api.py`.
fix: two-line top-level mapping guard plus two exception classes added to the existing dispatcher error boundary.
targeted_verify: `python3 -B test_parity_api.py && python3 -B test_bug_sweep_api.py --group validation` — passing.
neighbor_verify: complete six-group boundary suite and `test_sessions.py` — passing.
security_verify: `python3 -B test_secrets.py` — 3/3 passing; no new security findings in affected paths.
full_verify: `./run_all_tests.sh` — 27 modules, 71 unittest-reported cases, exit 0, no failure/warning signals.
instrumentation: none.

## UI-001

priority: P3
status: deferred
reason: cosmetic missing `/favicon.ico`; no functional impact; excluded by scope.
next_action: serve the existing OpenBox icon or add an explicit icon link in a later patch.
evidence: `specs/verifications/e01s03-browser-sweep.md`.

## Working-tree preservation

- The initial user-owned dirty paths remain: `DISCLAIMER.md`, `README.md`, `SECURITY.md`, `index.html`, `openbox.desktop`, `openbox.metainfo.xml`, `test_packaging.py`, and untracked `TRADEMARKS.md`.
- BUG-001 production/test edits are limited to previously clean `web_app.py`, `test_parity_api.py`, and new `test_bug_sweep_api.py`.
- The browser sweep observed the user's current `index.html` but did not edit it.
- No temporary `debug-*` instrumentation ledger exists.
- No real OpenBox user data was changed.

## Queue closure

All P1/P2 candidates are fixed, rejected, or absent. UI-001 is terminally deferred as P3. No item remains confirmed, queued, fixing, or unverified.
