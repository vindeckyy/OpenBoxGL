# e01 candidate ledger

## Candidates

No machine-detected candidates were produced by e01s01.

- id: BASELINE-001
  area: repository baseline
  evidence: `specs/verifications/e01s01-baseline.md`
  status: rejected
  reason: full suite, compile check, and independent critical groups all passed without failure or warning signals
  owner_story: e01s01

- id: BUG-001
  area: API validation
  priority: P1
  evidence: `specs/bugs/BUG-001-non-object-json-drops-api-connection.md`
  reproduction_count: 2 deterministic sweep runs plus durable red regression
  status: fixed
  reason: non-object JSON raised uncaught request-shape exceptions; dispatcher now returns 400 JSON
  owner_story: e01s04
  verify: `python3 -B test_parity_api.py && python3 -B test_bug_sweep_api.py --group validation`

- id: UI-001
  area: browser polish
  priority: P3
  evidence: `specs/verifications/e01s03-browser-sweep.md`
  reproduction_count: 2 browser runs
  status: deferred
  reason: missing `/favicon.ico` produces a harmless 404 console resource message
  owner_story: e01s03
  recommended_action: serve the existing icon in a later patch

All e01s01–e01s03 candidates now have evidence, priority, reproduction count, owner story, and terminal current disposition.
